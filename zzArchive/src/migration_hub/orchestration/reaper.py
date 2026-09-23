from __future__ import annotations

from migration_hub.adapters.databridge.client import DatabridgeAdapter
from migration_hub.core.registry import Registry
from migration_hub.core.states import FileState

def reap_stale_claims(
    *,
    registry: Registry,
    adapter: DatabridgeAdapter,
    timeout_minutes: int,
    max_attempts: int,
    actor: str = "reaper",
) -> int:
    stale = registry.stale_claims(timeout_minutes=timeout_minutes)
    if not stale:
        return 0

    resolved = 0
    for file in stale:
        if FileState(file.state) is FileState.IMPORTING and file.job_id:
            if reconcile_importing(
                registry=registry,
                adapter=adapter,
                file_id=file.file_id,
                job_id=file.job_id,
                instance_name=file.instance_name,
                database_name=file.database_name,
                max_attempts=max_attempts,
                actor=actor,
            ):
                resolved += 1
            continue

        registry.record_failure(
            file_id=file.file_id,
            error=f"stale claim reaped (claimed_by={file.claimed_by!r}, "
            f"older than {timeout_minutes}m)",
            retryable=True,
            max_attempts=max_attempts,
        )
        resolved += 1

    return resolved

def reconcile_importing(
    *,
    registry: Registry,
    adapter: DatabridgeAdapter,
    file_id: int,
    job_id: str,
    instance_name: str | None,
    database_name: str | None,
    max_attempts: int,
    actor: str,
) -> bool:
    with adapter.bound_to_file(file_id):
        status = adapter.poll_job(job_id=job_id)

    if not status.is_terminal:
        return False

    if not status.is_success:
        registry.record_failure(
            file_id=file_id,
            error=f"reconciled: job {job_id} ended in {status.raw_status}",
            retryable=True,
            max_attempts=max_attempts,
        )
        return True

    registry.transition(
        file_id=file_id,
        to_state=FileState.VERIFYING,
        actor=actor,
        detail=f"reconciled: job {job_id} finished while its worker was down",
    )

    with adapter.bound_to_file(file_id):
        exists = adapter.database_exists(instance_name=instance_name, database_name=database_name)

    if exists:
        registry.transition(file_id=file_id, to_state=FileState.BRIDGED, actor=actor)
    else:
        registry.record_failure(
            file_id=file_id,
            error=f"reconciled job {job_id} finished but {database_name!r} "
            "is not visible on the platform",
            retryable=True,
            max_attempts=max_attempts,
        )
    return True
