from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from migration_hub.core.registry import Registry
from migration_hub.core.scrub import scrub_credentials, scrub_optional
from migration_hub.observability.redaction import redact_text

KNOWN_GAPS = (
    "Upload PUTs are recorded from TASK-0087 on (file, part, bytes, status, duration, "
    "ETag; URL without its signature); uploads made before that have no PUT rows.",
    "The per-file session check (resource-group lookup) uses its own client and is not recorded.",
    "Each developer's registry holds only what that developer migrated (TASK-0073).",
)

_FILE_COLUMNS = (
    "file_id",
    "batch_id",
    "source_database",
    "source_path",
    "target_exposure_name",
    "size_bytes",
    "checksum",
    "source_last_write",
    "state",
    "attempts",
    "archive_attempts",
    "instance_name",
    "database_name",
    "job_id",
    "archive_job_id",
    "uploaded_at",
    "archived_at",
    "archive_expiration_date",
    "last_error",
    "created_at",
    "updated_at",
)
_EVENT_COLUMNS = (
    "event_id",
    "file_id",
    "source_database",
    "occurred_at",
    "from_state",
    "to_state",
    "actor",
    "detail",
)
_CALL_COLUMNS = (
    "transaction_id",
    "file_id",
    "source_database",
    "occurred_at",
    "method",
    "url",
    "status_code",
    "duration_ms",
    "correlation_id",
    "request_body",
    "response_body",
)
_RUN_COLUMNS = (
    "run_id",
    "batch_id",
    "run_seq",
    "trigger",
    "initiated_by",
    "started_at",
    "finished_at",
    "status",
    "source_count",
    "source_bytes",
    "target_count",
    "target_bytes",
    "completed_count",
    "archive_failed_count",
    "bridge_count",
    "abandoned_count",
    "rejected_count",
    "skipped_count",
    "outstanding_count",
    "retry_count",
    "exception_count",
    "reconciled_at",
    "evidence_uri",
    "evidence_sha256",
    "signed_off_by",
    "signed_off_at",
    "notes",
)

class UnknownBatchError(ValueError):
    pass

@dataclass(frozen=True, slots=True)
class AuditExport:
    path: Path
    sha256: str
    files: int
    events: int
    api_calls: int
    runs: int

def export_batch_audit(
    *,
    registry: Registry,
    batch_id: str,
    output_path: Path,
    generated_by: str,
    environment: str | None = None,
) -> AuditExport:
    if registry.get_batch(batch_id) is None:
        raise UnknownBatchError(f"no batch {batch_id!r} in this registry")

    files = registry.batch_files(batch_id=batch_id)
    events = registry.batch_events(batch_id=batch_id)
    calls = registry.batch_transactions(batch_id=batch_id)
    runs = registry.list_batch_runs(batch_id=batch_id)
    names = {f.file_id: f.source_database for f in files}

    members = {
        "files.csv": _csv(
            _FILE_COLUMNS,
            (
                {**_row(f, _FILE_COLUMNS), "last_error": scrub_optional(f.last_error)}
                for f in files
            ),
        ),
        "events.csv": _csv(
            _EVENT_COLUMNS,
            (
                {
                    **_row(e, _EVENT_COLUMNS),
                    "source_database": names.get(e.file_id),
                    "detail": scrub_optional(e.detail),
                }
                for e in events
            ),
        ),
        "api_calls.csv": _csv(
            _CALL_COLUMNS,
            (
                {
                    **_row(c, _CALL_COLUMNS),
                    "source_database": names.get(c.file_id) if c.file_id is not None else None,
                    "url": scrub_credentials(c.url),
                    "request_body": _redacted(c.request_body),
                    "response_body": _redacted(c.response_body),
                }
                for c in calls
            ),
        ),
        "runs.csv": _csv(_RUN_COLUMNS, (_row(r, _RUN_COLUMNS) for r in runs)),
    }

    manifest = {
        "batch_id": batch_id,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "generated_by": generated_by,
        "environment": environment,
        "files_by_state": dict(sorted(Counter(f.state for f in files).items())),
        "row_counts": {
            "files.csv": len(files),
            "events.csv": len(events),
            "api_calls.csv": len(calls),
            "runs.csv": len(runs),
        },
        "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in members.items()},
        "known_gaps": list(KNOWN_GAPS),
    }
    members["manifest.json"] = json.dumps(manifest, indent=2).encode("utf-8")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)

    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    sidecar = output_path.with_name(output_path.name + ".sha256")
    sidecar.write_text(f"{digest}  {output_path.name}\n", encoding="utf-8")

    return AuditExport(
        path=output_path,
        sha256=digest,
        files=len(files),
        events=len(events),
        api_calls=len(calls),
        runs=len(runs),
    )

def _row(obj: object, columns: Sequence[str]) -> dict[str, object]:
    return {name: getattr(obj, name, None) for name in columns}

def _redacted(body: str | None) -> str | None:
    return None if body is None else redact_text(body)

def _csv(columns: Sequence[str], rows: Iterable[dict[str, object]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(columns))
    writer.writeheader()
    for row in rows:
        writer.writerow({k: "" if v is None else v for k, v in row.items()})
    return buffer.getvalue().encode("utf-8-sig")
