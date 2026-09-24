from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from migration_hub.adapters.databridge.client import DatabridgeAdapter
from migration_hub.core.models import MigrationFile
from migration_hub.core.registry import Registry
from migration_hub.core.states import BatchState, FileState
from migration_hub.observability import controls
from migration_hub.orchestration import archiver
from migration_hub.orchestration.batch_identity import derive_batch_id
from migration_hub.orchestration.batches import place_by_location, platform_locator
from migration_hub.orchestration.reconciliation import reconcile_targets
from migration_hub.orchestration.worker_pool import run_worker_pool
from migration_hub.producers import scanner, validator

class SourceFolderError(ValueError):
    pass

_STRIPPED_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[^\\/]")

def check_source_folder(source_root: Path) -> None:
    if source_root.is_dir():
        return
    message = (
        f'source path is not a folder: "{source_root}"'
        if source_root.exists()
        else f'source folder does not exist: "{source_root}"'
    )
    if _STRIPPED_WINDOWS_PATH.match(str(source_root)):
        message += (
            " -- it looks like the shell removed its backslashes (Git Bash does). "
            'Use forward slashes, e.g. "C:/MigrationHub/src", or run from PowerShell.'
        )
    raise SourceFolderError(message)

AUTO_SIGN_OFF_ACTOR = "migration-hub-auto"

def _is_paused(registry: Registry, batch_id: str) -> bool:
    batch = registry.get_batch(batch_id)
    return batch is not None and batch.state == str(BatchState.PAUSED)

def mark_running(registry: Registry, batch_id: str) -> None:
    batch = registry.get_batch(batch_id)
    if batch is not None and batch.state == str(BatchState.PLANNED):
        registry.set_batch_state(batch_id=batch_id, state=BatchState.RUNNING)

@dataclass(frozen=True, slots=True)
class AutoMigrationResult:
    batch_id: str
    outcomes: dict[FileState, int]
    status: str
    signed_off: bool
    signed_off_by: str | None
    run_id: str
    passes: int = 1

def run_automated_migration(
    *,
    registry: Registry,
    adapter_factory: Callable[[], DatabridgeAdapter],
    source_root: Path | None = None,
    batch_id: str | None = None,
    pattern: str = "*.bak",
    max_files: int | None,
    thread_count: int,
    dry_run: bool,
    max_attempts: int,
    poll_interval_seconds: float,
    max_poll_minutes: float,
    instance_name: str | None,
    instances: dict[str, str] | None = None,
    group_ids: list[str] | None = None,
    max_archive_attempts: int | None = None,
    compute_checksum: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> AutoMigrationResult:

    def _log(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    if (source_root is None) == (batch_id is None):
        raise ValueError("specify exactly one of source_root or batch_id")

    if source_root is not None:
        check_source_folder(source_root)
        batch_id = derive_batch_id(source_root)
        _log(f"batch: {batch_id!r} (derived from {source_root})")
        registry.ensure_batch(batch_id=batch_id, max_concurrency=thread_count)

        _log("discovering...")
        registered = scanner.register(
            registry=registry, source_root=source_root, batch_id=batch_id,
            pattern=pattern, max_files=max_files, max_concurrency=thread_count,
        )
        _log(f"registered {registered} new file(s)")
    else:
        assert batch_id is not None
        if registry.get_batch(batch_id) is None:
            raise ValueError(f"no batch {batch_id!r} in this registry")
        _log(f"batch: {batch_id!r} (existing -- no discovery)")

    if not dry_run:
        mark_running(registry, batch_id)

    _log("validating...")
    validation_results = validator.validate_batch(
        registry=registry, batch_id=batch_id, compute_checksum=compute_checksum,
    )
    rejected = sum(1 for r in validation_results.values() if not r.ok)
    _log(f"validated {len(validation_results)} file(s), {rejected} rejected")

    attempts_at_start = {
        f.file_id: f.archive_attempts for f in registry.batch_files(batch_id=batch_id)
    }

    passes = 0
    while True:
        passes += 1
        _log(
            "migrating (threaded; each worker archives its file once it is on Data Bridge)..."
            if not dry_run
            else "migrating (threaded)..."
        )
        run_worker_pool(
            registry=registry, adapter_factory=adapter_factory, batch_id=batch_id,
            thread_count=thread_count, dry_run=dry_run, max_attempts=max_attempts,
            poll_interval_seconds=poll_interval_seconds, max_poll_minutes=max_poll_minutes,
            instance_name=instance_name,
            instances=instances,
            group_ids=group_ids,
            archive_after_bridge=not dry_run,
            max_archive_attempts=max_archive_attempts,
        )

        if _is_paused(registry, batch_id):
            break
        failed_now = list(registry.files_in_states(states=[FileState.FAILED], batch_id=batch_id))
        if not failed_now:
            break
        _requeue_failed_checked(
            registry=registry, failed=failed_now, adapter_factory=adapter_factory,
            dry_run=dry_run, log=_log,
        )

    if _is_paused(registry, batch_id):
        _log(
            "batch is PAUSED -- stopped after the files in hand; nothing closed. "
            "Resume continues from here."
        )
        return AutoMigrationResult(
            batch_id=batch_id, outcomes=_final_states(registry, batch_id), status="PAUSED",
            signed_off=False, signed_off_by=None, run_id="", passes=passes,
        )

    if dry_run:
        _log("dry run -- skipping archive/close/sign-off")
        return AutoMigrationResult(
            batch_id=batch_id, outcomes=_final_states(registry, batch_id), status="RUNNING",
            signed_off=False, signed_off_by=None, run_id="", passes=passes,
        )

    coordinator = adapter_factory()
    try:
        session = coordinator.open_session()
        def under_run_limit(file: MigrationFile) -> bool:
            used = file.archive_attempts - attempts_at_start.get(file.file_id, 0)
            return used < max(1, max_attempts)

        archived = sum(
            1
            for f in registry.completed_files(batch_id=batch_id)
            if f.archive_attempts > attempts_at_start.get(f.file_id, 0)
        )
        if [f for f in registry.pending_archives(batch_id=batch_id) if under_run_limit(f)]:
            _log("archiving what is left (retrying failed archives)...")
        for archive_pass in range(1, max(1, max_attempts) + 1):
            if not [f for f in registry.pending_archives(batch_id=batch_id) if under_run_limit(f)]:
                break
            archived += archiver.archive_pending(
                registry=registry, adapter=coordinator, batch_id=batch_id,
                resource_group_id=session.resource_group_id, only=under_run_limit,
                max_wait_minutes=max_poll_minutes, poll_interval_seconds=poll_interval_seconds,
                max_archive_attempts=max_archive_attempts,
            )
            still_failed = registry.files_in_states(
                states=[FileState.ARCHIVE_FAILED], batch_id=batch_id
            )
            if not still_failed:
                break
            if archive_pass < max_attempts:
                _log(f"retrying the archive for {len(still_failed)} ARCHIVE_FAILED file(s)...")
        _log(f"archived {archived} file(s)")
        still_failed = registry.files_in_states(
            states=[FileState.ARCHIVE_FAILED], batch_id=batch_id
        )
        if still_failed:
            _log(
                f"{len(still_failed)} file(s) ARCHIVE_FAILED -- on Data Bridge, not in Data "
                "Vault; the run cannot close CLEAN. The next run of this folder retries "
                "their archive; see each file's last_error"
            )

        _log("closing the control record...")
        record = controls.open_run(
            registry=registry, batch_id=batch_id,
            trigger=controls.RunTrigger.RUN, initiated_by=AUTO_SIGN_OFF_ACTOR,
        )
        targets = reconcile_targets(registry=registry, adapter=coordinator, batch_id=batch_id)
        if targets.lost_file_ids:
            _log(
                f"LOST: file(s) {targets.lost_file_ids} are ARCHIVE_FAILED but on neither "
                "Data Bridge nor Data Vault -- investigate before retrying"
            )
        closed = controls.close(
            registry=registry, run_id=record.run_id,
            reconciled_target_count=targets.target_count,
            reconciled_target_bytes=targets.target_bytes,
            reconciled_bridge_count=targets.bridge_count,
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
        batch_id=batch_id, outcomes=_final_states(registry, batch_id), status=closed.status.value,
        signed_off=signed_off, signed_off_by=(AUTO_SIGN_OFF_ACTOR if signed_off else None),
        run_id=str(closed.run_id), passes=passes,
    )

def _requeue_failed_checked(
    *,
    registry: Registry,
    failed: list[MigrationFile],
    adapter_factory: Callable[[], DatabridgeAdapter],
    dry_run: bool,
    log: Callable[[str], None],
) -> None:
    if dry_run:
        for file in failed:
            registry.transition(
                file_id=file.file_id, to_state=FileState.VALIDATED, actor="auto-retry"
            )
        return

    placed: dict[FileState, int] = {}
    adapter = adapter_factory()
    try:
        locate = platform_locator(adapter)
        for file in failed:
            try:
                location = locate(file)
            except Exception as exc:
                registry.transition(
                    file_id=file.file_id,
                    to_state=FileState.VALIDATED,
                    actor="auto-retry",
                    detail=(
                        f"platform check failed ({type(exc).__name__}: {exc}); "
                        "requeued unchecked"
                    ),
                )
                placed[FileState.VALIDATED] = placed.get(FileState.VALIDATED, 0) + 1
                continue
            state = place_by_location(
                registry,
                file_id=file.file_id,
                location=location,
                actor="auto-retry",
                detail="auto-retry",
                reset_attempts=False,
            )
            placed[state] = placed.get(state, 0) + 1
    finally:
        adapter.close()

    log(
        f"{len(failed)} FAILED file(s): "
        f"{placed.get(FileState.COMPLETED, 0)} already in Data Vault, "
        f"{placed.get(FileState.BRIDGED, 0)} already on Data Bridge (archive only), "
        f"{placed.get(FileState.VALIDATED, 0)} requeued for another pass"
    )

def _final_states(registry: Registry, batch_id: str) -> dict[FileState, int]:
    return {
        state: count
        for state, count in registry.counts_by_state(batch_id=batch_id).items()
        if count
    }
