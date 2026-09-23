from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from migration_hub.adapters.databridge.client import DatabridgeAdapter
from migration_hub.core.registry import Registry
from migration_hub.core.states import FileState
from migration_hub.observability import controls
from migration_hub.orchestration import archiver
from migration_hub.orchestration.batch_identity import derive_batch_id
from migration_hub.orchestration.worker_pool import run_worker_pool
from migration_hub.producers import scanner, validator

AUTO_SIGN_OFF_ACTOR = "migration-hub-auto"

@dataclass(frozen=True, slots=True)
class AutoMigrationResult:
    batch_id: str
    outcomes: dict[FileState, int]
    status: str
    signed_off: bool
    signed_off_by: str | None
    run_id: str

def run_automated_migration(
    *,
    registry: Registry,
    adapter_factory: Callable[[], DatabridgeAdapter],
    source_root: Path,
    pattern: str,
    max_files: int | None,
    thread_count: int,
    dry_run: bool,
    max_attempts: int,
    poll_interval_seconds: float,
    max_poll_minutes: float,
    instance_name: str | None,
    instances: dict[str, str] | None = None,
    compute_checksum: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> AutoMigrationResult:
    def _log(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    batch_id = derive_batch_id(source_root)
    _log(f"batch: {batch_id!r} (derived from {source_root})")

    registry.ensure_batch(batch_id=batch_id, max_concurrency=thread_count)

    _log("discovering...")
    registered = scanner.register(
        registry=registry, source_root=source_root, batch_id=batch_id,
        pattern=pattern, max_files=max_files, max_concurrency=thread_count,
    )
    _log(f"registered {registered} new file(s)")

    _log("validating...")
    validation_results = validator.validate_batch(
        registry=registry, batch_id=batch_id, compute_checksum=compute_checksum,
    )
    rejected = sum(1 for r in validation_results.values() if not r.ok)
    _log(f"validated {len(validation_results)} file(s), {rejected} rejected")

    outcomes: dict[FileState, int] = {}
    while True:
        _log("migrating (threaded)...")
        pass_outcomes = run_worker_pool(
            registry=registry, adapter_factory=adapter_factory, batch_id=batch_id,
            thread_count=thread_count, dry_run=dry_run, max_attempts=max_attempts,
            poll_interval_seconds=poll_interval_seconds, max_poll_minutes=max_poll_minutes,
            instance_name=instance_name,
            instances=instances,
        )
        for state, count in pass_outcomes.items():
            outcomes[state] = outcomes.get(state, 0) + count

        failed_now = [
            f for f in registry.files_in_states(states=[FileState.FAILED], batch_id=batch_id)
        ]
        if not failed_now:
            break
        _log(f"requeuing {len(failed_now)} FAILED file(s) for another pass...")
        for file in failed_now:
            registry.transition(
                file_id=file.file_id, to_state=FileState.VALIDATED, actor="auto-retry",
            )

    if dry_run:
        _log("dry run -- skipping archive/close/sign-off")
        return AutoMigrationResult(
            batch_id=batch_id, outcomes=outcomes, status="RUNNING",
            signed_off=False, signed_off_by=None, run_id="",
        )

    _log("archiving...")
    coordinator = adapter_factory()
    try:
        session = coordinator.open_session()
        archived = archiver.archive_pending(
            registry=registry, adapter=coordinator, batch_id=batch_id,
            resource_group_id=session.resource_group_id,
            max_wait_minutes=max_poll_minutes, poll_interval_seconds=poll_interval_seconds,
        )
        _log(f"archived {archived} file(s)")
        not_archived = registry.pending_archives(batch_id=batch_id)
        if not_archived:
            _log(
                f"{len(not_archived)} file(s) not confirmed in Data Vault -- "
                "the run cannot close CLEAN; see each file's last_error"
            )

        _log("closing the control record...")
        record = controls.open_run(
            registry=registry, batch_id=batch_id,
            trigger=controls.RunTrigger.RUN, initiated_by=AUTO_SIGN_OFF_ACTOR,
        )
        confirmed_count, confirmed_bytes = _reconcile(registry=registry, adapter=coordinator, batch_id=batch_id)
        closed = controls.close(
            registry=registry, run_id=record.run_id,
            reconciled_target_count=confirmed_count, reconciled_target_bytes=confirmed_bytes,
        )
    finally:
        coordinator.close()

    signed_off = False
    if closed.status is controls.RunStatus.CLEAN:
        _log("run is CLEAN -- signing off automatically")
        controls.sign_off(registry=registry, run_id=closed.run_id, by=AUTO_SIGN_OFF_ACTOR)
        signed_off = True
    else:
        _log(f"run is {closed.status.value} -- needs a human to review and sign off")

    return AutoMigrationResult(
        batch_id=batch_id, outcomes=outcomes, status=closed.status.value,
        signed_off=signed_off, signed_off_by=(AUTO_SIGN_OFF_ACTOR if signed_off else None),
        run_id=str(closed.run_id),
    )

def _reconcile(*, registry: Registry, adapter: DatabridgeAdapter, batch_id: str) -> tuple[int, int]:
    from migration_hub.orchestration.reconciliation import reconcile_target_count
    return reconcile_target_count(
        registry=registry, adapter=adapter, batch_id=batch_id, require_archived=True
    )
