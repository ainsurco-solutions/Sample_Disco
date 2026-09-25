from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from migration_hub.adapters.databridge.client import DatabridgeAdapter, DatabridgeJobStatus
from migration_hub.core.models import MigrationFile
from migration_hub.core.registry import Registry
from migration_hub.core.retry import HaltError, RetryableError
from migration_hub.core.states import FileState
from migration_hub.observability.logging import Heartbeat, get_logger

RETENTION_YEARS = 5

def archive_pending(
    *,
    registry: Registry,
    adapter: DatabridgeAdapter,
    batch_id: str | None = None,
    resource_group_id: str | None = None,
    actor: str = "archiver",
    include_failed: bool = True,
    only: Callable[[MigrationFile], bool] | None = None,
    max_archive_attempts: int | None = None,
    max_wait_minutes: float = 0.0,
    poll_interval_seconds: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC).replace(tzinfo=None),
) -> int:
    pending = registry.pending_archives(batch_id=batch_id, include_failed=include_failed)
    archived = 0
    for file in pending:
        if only is not None and not only(file):
            continue
        if _archive_guarded(
            registry=registry,
            adapter=adapter,
            file=file,
            resource_group_id=resource_group_id,
            actor=actor,
            limit=max_archive_attempts,
            max_wait_minutes=max_wait_minutes,
            poll_interval_seconds=poll_interval_seconds,
            sleep=sleep,
            clock=clock,
            now=now,
        ):
            archived += 1
    return archived

def archive_one_file(
    *,
    registry: Registry,
    adapter: DatabridgeAdapter,
    file_id: int,
    resource_group_id: str | None,
    actor: str,
    max_archive_attempts: int | None = None,
    max_wait_minutes: float = 0.0,
    poll_interval_seconds: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC).replace(tzinfo=None),
) -> FileState:
    file = registry.get(file_id)
    if file is None:
        raise ValueError(f"No migration_file with file_id={file_id}")
    _archive_guarded(
        registry=registry,
        adapter=adapter,
        file=file,
        resource_group_id=resource_group_id,
        actor=actor,
        limit=max_archive_attempts,
        max_wait_minutes=max_wait_minutes,
        poll_interval_seconds=poll_interval_seconds,
        sleep=sleep,
        clock=clock,
        now=now,
    )
    final = registry.get(file_id)
    assert final is not None
    return FileState(final.state)

def _archive_guarded(
    *,
    registry: Registry,
    adapter: DatabridgeAdapter,
    file: MigrationFile,
    resource_group_id: str | None,
    actor: str,
    limit: int | None,
    max_wait_minutes: float,
    poll_interval_seconds: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    now: Callable[[], datetime],
) -> bool:
    try:
        return _archive_one(
            registry=registry,
            adapter=adapter,
            file=file,
            resource_group_id=resource_group_id,
            actor=actor,
            limit=limit,
            max_wait_minutes=max_wait_minutes,
            poll_interval_seconds=poll_interval_seconds,
            sleep=sleep,
            clock=clock,
            now=now,
        )
    except HaltError:
        raise
    except Exception as exc:
        reason = f"archive failed: {type(exc).__name__}: {exc}"
        _fail(registry, file.file_id, actor, reason, limit=limit)
        return False

def _archive_one(
    *,
    registry: Registry,
    adapter: DatabridgeAdapter,
    file: MigrationFile,
    resource_group_id: str | None,
    actor: str,
    limit: int | None,
    max_wait_minutes: float,
    poll_interval_seconds: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    now: Callable[[], datetime],
) -> bool:
    assert file.instance_name is not None
    assert file.database_name is not None
    instance_name = file.instance_name
    database_name = file.database_name
    deadline = clock() + max_wait_minutes * 60

    def _until[T](check: Callable[[], T | None], waiting_for: str) -> T | None:
        heartbeat = Heartbeat(get_logger(__name__, file_id=file.file_id), clock=clock)

        def check_and_beat() -> T | None:
            result = check()
            if result is None:
                heartbeat.beat(
                    lambda minutes: (
                        f"still waiting for {waiting_for}, {minutes:.0f} min elapsed of "
                        f"{max_wait_minutes:.0f}, checking every {poll_interval_seconds:.0f} s"
                    )
                )
            return result

        return _poll_until(
            check_and_beat,
            deadline=deadline,
            interval=poll_interval_seconds,
            sleep=sleep,
            clock=clock,
        )

    def _terminal(job_id: str) -> DatabridgeJobStatus | None:
        def check() -> DatabridgeJobStatus | None:
            with adapter.bound_to_file(file.file_id):
                polled = adapter.poll_job(job_id=job_id)
            return polled if polled.is_terminal else None

        return _until(check, f"archive job {job_id}")

    def _in_vault() -> bool:
        with adapter.bound_to_file(file.file_id):
            return adapter.archive_exists(database_name=database_name)

    retrying = file.state != str(FileState.BRIDGED)

    if (
        limit is not None
        and file.state == str(FileState.ARCHIVE_FAILED)
        and file.archive_attempts >= limit
    ):
        registry.transition(
            file_id=file.file_id,
            to_state=FileState.ABANDONED,
            actor=actor,
            detail=(
                f"archive attempts exhausted ({file.archive_attempts}/{limit}): "
                f"{file.last_error or 'no reason recorded'}"
            ),
        )
        return False

    if file.state != str(FileState.ARCHIVING):
        registry.transition(
            file_id=file.file_id,
            to_state=FileState.ARCHIVING,
            actor=actor,
            archive_attempts=file.archive_attempts + 1,
        )

    job_id = file.archive_job_id
    job_succeeded = False
    if job_id is not None:
        status = _terminal(job_id)
        if status is None:
            return _timed_out(registry, file.file_id, actor, job_id, max_wait_minutes, limit)
        job_succeeded = status.is_success

    if not job_succeeded:
        if retrying and _in_vault():
            return _complete(registry, file.file_id, actor)

        expiration_date = _expiration_date(file.uploaded_at, now=now())
        with adapter.bound_to_file(file.file_id):
            job_id = adapter.archive_database(
                instance_name=instance_name,
                database_name=database_name,
                resource_group_id=resource_group_id,
                expiration_date=expiration_date,
            )
        registry.record_identifiers(
            file_id=file.file_id,
            actor=actor,
            archive_job_id=job_id,
            archive_expiration_date=datetime.fromisoformat(expiration_date),
        )
        status = _terminal(job_id)
        if status is None:
            return _timed_out(registry, file.file_id, actor, job_id, max_wait_minutes, limit)
        if not status.is_success:
            reason = f"archive job {job_id} ended in {status.raw_status}"
            _fail(registry, file.file_id, actor, reason, limit=limit)
            return False

    if not _until(lambda: True if _in_vault() else None, f"{database_name} in Data Vault"):
        _fail(
            registry,
            file.file_id,
            actor,
            f"archive job {job_id} succeeded but {database_name!r} is not visible in "
            "searchArchives",
            limit=limit,
        )
        return False

    return _complete(registry, file.file_id, actor)

def _complete(registry: Registry, file_id: int, actor: str) -> bool:
    registry.transition(
        file_id=file_id,
        to_state=FileState.COMPLETED,
        actor=actor,
        archived_at=datetime.now(UTC).replace(tzinfo=None),
    )
    return True

def _timed_out(
    registry: Registry,
    file_id: int,
    actor: str,
    job_id: str,
    max_wait_minutes: float,
    limit: int | None,
) -> bool:
    _fail(
        registry,
        file_id,
        actor,
        f"archive job {job_id} did not finish within {max_wait_minutes:g} minutes",
        limit=limit,
    )
    return False

def _fail(registry: Registry, file_id: int, actor: str, reason: str, *, limit: int | None) -> None:
    current = registry.get(file_id)
    if current is None or current.state != str(FileState.ARCHIVING):
        registry.record_identifiers(file_id=file_id, actor=actor, last_error=reason)
        return
    registry.transition(
        file_id=file_id, to_state=FileState.ARCHIVE_FAILED, actor=actor, detail=reason
    )
    if limit is not None and current.archive_attempts >= limit:
        registry.transition(
            file_id=file_id,
            to_state=FileState.ABANDONED,
            actor=actor,
            detail=f"archive attempts exhausted ({current.archive_attempts}/{limit}): {reason}",
        )

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

def _expiration_date(uploaded_at: datetime | None, *, now: datetime) -> str:
    anchor = uploaded_at if uploaded_at is not None else now
    return (anchor + timedelta(days=365 * RETENTION_YEARS)).isoformat(timespec="milliseconds")
