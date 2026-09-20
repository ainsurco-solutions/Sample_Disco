from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from migration_hub.adapters.base import MigrationTargetAdapter
from migration_hub.consumers.worker import MigrationWorker
from migration_hub.core.models import MigrationFile
from migration_hub.core.registry import Registry
from migration_hub.core.states import BatchState, FileState
from migration_hub.observability import controls

_WINDOWS_DETACH_FLAGS = (
    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    if sys.platform == "win32"
    else 0
)

def create_batch(
    *,
    registry: Registry,
    batch_id: str,
    description: str | None = None,
    max_concurrency: int = 1,
) -> None:
    registry.ensure_batch(
        batch_id=batch_id, description=description, max_concurrency=max_concurrency
    )

def assign_files(*, registry: Registry, batch_id: str, file_ids: Sequence[int]) -> int:
    raise NotImplementedError

def pause(*, registry: Registry, batch_id: str) -> None:
    registry.set_batch_state(batch_id=batch_id, state=BatchState.PAUSED)

def resume(*, registry: Registry, batch_id: str) -> None:
    registry.set_batch_state(batch_id=batch_id, state=BatchState.RUNNING)

def run_worker_loop(
    *,
    registry: Registry,
    adapter: MigrationTargetAdapter,
    batch_id: str,
    worker_id: str,
    dry_run: bool,
    max_attempts: int,
    poll_interval_seconds: float,
    max_poll_minutes: float,
    max_files: int | None = None,
    on_outcome: Callable[[FileState, int], None] | None = None,
) -> dict[FileState, int]:
    worker = MigrationWorker(
        registry=registry,
        adapter=adapter,
        worker_id=worker_id,
        dry_run=dry_run,
        max_attempts=max_attempts,
        poll_interval_seconds=poll_interval_seconds,
        max_poll_minutes=max_poll_minutes,
    )

    outcomes: dict[FileState, int] = {}
    while max_files is None or sum(outcomes.values()) < max_files:
        if state_of(registry=registry, batch_id=batch_id) is BatchState.PAUSED:
            break
        outcome = worker.run_once(batch_id=batch_id)
        if outcome is None:
            break
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if on_outcome is not None:
            on_outcome(outcome, sum(outcomes.values()))
    return outcomes

def state_of(*, registry: Registry, batch_id: str) -> BatchState:
    batch = registry.get_batch(batch_id)
    if batch is None:
        raise ValueError(f"No batch {batch_id!r}")
    return BatchState(batch.state)

def progress(*, registry: Registry, batch_id: str) -> dict[FileState, int]:
    return registry.counts_by_state(batch_id=batch_id)

class ExitCriteriaNotMetError(RuntimeError):

    def __init__(self, batch_id: str, reasons: list[str]) -> None:
        super().__init__(f"Batch {batch_id!r} does not meet exit criteria: {'; '.join(reasons)}")
        self.batch_id = batch_id
        self.reasons = reasons

def exit_criteria_met(*, registry: Registry, batch_id: str) -> tuple[bool, list[str]]:
    runs = controls.history(registry=registry, batch_id=batch_id)
    if not runs:
        return False, [f"no runs recorded for batch {batch_id!r}"]

    reasons = [
        f"run {run.run_id} (seq {run.run_seq}) is still open"
        for run in runs
        if run.status is controls.RunStatus.RUNNING
    ]
    if reasons:
        return False, reasons

    last = runs[-1]
    if last.status is controls.RunStatus.ABORTED:
        return False, [f"last run (seq {last.run_seq}) was aborted, not closed cleanly"]
    if last.signed_off_at is None:
        return False, [f"last run (seq {last.run_seq}) has not been signed off"]
    return True, []

def mark_done(*, registry: Registry, batch_id: str) -> None:
    if state_of(registry=registry, batch_id=batch_id) is BatchState.DONE:
        return
    ok, reasons = exit_criteria_met(registry=registry, batch_id=batch_id)
    if not ok:
        raise ExitCriteriaNotMetError(batch_id, reasons)
    registry.set_batch_state(batch_id=batch_id, state=BatchState.DONE)

@dataclass(frozen=True, slots=True)
class WorkerHandle:

    process: subprocess.Popen[bytes]
    log_path: Path

    @property
    def pid(self) -> int:
        return self.process.pid

    def is_running(self) -> bool:
        return self.process.poll() is None

    def tail(self, *, lines: int = 40) -> str:
        if not self.log_path.exists():
            return ""
        return "".join(
            self.log_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[
                -lines:
            ]
        )

def start_run_subprocess(
    *, batch_id: str, max_files: int | None = None, environment: str | None = None
) -> WorkerHandle:
    args = [sys.executable, "-m", "migration_hub.cli", "run", "--batch", batch_id]
    if max_files is not None:
        args += ["--max-files", str(max_files)]

    env = os.environ.copy()
    if environment is not None:
        env["MIGRATION_HUB_ENV"] = environment

    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run-{batch_id}-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
    log_file = log_path.open("w", encoding="utf-8")

    process = subprocess.Popen(
        args,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=_WINDOWS_DETACH_FLAGS,
        close_fds=True,
        start_new_session=sys.platform != "win32",
    )
    log_file.close()
    return WorkerHandle(process=process, log_path=log_path)

def start_run_subprocesses(
    *,
    registry: Registry,
    batch_id: str,
    count: int,
    max_files: int | None = None,
    environment: str | None = None,
) -> list[WorkerHandle]:
    batch = registry.get_batch(batch_id)
    effective_count = count if batch is None else min(count, max(batch.max_concurrency, 1))
    return [
        start_run_subprocess(batch_id=batch_id, max_files=max_files, environment=environment)
        for _ in range(effective_count)
    ]

class NotRetryableError(Exception):

    def __init__(self, file_id: int, state: str) -> None:
        self.file_id = file_id
        self.state = state
        super().__init__(f"file {file_id} is {state}, not FAILED or ABANDONED")

@dataclass(frozen=True, slots=True)
class RetryOutcome:

    requeued_file_ids: list[int]
    workers: list[WorkerHandle]

STAGING_FAILURE_PREFIX = "staging failed: "

def _failed_before_staging(file: MigrationFile) -> bool:
    return (file.last_error or "").startswith(STAGING_FAILURE_PREFIX)

def retry_files(
    registry: Registry,
    *,
    batch_id: str,
    file_ids: Sequence[int],
    actor: str,
    detail: str | None = None,
    worker_count: int = 1,
    max_files_per_worker: int | None = 1,
    environment: str | None = None,
) -> RetryOutcome:
    for file_id in file_ids:
        file = registry.get(file_id)
        if file is None:
            raise ValueError(f"No migration_file with file_id={file_id}")
        if FileState(file.state) not in (FileState.FAILED, FileState.ABANDONED):
            raise NotRetryableError(file_id, file.state)

    requeued_to: dict[int, FileState] = {}
    for file_id in file_ids:
        file = registry.get(file_id)
        assert file is not None
        target = FileState.VALIDATED if _failed_before_staging(file) else FileState.STAGED
        requeued_to[file_id] = target
        registry.transition(file_id=file_id, to_state=target, actor=actor, detail=detail)

    if not any(state is FileState.STAGED for state in requeued_to.values()):
        return RetryOutcome(requeued_file_ids=list(file_ids), workers=[])

    workers = start_run_subprocesses(
        registry=registry,
        batch_id=batch_id,
        count=worker_count,
        max_files=max_files_per_worker,
        environment=environment,
    )
    return RetryOutcome(requeued_file_ids=list(file_ids), workers=workers)
