from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.orm import Session

from migration_hub.adapters.databridge.client import DatabridgeAdapter
from migration_hub.config.settings import Settings
from migration_hub.core.engine import create_registry_engine
from migration_hub.core.models import MigrationFile

ACTIVE_STATUSES = frozenset(
    {"queued", "pending", "running", "enqueued", "processing", "inprogress"}
)

_LIST_KEYS = ("items", "jobs", "data", "searchItems", "results", "value")

_ID_KEYS = ("jobId", "id", "JobId", "Id")
_COUNTED_FIELDS = ("status", "type", "platformOperationType", "entitlement", "jobType", "Status")

_PAUSE_SECONDS = 1.0

@dataclass
class ProbeResult:
    candidate: str
    path: str
    documented: bool
    status_code: int | None = None
    shape: str = ""
    items: int = 0
    known_ids_matched: int = 0
    active_items: int = 0
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    item_fields: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    response_bytes: int = 0
    params: dict[str, str] = field(default_factory=dict)
    verdict: str = ""
    note: str = ""

def items_of(body: object) -> list[dict[str, object]] | None:
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        for key in _LIST_KEYS:
            value = body.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return None

def shape_of(body: object) -> str:
    if isinstance(body, list):
        return f"list[{len(body)}]"
    if isinstance(body, dict):
        return "object{" + ", ".join(sorted(body)[:12]) + "}"
    if isinstance(body, str):
        return f"text({len(body)} chars)"
    return type(body).__name__

def summarise(result: ProbeResult, body: object, known_ids: set[str]) -> None:
    result.shape = shape_of(body)
    jobs = items_of(body)
    if jobs is None:
        return
    result.items = len(jobs)
    result.item_fields = sorted({key for job in jobs[:50] for key in job})
    counts: dict[str, Counter[str]] = {name: Counter() for name in _COUNTED_FIELDS}
    for job in jobs:
        job_id = next((str(job[k]) for k in _ID_KEYS if job.get(k) is not None), None)
        if job_id is not None and job_id.lower() in known_ids:
            result.known_ids_matched += 1
        for name in _COUNTED_FIELDS:
            value = job.get(name)
            if isinstance(value, str | int):
                counts[name][str(value)] += 1
        status = str(job.get("status") or job.get("Status") or "").lower()
        if status in ACTIVE_STATUSES:
            result.active_items += 1
    result.counts = {name: dict(c) for name, c in counts.items() if c}

def classify(result: ProbeResult) -> str:
    code = result.status_code
    if code is None:
        return "Unknown -- no response"
    if code in (401, 403):
        return "Forbidden -- exists, but this API key may not list it"
    if code in (404, 405):
        return "Not available -- no such endpoint on this tenant"
    if code >= 400:
        return f"Unknown -- HTTP {code}"
    if result.items == 0:
        return "Unknown -- answered, but no job list to compare"
    if result.known_ids_matched:
        return "Relevant -- returns this Hub's Data Bridge jobs"
    return "Adjacent or wrong family -- lists jobs, none of them ours"

FILTER_VARIANTS: tuple[dict[str, str], ...] = (
    {"status": "Processing"},
    {"status": "Done"},
    {"$filter": "status eq 'Processing'"},
    {"filter": 'status="PROCESSING"'},
    {"$top": "5"},
    {"limit": "5"},
    {"pageSize": "5"},
    {"take": "5"},
)

def filter_verdict(result: ProbeResult, baseline_items: int) -> str:
    code = result.status_code
    if code is None:
        return "Unknown -- no response"
    if code >= 400:
        return f"Rejected -- HTTP {code}"
    if result.items < baseline_items:
        return f"Honoured -- {result.items} of {baseline_items}"
    return "Ignored -- same count as the baseline"

def _known_job_ids(database_url: str) -> set[str]:
    engine = create_registry_engine(database_url)
    with Session(engine) as session:
        rows = session.execute(select(MigrationFile.job_id, MigrationFile.archive_job_id)).all()
    return {str(v).lower() for row in rows for v in row if v}

def _settings() -> Settings:
    load_dotenv(Path.cwd() / ".env", override=False)
    environment = os.environ.get("MIGRATION_HUB_ENV", "dev")
    config_dir = Path(os.environ.get("MIGRATION_HUB_CONFIG_DIR", "config"))
    return Settings.load(environment=environment, config_dir=config_dir)

def _instances(settings: Settings) -> list[str]:
    names = list(dict.fromkeys(settings.databridge_instances.values()))
    if settings.databridge_instance_name and settings.databridge_instance_name not in names:
        names.append(settings.databridge_instance_name)
    return names

def _filter_candidates(
    settings: Settings,
) -> list[tuple[str, str, bool, dict[str, str] | None]]:
    instances = _instances(settings)
    out: list[tuple[str, str, bool, dict[str, str] | None]] = [
        (f"{name} baseline", f"/databridge/v1/sql-instances/{name}/Jobs", False, None)
        for name in instances
    ]
    if instances:
        path = f"/databridge/v1/sql-instances/{instances[0]}/Jobs"
        out += [(f"{instances[0]} ?{next(iter(v))}", path, False, v) for v in FILTER_VARIANTS]
    return out

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Which Moody's API lists this Hub's jobs? A read-only probe (TASK-0094)."
    )
    parser.add_argument(
        "--filters",
        action="store_true",
        help="try filter/page-size parameters on the instance jobs list",
    )
    args = parser.parse_args()

    settings = _settings()
    known_ids = _known_job_ids(settings.database_url)
    print(f"registry holds {len(known_ids)} known Data Bridge job id(s)")
    if not known_ids:
        print("  [!!] nothing to match against -- run at least one real import first")

    instance = settings.databridge_instance_name or next(
        iter(settings.databridge_instances.values()), None
    )
    candidates: list[tuple[str, str, bool, dict[str, str] | None]] = [
        (
            "platform tenant jobs",
            "/platform/tenantdata/v1/jobs",
            True,
            {"limit": "100", "sort": "submittedAt DESC", "includeArchivedJobs": "true"},
        ),
        ("platform admin-data jobs", "/platform/admindata/v1/jobs", True, {"limit": "100"}),
        ("databridge jobs list (undocumented)", "/databridge/v1/Jobs", False, None),
    ]
    if instance:
        candidates.append(
            (
                "databridge instance jobs (undocumented)",
                f"/databridge/v1/sql-instances/{instance}/Jobs",
                False,
                None,
            )
        )
    if args.filters:
        candidates = _filter_candidates(settings)
    elif known_ids:
        candidates.append(
            (
                "databridge single job (control)",
                f"/databridge/v1/Jobs/{sorted(known_ids)[0]}",
                True,
                None,
            )
        )

    adapter = DatabridgeAdapter(
        host=settings.api_host,
        api_key=settings.api_key(),
        engine=create_registry_engine(settings.database_url),
    )
    results: list[ProbeResult] = []
    try:
        for i, (name, path, documented, params) in enumerate(candidates):
            if i:
                time.sleep(_PAUSE_SECONDS)
            result = ProbeResult(
                candidate=name, path=path, documented=documented, params=dict(params or {})
            )
            try:
                started = time.monotonic()
                response = adapter.probe_get(path, params=params)
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
                result.response_bytes = len(response.content)
                result.status_code = response.status_code
                try:
                    body: object = response.json()
                except ValueError:
                    body = response.text
                summarise(result, body, known_ids)
            except Exception as exc:
                result.note = type(exc).__name__
            result.verdict = classify(result)
            if args.filters and result.params:
                baseline = next(
                    (r.items for r in results if not r.params and r.path == result.path), 0
                )
                result.verdict = filter_verdict(result, baseline)
            results.append(result)
    finally:
        adapter.close()

    print()
    for r in results:
        print(f"{r.candidate}  ({'documented' if r.documented else 'undocumented'})")
        query = "?" + "&".join(f"{k}={v}" for k, v in r.params.items()) if r.params else ""
        print(f"    GET {r.path}{query}  ->  {r.status_code}  {r.shape}")
        print(f"    {r.elapsed_ms} ms, {r.response_bytes:,} bytes")
        print(f"    items {r.items}, ours matched {r.known_ids_matched}, active {r.active_items}")
        for fname, values in r.counts.items():
            print(f"    {fname}: {values}")
        if r.item_fields:
            print(f"    fields: {', '.join(r.item_fields)}")
        print(f"    VERDICT: {r.verdict}{'  (' + r.note + ')' if r.note else ''}")
        print()

    out_dir = Path("results")
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"probe_job_queue_{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(
        json.dumps(
            {"known_job_ids": len(known_ids), "results": [asdict(r) for r in results]},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"saved {out} -- send this file (it holds no response bodies or ids)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
