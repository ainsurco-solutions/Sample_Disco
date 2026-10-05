from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

import httpx
from dotenv import load_dotenv

from migration_hub.adapters.databridge.client import DatabridgeAdapter
from migration_hub.adapters.storage import databridge_uploader
from migration_hub.adapters.storage.s3_uploader import (
    S3_MIN_PART,
    multipart_chunksize,
)
from migration_hub.config.settings import Settings
from migration_hub.core.engine import create_registry_engine
from migration_hub.core.scrub import scrub_credentials

DEFAULT_MIN_WAIT_MINUTES = 120

_FORMAT_CODES = {"bak": 1, "mdf": 0, "dacpac": 2}

_EXCERPT_CHARS = 300
_MB = 1024 * 1024

Clock = Callable[[], datetime]

def _utcnow() -> datetime:
    return datetime.now(UTC)

def excerpt(response: httpx.Response) -> str:
    return scrub_credentials(response.text.strip())[:_EXCERPT_CHARS]

def part_url_of(response: httpx.Response) -> str:
    try:
        return str(response.json())
    except ValueError:
        return response.text.strip()

class Recorder:

    def __init__(self, now: Clock) -> None:
        self._now = now
        self.events: list[dict[str, object]] = []

    def add(self, step: str, *, status: int | None = None, note: str = "") -> None:
        event: dict[str, object] = {"at": self._now().isoformat(), "step": step}
        if status is not None:
            event["status"] = status
        if note:
            event["note"] = note
        self.events.append(event)
        print(
            f"  {step}"
            + (f"  ->  {status}" if status is not None else "")
            + (f"  {note}" if note else "")
        )

def _base(instance: str, db: str, ext: str) -> str:
    return f"/databridge/v1/sql-instances/{instance}/databases/{db}/{ext}"

def _init_upload(adapter: DatabridgeAdapter, base: str, log: Recorder, label: str) -> str | None:
    response = adapter.probe_request("POST", f"{base}/init-upload")
    if not response.is_success:
        log.add(f"{label} init-upload", status=response.status_code, note=excerpt(response))
        return None
    try:
        upload_id = str(response.json()["uploadId"])
    except (ValueError, KeyError, TypeError):
        log.add(f"{label} init-upload", status=response.status_code, note="no uploadId in body")
        return None
    log.add(f"{label} init-upload", status=response.status_code, note=f"uploadId {upload_id}")
    return upload_id

def _send_part(
    adapter: DatabridgeAdapter,
    *,
    base: str,
    upload_id: str,
    source: Path,
    chunk: int,
    part: int,
    log: Recorder,
    engine: object,
) -> str | None:
    response = adapter.probe_request("GET", f"{base}/upload-part/{upload_id}/{part}")
    if not response.is_success:
        log.add(f"part {part} URL", status=response.status_code, note=excerpt(response))
        return None
    url = part_url_of(response)
    with source.open("rb") as handle:
        handle.seek((part - 1) * chunk)
        data = handle.read(chunk)
    try:
        put = databridge_uploader._put(
            url,
            data,
            source=source,
            part=part,
            size=len(data),
            engine=engine,
            file_id=None,
            headers=databridge_uploader._PUT_HEADERS,
        )
    except Exception as exc:
        log.add(f"part {part} PUT", note=f"{type(exc).__name__}: {exc}")
        return None
    etag = str(put.headers.get("ETag", "")).strip('"')
    if not etag:
        log.add(f"part {part} PUT", status=put.status_code, note="no ETag")
        return None
    log.add(f"part {part} PUT", status=put.status_code, note=f"{len(data) / _MB:,.0f} MB")
    return etag

def _complete(
    adapter: DatabridgeAdapter, base: str, upload_id: str, etags: dict[int, str], log: Recorder
) -> bool:
    body = {"uploadId": upload_id, "etags": {str(k): v for k, v in sorted(etags.items())}}
    response = adapter.probe_request("POST", f"{base}/complete-upload", json=body)
    log.add(
        "complete-upload",
        status=response.status_code,
        note="" if response.is_success else excerpt(response),
    )
    return response.is_success

def plan_parts(size: int, chunk: int) -> int:
    return max(1, -(-size // chunk))

def chunk_for(size: int, chunk_mb: int | None) -> int:
    if chunk_mb is None:
        return multipart_chunksize(size)
    chunk = chunk_mb * _MB
    if chunk < S3_MIN_PART:
        raise SystemExit(f"--chunk-mb must be at least {S3_MIN_PART // _MB}")
    return chunk

def _save(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

def start(
    adapter: DatabridgeAdapter,
    *,
    source: Path,
    instance: str,
    db: str,
    chunk: int,
    parts: int | None,
    state_path: Path,
    engine: object = None,
    now: Clock = _utcnow,
) -> dict[str, object]:
    ext = source.suffix.lstrip(".").lower()
    if ext not in _FORMAT_CODES:
        raise SystemExit(f"unsupported file type: {source.suffix!r}")
    stat = source.stat()
    total = plan_parts(stat.st_size, chunk)
    if total < 2:
        raise SystemExit(
            f"{source.name} is one part at {chunk // _MB} MB chunks -- nothing left to "
            "resume. Use a larger file or a smaller --chunk-mb."
        )
    send = parts if parts is not None else total // 2
    if not 1 <= send < total:
        raise SystemExit(f"--parts must be between 1 and {total - 1} (the file has {total})")

    log = Recorder(now)
    base = _base(instance, db, ext)
    state: dict[str, object] = {
        "probe": "start",
        "db": db,
        "instance": instance,
        "ext": ext,
        "source": str(source),
        "source_size": stat.st_size,
        "source_mtime": stat.st_mtime,
        "chunk_bytes": chunk,
        "parts_total": total,
        "parts_planned": send,
        "started_at": now().isoformat(),
        "upload_id": None,
        "etags": {},
        "events": log.events,
    }
    print(f"start: {source.name}, {total} parts of {chunk // _MB} MB; sending 1..{send}")
    upload_id = _init_upload(adapter, base, log, "first")
    state["upload_id"] = upload_id
    etags: dict[str, str] = {}
    state["etags"] = etags
    if upload_id is not None:
        for part in range(1, send + 1):
            etag = _send_part(
                adapter,
                base=base,
                upload_id=upload_id,
                source=source,
                chunk=chunk,
                part=part,
                log=log,
                engine=engine,
            )
            if etag is None:
                break
            etags[str(part)] = etag
            _save(state_path, state)
    state["verdict"] = (
        f"STARTED -- {len(etags)} of {total} parts sent under uploadId {upload_id}, not completed"
        if upload_id is not None and etags
        else "NOT STARTED -- see the events"
    )
    _save(state_path, state)
    return state

def finish(
    adapter: DatabridgeAdapter,
    *,
    state_path: Path,
    min_wait_minutes: int = DEFAULT_MIN_WAIT_MINUTES,
    force: bool = False,
    do_import: bool = False,
    group_ids: list[str] | None = None,
    poll_seconds: float = 30.0,
    import_timeout_minutes: int = 120,
    engine: object = None,
    now: Clock = _utcnow,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    upload_id = state.get("upload_id")
    if not upload_id or not state.get("etags"):
        raise SystemExit(f"{state_path} holds no started upload -- run start again")
    source = Path(state["source"])
    stat = source.stat()
    if stat.st_size != state["source_size"] or stat.st_mtime != state["source_mtime"]:
        raise SystemExit(f"{source} changed since start -- its parts would not line up")

    elapsed = (now() - datetime.fromisoformat(state["started_at"])).total_seconds() / 60
    if elapsed < min_wait_minutes and not force:
        raise SystemExit(
            f"only {elapsed:.0f} min since start; the test needs {min_wait_minutes}. "
            "Wait, or pass --force."
        )

    log = Recorder(now)
    base = _base(state["instance"], state["db"], state["ext"])
    chunk = int(state["chunk_bytes"])
    total = int(state["parts_total"])
    etags = {int(k): v for k, v in state["etags"].items()}
    missing = [p for p in range(1, total + 1) if p not in etags]
    result: dict[str, object] = {
        "probe": "finish",
        "db": state["db"],
        "upload_id": upload_id,
        "minutes_since_start": round(elapsed),
        "parts_total": total,
        "parts_from_start": len(etags),
        "events": log.events,
    }
    print(
        f"finish: {elapsed:.0f} min after start; sending {len(missing)} part(s) "
        f"under uploadId {upload_id}"
    )

    verdict = ""
    sent_now = 0
    for part in missing:
        etag = _send_part(
            adapter,
            base=base,
            upload_id=upload_id,
            source=source,
            chunk=chunk,
            part=part,
            log=log,
            engine=engine,
        )
        if etag is None:
            verdict = (
                f"REFUSED -- part {part} could not be sent under the saved uploadId "
                f"{elapsed:.0f} min after start (see its event)"
            )
            break
        etags[part] = etag
        sent_now += 1
    result["parts_sent_now"] = sent_now

    if not verdict:
        if _complete(adapter, base, upload_id, etags, log):
            verdict = (
                f"RESUMED -- the uploadId took {sent_now} new part(s) and "
                f"completed {elapsed:.0f} min after start"
            )
        else:
            verdict = f"REFUSED -- complete-upload failed {elapsed:.0f} min after start"
    result["verdict"] = verdict

    if do_import and verdict.startswith("RESUMED"):
        result["import"] = _import_and_poll(
            adapter,
            instance=state["instance"],
            db=state["db"],
            format_code=_FORMAT_CODES[state["ext"]],
            group_ids=group_ids or [],
            poll_seconds=poll_seconds,
            timeout_minutes=import_timeout_minutes,
            log=log,
            sleep=sleep,
        )
    _save(state_path.with_name(state_path.stem + "_finish.json"), result)
    return result

def _import_and_poll(
    adapter: DatabridgeAdapter,
    *,
    instance: str,
    db: str,
    format_code: int,
    group_ids: list[str],
    poll_seconds: float,
    timeout_minutes: int,
    log: Recorder,
    sleep: Callable[[float], None],
) -> str:
    response = adapter.probe_request(
        "POST",
        f"/databridge/v1/sql-instances/{instance}/Databases/{db}/import",
        params={"importFrom": format_code},
        json={"groupIds": group_ids},
    )
    if not response.is_success:
        log.add("import", status=response.status_code, note=excerpt(response))
        return f"import refused: HTTP {response.status_code}"
    job_id = str(response.headers.get("Location", "")).rstrip("/").rsplit("/", 1)[-1]
    if not job_id:
        try:
            job_id = str(response.json().get("jobId") or "")
        except (ValueError, AttributeError):
            job_id = ""
    log.add("import", status=response.status_code, note=f"job {job_id or '(no id)'}")
    if not job_id:
        return "import started, but no job id to poll"

    polls = max(1, int(timeout_minutes * 60 / poll_seconds))
    for _ in range(polls):
        try:
            status = adapter.poll_job(job_id=job_id)
        except Exception as exc:
            log.add("poll", note=f"{type(exc).__name__}: {exc}")
            return f"poll failed: {type(exc).__name__}"
        if status.is_terminal:
            log.add("poll", note=status.raw_status)
            return f"job {job_id} {status.raw_status}"
        sleep(poll_seconds)
    log.add("poll", note="still running at the timeout")
    return f"job {job_id} still running after {timeout_minutes} min -- check the Jobs page"

def double_init(
    adapter: DatabridgeAdapter,
    *,
    source: Path,
    instance: str,
    db: str,
    chunk: int,
    out_path: Path,
    engine: object = None,
    now: Clock = _utcnow,
) -> dict[str, object]:
    ext = source.suffix.lstrip(".").lower()
    if ext not in _FORMAT_CODES:
        raise SystemExit(f"unsupported file type: {source.suffix!r}")
    total = plan_parts(source.stat().st_size, chunk)
    log = Recorder(now)
    base = _base(instance, db, ext)
    result: dict[str, object] = {
        "probe": "double-init",
        "db": db,
        "parts_total": total,
        "events": log.events,
    }
    print(f"double-init: {source.name}, {total} part(s) of {chunk // _MB} MB")

    first = _init_upload(adapter, base, log, "first")
    second = _init_upload(adapter, base, log, "second") if first else None
    result["first_upload_id"] = first
    result["second_upload_id"] = second
    if first is None:
        result["verdict"] = "NOT RUN -- the first init-upload failed"
        _save(out_path, result)
        return result
    if second is None:
        second_said = "the second init-upload was refused (see its event)"
    elif second == first:
        second_said = "the second init-upload returned the SAME uploadId"
    else:
        second_said = "the second init-upload returned a new uploadId"

    etags: dict[int, str] = {}
    for part in range(1, total + 1):
        etag = _send_part(
            adapter,
            base=base,
            upload_id=first,
            source=source,
            chunk=chunk,
            part=part,
            log=log,
            engine=engine,
        )
        if etag is None:
            break
        etags[part] = etag
    if len(etags) < total:
        first_said = f"the first uploadId stopped accepting parts at part {len(etags) + 1}"
    elif _complete(adapter, base, first, etags, log):
        first_said = "the first uploadId still took every part and completed"
    else:
        first_said = "the first uploadId took every part but complete-upload failed"

    if second and second != first:
        response = adapter.probe_request("GET", f"{base}/upload-part/{second}/1")
        log.add(
            "second uploadId part 1 URL",
            status=response.status_code,
            note="" if response.is_success else excerpt(response),
        )

    result["verdict"] = f"{second_said}; {first_said}"
    _save(out_path, result)
    return result

def _settings() -> Settings:
    load_dotenv(Path.cwd() / ".env", override=False)
    environment = os.environ.get("MIGRATION_HUB_ENV", "dev")
    config_dir = Path(os.environ.get("MIGRATION_HUB_CONFIG_DIR", "config"))
    return Settings.load(environment=environment, config_dir=config_dir)

def _state_path(db: str) -> Path:
    return Path("results") / f"probe_resume_{db}.json"

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Does the tenant keep an unfinished large upload alive? (TASK-0128)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="init-upload, send parts 1..k, stop")
    p_start.add_argument("--file", required=True, type=Path, help="a test .bak/.mdf/.dacpac")
    p_start.add_argument("--db", required=True, help="a throwaway database name")
    p_start.add_argument("--instance", help="Data Bridge instance (default: from settings)")
    p_start.add_argument("--parts", type=int, help="parts to send now (default: half)")
    p_start.add_argument("--chunk-mb", type=int, help="part size in MB (default: the Hub's)")
    p_start.add_argument("--overwrite", action="store_true", help="replace a saved start")

    p_finish = sub.add_parser("finish", help="send the rest under the saved uploadId")
    p_finish.add_argument("--db", required=True, help="the database name used for start")
    p_finish.add_argument(
        "--min-wait-minutes",
        type=int,
        default=DEFAULT_MIN_WAIT_MINUTES,
        help="refuse to run sooner than this after start (default: 120)",
    )
    p_finish.add_argument("--force", action="store_true", help="run before the wait is up")
    p_finish.add_argument(
        "--import",
        dest="do_import",
        action="store_true",
        help="import the completed file and poll the job",
    )

    p_double = sub.add_parser("double-init", help="init-upload twice, finish the first")
    p_double.add_argument("--file", required=True, type=Path, help="a small test file")
    p_double.add_argument("--db", required=True, help="a throwaway database name")
    p_double.add_argument("--instance", help="Data Bridge instance (default: from settings)")
    p_double.add_argument("--chunk-mb", type=int, help="part size in MB (default: the Hub's)")

    args = parser.parse_args()
    settings = _settings()
    engine = create_registry_engine(settings.database_url)
    adapter = DatabridgeAdapter(host=settings.api_host, api_key=settings.api_key(), engine=engine)
    instance = (
        getattr(args, "instance", None)
        or settings.databridge_instance_name
        or next(iter(settings.databridge_instances.values()), None)
    )

    try:
        if args.command == "start":
            if not instance:
                raise SystemExit("no Data Bridge instance in settings -- pass --instance")
            state_path = _state_path(args.db)
            if state_path.exists() and not args.overwrite:
                raise SystemExit(f"{state_path} exists -- run finish, or pass --overwrite")
            size = args.file.stat().st_size
            result = start(
                adapter,
                source=args.file,
                instance=instance,
                db=args.db,
                chunk=chunk_for(size, args.chunk_mb),
                parts=args.parts,
                state_path=state_path,
                engine=engine,
            )
            saved = state_path
            follow_up = (
                f"next: wait {DEFAULT_MIN_WAIT_MINUTES} min, then run "
                f"finish --db {args.db} --import"
            )
        elif args.command == "finish":
            state_path = _state_path(args.db)
            result = finish(
                adapter,
                state_path=state_path,
                min_wait_minutes=args.min_wait_minutes,
                force=args.force,
                do_import=args.do_import,
                group_ids=list(settings.databridge_group_ids),
                engine=engine,
            )
            saved = state_path.with_name(state_path.stem + "_finish.json")
            follow_up = "the database is now on Data Bridge if it imported -- remove it after"
        else:
            if not instance:
                raise SystemExit("no Data Bridge instance in settings -- pass --instance")
            saved = Path("results") / f"probe_resume_{args.db}_double_init.json"
            result = double_init(
                adapter,
                source=args.file,
                instance=instance,
                db=args.db,
                chunk=chunk_for(args.file.stat().st_size, args.chunk_mb),
                out_path=saved,
                engine=engine,
            )
            follow_up = "the second uploadId (if any) is left unfinished on purpose"
    finally:
        adapter.close()

    print()
    print(f"VERDICT: {result['verdict']}")
    if "import" in result:
        print(f"IMPORT:  {result['import']}")
    print(f"saved {saved} -- send this file")
    print(follow_up)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
