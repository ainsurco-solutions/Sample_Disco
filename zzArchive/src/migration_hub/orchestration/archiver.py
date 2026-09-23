from __future__ import annotations

from datetime import UTC, datetime, timedelta

from migration_hub.adapters.databridge.client import DatabridgeAdapter
from migration_hub.core.models import MigrationFile
from migration_hub.core.registry import Registry

RETENTION_YEARS = 5

def archive_pending(
    *,
    registry: Registry,
    adapter: DatabridgeAdapter,
    batch_id: str | None = None,
    resource_group_id: str | None = None,
    actor: str = "archiver",
) -> int:
    pending = registry.pending_archives(batch_id=batch_id)
    archived = 0
    for file in pending:
        if _archive_one(
            registry=registry, adapter=adapter, file=file,
            resource_group_id=resource_group_id, actor=actor,
        ):
            archived += 1
    return archived

def _archive_one(
    *,
    registry: Registry,
    adapter: DatabridgeAdapter,
    file: MigrationFile,
    resource_group_id: str | None,
    actor: str,
) -> bool:
    assert file.instance_name is not None
    assert file.database_name is not None

    if file.archive_job_id is None:
        expiration_date = _expiration_date(file.uploaded_at)
        with adapter.bound_to_file(file.file_id):
            job_id = adapter.archive_database(
                instance_name=file.instance_name,
                database_name=file.database_name,
                resource_group_id=resource_group_id,
                expiration_date=expiration_date,
            )
        registry.record_identifiers(
            file_id=file.file_id,
            actor=actor,
            archive_job_id=job_id,
            archive_expiration_date=(
                datetime.fromisoformat(expiration_date) if expiration_date else None
            ),
        )
        job_id_to_poll = job_id
    else:
        job_id_to_poll = file.archive_job_id

    with adapter.bound_to_file(file.file_id):
        status = adapter.poll_job(job_id=job_id_to_poll)

    if not status.is_terminal:
        return False

    if not status.is_success:
        registry.record_identifiers(
            file_id=file.file_id,
            actor=actor,
            last_error=f"archive job {job_id_to_poll} ended in {status.raw_status}",
        )
        return False

    with adapter.bound_to_file(file.file_id):
        exists = adapter.archive_exists(database_name=file.database_name)

    if not exists:
        registry.record_identifiers(
            file_id=file.file_id,
            actor=actor,
            last_error=(
                f"archive job {job_id_to_poll} succeeded but {file.database_name!r} "
                "is not visible in searchArchives yet"
            ),
        )
        return False

    registry.record_identifiers(
        file_id=file.file_id,
        actor=actor,
        archived_at=datetime.now(UTC).replace(tzinfo=None),
    )
    return True

def _expiration_date(uploaded_at: datetime | None) -> str | None:
    if uploaded_at is None:
        return None
    return (uploaded_at + timedelta(days=365 * RETENTION_YEARS)).isoformat(timespec="milliseconds")
