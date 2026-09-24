from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from pathlib import Path

from migration_hub.adapters.databridge.client import DatabridgeAdapter
from migration_hub.consumers.worker import MigrationWorker
from migration_hub.core.models import MigrationFile
from migration_hub.core.registry import Registry
from migration_hub.core.states import BatchDestination, BatchState, FileState
from migration_hub.observability import controls
from migration_hub.orchestration import archiver
from migration_hub.orchestration.batch_identity import derive_batch_id

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
    adapter: DatabridgeAdapter,
    batch_id: str,
    worker_id: str,
    dry_run: bool,
    max_attempts: int,
    poll_interval_seconds: float,
    max_poll_minutes: float,
    max_files: int | None = None,
    on_outcome: Callable[[FileState, int], None] | None = None,
    instance_name: str | None = None,
    instances: dict[str, str] | None = None,
    group_ids: list[str] | None = None,
    archive_after_bridge: bool = False,
    max_archive_attempts: int | None = None,
) -> dict[FileState, int]:
    worker = MigrationWorker(
        registry=registry,
        adapter=adapter,
        worker_id=worker_id,
        dry_run=dry_run,
        max_attempts=max_attempts,
        poll_interval_seconds=poll_interval_seconds,
        max_poll_minutes=max_poll_minutes,
        instance_name=instance_name,
        instances=instances,
        group_ids=group_ids,
        archive=(
            partial(
                _archive_bridged_file,
                registry=registry,
                adapter=adapter,
                actor=worker_id,
                max_archive_attempts=max_archive_attempts,
                max_wait_minutes=max_poll_minutes,
                poll_interval_seconds=poll_interval_seconds,
            )
            if archive_after_bridge
            else None
        ),
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

def _archive_bridged_file(
    file_id: int,
    resource_group_id: str | None,
    *,
    registry: Registry,
    adapter: DatabridgeAdapter,
    actor: str,
    max_archive_attempts: int | None,
    max_wait_minutes: float,
    poll_interval_seconds: float,
) -> FileState:
    return archiver.archive_one_file(
        registry=registry,
        adapter=adapter,
        file_id=file_id,
        resource_group_id=resource_group_id,
        actor=actor,
        max_archive_attempts=max_archive_attempts,
        max_wait_minutes=max_wait_minutes,
        poll_interval_seconds=poll_interval_seconds,
    )

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
    env["PYTHONUNBUFFERED"] = "1"

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

def start_archive_subprocess(
    *, batch_id: str | None = None, environment: str | None = None
) -> WorkerHandle:
    args = [sys.executable, "-m", "migration_hub.cli", "archive"]
    if batch_id is not None:
        args += ["--batch", batch_id]

    env = os.environ.copy()
    if environment is not None:
        env["MIGRATION_HUB_ENV"] = environment
    env["PYTHONUNBUFFERED"] = "1"

    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S-%f}"
    log_path = log_dir / f"archive-{batch_id or 'all'}-{stamp}.log"
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

RETRYABLE_STATES: frozenset[FileState] = frozenset(
    {FileState.FAILED, FileState.ABANDONED, FileState.ARCHIVE_FAILED}
)

class Location(StrEnum):

    DATA_VAULT = "DATA_VAULT"
    DATA_BRIDGE = "DATA_BRIDGE"
    NEITHER = "NEITHER"

def platform_locator(adapter: DatabridgeAdapter) -> Callable[[MigrationFile], Location]:

    def locate(file: MigrationFile) -> Location:
        database_name = file.database_name or file.source_database
        with adapter.bound_to_file(file.file_id):
            if adapter.archive_exists(database_name=database_name):
                return Location.DATA_VAULT
            if file.instance_name is not None and adapter.database_exists(
                instance_name=file.instance_name, database_name=database_name
            ):
                return Location.DATA_BRIDGE
        return Location.NEITHER

    return locate

IN_FLIGHT_STATES: frozenset[FileState] = frozenset(
    {
        FileState.UPLOADING,
        FileState.UPLOADED,
        FileState.IMPORTING,
        FileState.VERIFYING,
        FileState.ARCHIVING,
    }
)

def place_by_location(
    registry: Registry,
    *,
    file_id: int,
    location: Location,
    actor: str,
    detail: str,
    reset_attempts: bool,
) -> FileState:
    if location is Location.DATA_VAULT:
        registry.transition(
            file_id=file_id,
            to_state=FileState.COMPLETED,
            actor=actor,
            detail=f"{detail}: already in Data Vault",
            archived_at=datetime.now(UTC).replace(tzinfo=None),
        )
        return FileState.COMPLETED
    if location is Location.DATA_BRIDGE:
        registry.transition(
            file_id=file_id,
            to_state=FileState.BRIDGED,
            actor=actor,
            detail=f"{detail}: already on Data Bridge, archive only",
            **({"archive_attempts": 0} if reset_attempts else {}),
        )
        return FileState.BRIDGED
    fields: dict[str, object] = {"attempts": 0, "archive_attempts": 0} if reset_attempts else {}
    registry.transition(
        file_id=file_id,
        to_state=FileState.VALIDATED,
        actor=actor,
        detail=f"{detail}: on neither Data Bridge nor Data Vault, uploading again",
        **fields,
    )
    return FileState.VALIDATED

class NotRetryableError(Exception):

    def __init__(self, file_id: int, state: str) -> None:
        self.file_id = file_id
        self.state = state
        super().__init__(f"file {file_id} is {state}, not FAILED, ABANDONED or ARCHIVE_FAILED")

@dataclass(frozen=True, slots=True)
class RetryOutcome:

    requeued_file_ids: list[int]
    workers: list[WorkerHandle]
    archive_retry_file_ids: list[int] = field(default_factory=list)
    completed_file_ids: list[int] = field(default_factory=list)
    bridged_file_ids: list[int] = field(default_factory=list)
    unchecked: dict[int, str] = field(default_factory=dict)
    pipeline_started: bool = False
    not_started_reason: str | None = None

def retry_files(
    registry: Registry,
    *,
    batch_id: str,
    file_ids: Sequence[int],
    actor: str,
    locate: Callable[[MigrationFile], Location],
    detail: str | None = None,
    worker_count: int = 1,
    max_files_per_worker: int | None = 1,
    environment: str | None = None,
) -> RetryOutcome:
    for file_id in file_ids:
        file = registry.get(file_id)
        if file is None:
            raise ValueError(f"No migration_file with file_id={file_id}")
        if FileState(file.state) not in RETRYABLE_STATES:
            raise NotRetryableError(file_id, file.state)

    requeue: list[int] = []
    archive_retry: list[int] = []
    completed: list[int] = []
    bridged: list[int] = []
    unchecked: dict[int, str] = {}
    for file_id in file_ids:
        file = registry.get(file_id)
        assert file is not None
        if file.state == str(FileState.ARCHIVE_FAILED):
            archive_retry.append(file_id)
            continue
        try:
            location = locate(file)
        except Exception as exc:
            unchecked[file_id] = f"{type(exc).__name__}: {exc}"
            continue
        placed = place_by_location(
            registry,
            file_id=file_id,
            location=location,
            actor=actor,
            detail=f"retry ({detail})" if detail else "retry",
            reset_attempts=True,
        )
        {
            FileState.COMPLETED: completed,
            FileState.BRIDGED: bridged,
            FileState.VALIDATED: requeue,
        }[placed].append(file_id)

    workers: list[WorkerHandle] = []
    pipeline_started = False
    not_started_reason: str | None = None
    if requeue or bridged or archive_retry:
        in_flight = registry.files_in_states(states=sorted(IN_FLIGHT_STATES), batch_id=batch_id)
        if in_flight:
            not_started_reason = (
                f"{len(in_flight)} file(s) in batch {batch_id!r} are already in flight -- "
                "not starting a second pipeline or archiver over them. Once that work "
                f"finishes, run `migration-hub migrate --batch {batch_id}`."
            )
        elif requeue and registry.batch_destination(batch_id) is BatchDestination.BRIDGE:
            workers.extend(
                start_run_subprocesses(
                    registry=registry,
                    batch_id=batch_id,
                    count=worker_count,
                    environment=environment,
                )
            )
        elif requeue:
            workers.append(start_migrate_subprocess(batch_id=batch_id, environment=environment))
            pipeline_started = True
        elif archive_retry or registry.batch_destination(batch_id) is BatchDestination.VAULT:
            workers.append(start_archive_subprocess(batch_id=batch_id, environment=environment))

    return RetryOutcome(
        requeued_file_ids=requeue,
        workers=workers,
        archive_retry_file_ids=archive_retry,
        completed_file_ids=completed,
        bridged_file_ids=bridged,
        unchecked=unchecked,
        pipeline_started=pipeline_started,
        not_started_reason=not_started_reason,
    )

def start_to_destination(
    *, registry: Registry, batch_id: str, count: int, environment: str | None = None
) -> list[WorkerHandle]:
    if registry.batch_destination(batch_id) is BatchDestination.BRIDGE:
        return start_run_subprocesses(
            registry=registry, batch_id=batch_id, count=count, environment=environment
        )
    return [start_migrate_subprocess(batch_id=batch_id, environment=environment)]

def start_migrate_subprocess(*, batch_id: str, environment: str | None = None) -> WorkerHandle:
    return _launch_cli(["migrate", "--batch", batch_id], f"migrate-{batch_id}", environment)

def start_migrate_source_subprocess(
    *, source_root: Path, environment: str | None = None
) -> WorkerHandle:
    batch_id = derive_batch_id(source_root)
    return _launch_cli(
        ["migrate", "--source", str(source_root)], f"migrate-{batch_id}", environment
    )

def start_queue_subprocess(*, environment: str | None = None) -> WorkerHandle:
    return _launch_cli(["queue"], "queue", environment)

def start_controls_open_subprocess(
    *,
    batch_id: str,
    trigger: controls.RunTrigger | str,
    by: str | None = None,
    environment: str | None = None,
) -> WorkerHandle:
    args = ["controls", "open", "--batch", batch_id, "--trigger", str(trigger)]
    if by:
        args += ["--by", by]
    return _launch_cli(args, f"controls-open-{batch_id}", environment)

def start_controls_close_subprocess(
    *,
    run_id: str,
    environment: str | None = None,
    batch_id: str | None = None,
) -> WorkerHandle:
    return _launch_cli(
        ["controls", "close", "--run-id", run_id],
        f"controls-close-{batch_id or run_id}",
        environment,
    )

def start_controls_abort_subprocess(
    *,
    run_id: str,
    reason: str,
    environment: str | None = None,
    batch_id: str | None = None,
) -> WorkerHandle:
    return _launch_cli(
        ["controls", "abort", "--run-id", run_id, "--reason", reason],
        f"controls-abort-{batch_id or run_id}",
        environment,
    )

def start_controls_export_subprocess(
    *,
    run_id: str,
    output_path: Path,
    environment: str | None = None,
    batch_id: str | None = None,
) -> WorkerHandle:
    return _launch_cli(
        ["controls", "export", "--run-id", run_id, "--output", str(output_path)],
        f"controls-export-{batch_id or run_id}",
        environment,
    )

def start_controls_verify_subprocess(
    *,
    run_id: str,
    environment: str | None = None,
    batch_id: str | None = None,
) -> WorkerHandle:
    return _launch_cli(
        ["controls", "verify", "--run-id", run_id],
        f"controls-verify-{batch_id or run_id}",
        environment,
    )

def start_controls_sign_off_subprocess(
    *,
    run_id: str,
    by: str | None = None,
    environment: str | None = None,
    batch_id: str | None = None,
) -> WorkerHandle:
    args = ["controls", "sign-off", "--run-id", run_id]
    if by:
        args += ["--by", by]
    return _launch_cli(args, f"controls-sign-off-{batch_id or run_id}", environment)

_LOG_KINDS = (
    "run",
    "migrate",
    "archive",
    "retry",
    "controls-open",
    "controls-close",
    "controls-abort",
    "controls-export",
    "controls-verify",
    "controls-sign-off",
)

_TAIL_BYTES = 128 * 1024

def logs_for_batch(batch_id: str, *, log_dir: Path = Path("logs")) -> list[Path]:
    pattern = re.compile(
        rf"^(?:{'|'.join(_LOG_KINDS)})-{re.escape(batch_id)}-\d{{8}}-\d{{6}}.*\.log$"
    )
    if not log_dir.is_dir():
        return []
    found = [p for p in log_dir.iterdir() if p.is_file() and pattern.match(p.name)]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)

def tail_lines(path: Path, *, lines: int = 200) -> list[str]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - _TAIL_BYTES))
        chunk = handle.read()
    text = chunk.decode("utf-8", errors="replace").splitlines()
    if size > _TAIL_BYTES and text:
        text = text[1:]
    return text[-lines:]

def _launch_cli(cli_args: list[str], log_stem: str, environment: str | None) -> WorkerHandle:
    args = [sys.executable, "-m", "migration_hub.cli", *cli_args]
    env = os.environ.copy()
    if environment is not None:
        env["MIGRATION_HUB_ENV"] = environment
    env["PYTHONUNBUFFERED"] = "1"

    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S-%f}"
    log_path = log_dir / f"{log_stem}-{stamp}.log"
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

def start_retry_subprocess(
    *,
    batch_id: str | None = None,
    file_id: int | None = None,
    environment: str | None = None,
) -> WorkerHandle:
    if (batch_id is None) == (file_id is None):
        raise ValueError("specify exactly one of batch_id or file_id")
    if batch_id is not None:
        return _launch_cli(["retry", "--batch", batch_id], f"retry-{batch_id}", environment)
    return _launch_cli(["retry", "--file-id", str(file_id)], f"retry-file-{file_id}", environment)
