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
for _src in (_HERE.parent / "src", Path.cwd() / "src"):
    if _src.is_dir():
        sys.path.insert(0, str(_src))
        break

import httpx
from dotenv import load_dotenv

from migration_hub.adapters.databridge.client import (
    _FAILURE_STATUSES,
    _SUCCESS_STATUSES,
    DatabridgeAdapter,
)
from migration_hub.config.settings import Settings
from migration_hub.core.engine import create_registry_engine
from migration_hub.core.models import MigrationFile
from migration_hub.core.registry import Registry
from migration_hub.core.scrub import scrub_credentials

_FORMAT_CODES = {"bak": 1, "mdf": 0, "dacpac": 2}

_IMPORT_FAILED_MARK = "Import job "

_PROBE_STATES = frozenset({"FAILED", "ABANDONED"})

_EXCERPT_CHARS = 500
DEFAULT_POLL_SECONDS = 30.0
DEFAULT_MAX_MINUTES = 240.0

Clock = Callable[[], datetime]

def _utcnow() -> datetime:
    return datetime.now(UTC)

def excerpt(response: httpx.Response) -> str:
    return str(scrub_credentials(response.text.strip()))[:_EXCERPT_CHARS]

def extension_of(source_path: str) -> str:
    name = source_path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""

def refusal(file: MigrationFile | None, *, check_error: bool = True) -> str | None:
    if file is None:
        return "no such file in this registry -- run on the machine that uploaded it"
    if file.state not in _PROBE_STATES:
        return f"file is {file.state}, not FAILED or ABANDONED -- leaving it alone"
    if check_error and _IMPORT_FAILED_MARK not in (file.last_error or ""):
        return f"its last failure was not the import job: {file.last_error or '(none recorded)'}"
    if not file.instance_name:
        return "no instance_name recorded -- the file never started uploading"
    ext = extension_of(file.source_path)
    if ext not in _FORMAT_CODES:
        return f"unsupported file type: {ext!r}"
    return None

def job_id_of(response: httpx.Response) -> str | None:
    location = response.headers.get("Location")
    if location:
        return str(location).rstrip("/").rsplit("/", 1)[-1]
    try:
        body = response.json()
    except ValueError:
        return None
    job_id = body.get("jobId") if isinstance(body, dict) else None
    return str(job_id) if job_id else None

def status_of(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()
    if isinstance(body, dict):
        return str(body.get("status", ""))
    return str(body)

def reimport(
    adapter: DatabridgeAdapter,
    *,
    file: MigrationFile,
    group_ids: list[str],
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    max_minutes: float = DEFAULT_MAX_MINUTES,
    sleep: Callable[[float], None] = time.sleep,
    now: Clock = _utcnow,
) -> dict[str, object]:
    db = file.database_name or file.source_database
    instance = str(file.instance_name)
    ext = extension_of(file.source_path)
    result: dict[str, object] = {
        "probe": "reimport",
        "file_id": file.file_id,
        "db": db,
        "instance": instance,
        "last_error": file.last_error,
        "started_at": now().isoformat(),
    }

    def say(line: str) -> None:
        print(f"  {now():%H:%M:%S}  {line}")

    with adapter.bound_to_file(file.file_id):
        if adapter.archive_exists(database_name=db):
            say("already in Data Vault -- nothing triggered")
            result["verdict"] = "IN_DATA_VAULT"
            return result
        if adapter.database_exists(instance_name=instance, database_name=db):
            say("already on Data Bridge -- nothing triggered")
            result["verdict"] = "ON_DATA_BRIDGE"
            return result
        say("in neither Data Vault nor Data Bridge -- starting the import, no upload")

        trigger = adapter.probe_request(
            "POST",
            f"/databridge/v1/sql-instances/{instance}/Databases/{db}/import",
            params={"importFrom": _FORMAT_CODES[ext]},
            json={"groupIds": group_ids},
        )
        result["trigger_status"] = trigger.status_code
        job_id = job_id_of(trigger) if trigger.is_success else None
        if job_id is None:
            result["trigger_response"] = excerpt(trigger)
            say(f"import trigger -> {trigger.status_code}  {excerpt(trigger)}")
            result["verdict"] = "REFUSED"
            return result
        result["job_id"] = job_id
        say(f"import trigger -> {trigger.status_code}  job {job_id}")

        deadline = time.monotonic() + max_minutes * 60
        last = ""
        while True:
            poll = adapter.probe_get(f"/databridge/v1/Jobs/{job_id}")
            status = status_of(poll) if poll.is_success else f"HTTP {poll.status_code}"
            if status != last:
                say(f"job {job_id}: {status}")
                last = status
            word = status.strip().lower()
            if word in _SUCCESS_STATUSES or word in _FAILURE_STATUSES:
                result["job_status"] = status
                result["job_response"] = excerpt(poll)
                result["verdict"] = "SUCCEEDED" if word in _SUCCESS_STATUSES else "FAILED"
                break
            if time.monotonic() >= deadline:
                result["job_status"] = status
                result["verdict"] = "TIMED_OUT"
                break
            sleep(poll_seconds)

    result["finished_at"] = now().isoformat()
    return result

def _load_settings() -> Settings:
    load_dotenv(Path.cwd() / ".env", override=False)
    environment = os.environ.get("MIGRATION_HUB_ENV", "dev")
    config_dir = Path(os.environ.get("MIGRATION_HUB_CONFIG_DIR", "config"))
    return Settings.load(environment=environment, config_dir=config_dir)

_NEXT = {
    "IN_DATA_VAULT": "run `migration-hub retry --file-id {id}` -- it marks the file COMPLETED",
    "ON_DATA_BRIDGE": (
        "run `migration-hub retry --file-id {id}` -- it archives without re-uploading"
    ),
    "REFUSED": (
        "the import could not start without an upload -- `retry` (full re-upload) is the way"
    ),
    "SUCCEEDED": (
        "the .bak was still there. Run `migration-hub retry --file-id {id}` "
        "to archive it without re-uploading"
    ),
    "FAILED": (
        "the import failed again on the same upload -- read job_response; "
        "if it names the backup, re-uploading will fail the same way"
    ),
    "TIMED_OUT": "still running -- watch job {job} on the Data Bridge Jobs page",
}

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-import a failed file without uploading it again (TASK-0136)"
    )
    parser.add_argument(
        "--file-id", required=True, type=int, help="the FAILED or ABANDONED file's id"
    )
    parser.add_argument(
        "--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS, help="between job polls"
    )
    parser.add_argument(
        "--max-minutes", type=float, default=DEFAULT_MAX_MINUTES, help="stop polling after"
    )
    parser.add_argument(
        "--skip-error-check",
        action="store_true",
        help="do not require last_error to be the import job (state and location still checked)",
    )
    args = parser.parse_args()

    settings = _load_settings()
    engine = create_registry_engine(settings.database_url)
    file = Registry(engine).get(args.file_id)
    reason = refusal(file, check_error=not args.skip_error_check)
    if reason is not None:
        raise SystemExit(f"file {args.file_id}: {reason}")
    assert file is not None

    print(f"file {file.file_id}: {file.state}  {file.last_error}")
    adapter = DatabridgeAdapter(host=settings.api_host, api_key=settings.api_key(), engine=engine)
    try:
        result = reimport(adapter, file=file, group_ids=list(settings.databridge_group_ids))
    finally:
        adapter.close()

    saved = Path("results") / f"probe_reimport_{file.file_id}.json"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print()
    print(f"VERDICT: {result['verdict']}")
    print(f"saved {saved} -- send this file")
    follow_up = _NEXT[str(result["verdict"])]
    print("next: " + follow_up.format(id=file.file_id, job=result.get("job_id")))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
