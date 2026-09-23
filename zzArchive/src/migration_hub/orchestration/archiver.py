from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from migration_hub.adapters.databridge.client import DatabridgeAdapter, DatabridgeJobStatus
from migration_hub.core.models import MigrationFile
from migration_hub.core.registry import Registry
from migration_hub.core.retry import HaltError, RetryableError

RETENTION_YEARS = 5

def archive_pending(
    *,
    registry: Registry,
    adapter: DatabridgeAdapter,
    batch_id: str | None = None,
    resource_group_id: str | None = None,
    actor: str = "archiver",
    max_wait_minutes: float = 0.0,
    poll_interval_seconds: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    pending = registry.pending_archives(batch_id=batch_id)
    archived = 0
    for file in pending:
        try:
            done = _archive_one(
                registry=registry,
                adapter=adapter,
                file=file,
                resource_group_id=resource_group_id,
                actor=actor,
                max_wait_minutes=max_wait_minutes,
                poll_interval_seconds=poll_interval_seconds,
                sleep=sleep,
                clock=clock,
            )
        except HaltError:
            raise
        except Exception as exc:
            registry.record_identifiers(
                file_id=file.file_id,
                actor=actor,
                last_error=f"archive failed: {type(exc).__name__}: {exc}",
            )
            done = False
        if done:
            archived += 1
    return archived

def _archive_one(
    *,
    registry: Registry,
    adapter: DatabridgeAdapter,
    file: MigrationFile,
    resource_group_id: str | None,
    actor: str,
    max_wait_minutes: float,
    poll_interval_seconds: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
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

    deadline = clock() + max_wait_minutes * 60

    def _until[T](check: Callable[[], T | None]) -> T | None:
        return _poll_until(
            check, deadline=deadline, interval=poll_interval_seconds, sleep=sleep, clock=clock
        )

    def _terminal_status() -> DatabridgeJobStatus | None:
        with adapter.bound_to_file(file.file_id):
            polled = adapter.poll_job(job_id=job_id_to_poll)
        return polled if polled.is_terminal else None

    status = _until(_terminal_status)

    if status is None:
        if max_wait_minutes > 0:
            registry.record_identifiers(
                file_id=file.file_id,
                actor=actor,
                last_error=(
                    f"archive job {job_id_to_poll} did not finish within "
                    f"{max_wait_minutes:g} minutes"
                ),
            )
        return False

    if not status.is_success:
        registry.record_identifiers(
            file_id=file.file_id,
            actor=actor,
            last_error=f"archive job {job_id_to_poll} ended in {status.raw_status}",
        )
        return False

    database_name = file.database_name

    def _visible() -> bool | None:
        with adapter.bound_to_file(file.file_id):
            return True if adapter.archive_exists(database_name=database_name) else None

    exists = _until(_visible)

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

def _poll_until[T](
    check: Callable[[], T | None],
    *,
    deadline: float,
    interval: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> T | None:
    while True:
        try:
            result = check()
        except (httpx.TransportError, RetryableError):
            result = None
        if result is not None or clock() >= deadline:
            return result
        sleep(interval)

def _expiration_date(uploaded_at: datetime | None) -> str | None:
    if uploaded_at is None:
        return None
    return (uploaded_at + timedelta(days=365 * RETENTION_YEARS)).isoformat(timespec="milliseconds")
