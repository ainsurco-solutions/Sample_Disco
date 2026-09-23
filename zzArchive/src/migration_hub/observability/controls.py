from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from migration_hub.core.models import BatchRun
from migration_hub.core.registry import Registry
from migration_hub.core.states import FileState

class RunTrigger(StrEnum):

    RUN = "RUN"
    RETRY = "RETRY"
    RESUME = "RESUME"

class RunStatus(StrEnum):

    RUNNING = "RUNNING"
    CLEAN = "CLEAN"
    EXCEPTIONS = "EXCEPTIONS"
    ABORTED = "ABORTED"

@dataclass(frozen=True, slots=True)
class ControlTotals:

    source_count: int
    source_bytes: int
    target_count: int
    target_bytes: int

    completed_count: int
    abandoned_count: int
    rejected_count: int
    skipped_count: int
    outstanding_count: int

    retry_count: int
    exception_count: int

    archive_failed_count: int = 0
    bridge_count: int = 0

    @property
    def balances(self) -> bool:
        return self.unaccounted == 0

    @property
    def unaccounted(self) -> int:
        return self.source_count - (
            self.completed_count
            + self.archive_failed_count
            + self.abandoned_count
            + self.rejected_count
            + self.skipped_count
            + self.outstanding_count
        )

@dataclass(frozen=True, slots=True)
class ControlRecord:

    run_id: UUID
    batch_id: str
    run_seq: int
    trigger: RunTrigger
    initiated_by: str
    started_at: datetime
    finished_at: datetime | None
    status: RunStatus
    totals: ControlTotals
    evidence_uri: str | None
    evidence_sha256: str | None
    reconciled_at: datetime | None
    signed_off_by: str | None
    signed_off_at: datetime | None
    notes: str | None

class ControlBalanceError(RuntimeError):

    def __init__(self, run_id: UUID, unaccounted: int) -> None:
        super().__init__(
            f"Control totals for run {run_id} do not balance: {unaccounted} unaccounted"
        )
        self.run_id = run_id
        self.unaccounted = unaccounted

class RunNotSettledError(RuntimeError):

    def __init__(self, run_id: UUID, outstanding: list[int]) -> None:
        super().__init__(f"Run {run_id} has {len(outstanding)} file(s) still in flight")
        self.run_id = run_id
        self.outstanding = outstanding

def open_run(
    *, registry: Registry, batch_id: str, trigger: RunTrigger, initiated_by: str
) -> ControlRecord:
    metrics = registry.file_metrics(batch_id=batch_id)
    run_id = uuid4()
    registry.create_batch_run(
        run=BatchRun(
            run_id=run_id,
            batch_id=batch_id,
            run_seq=registry.next_run_seq(batch_id=batch_id),
            trigger=str(trigger),
            initiated_by=initiated_by,
            status=str(RunStatus.RUNNING),
            source_count=metrics["total_count"],
            source_bytes=metrics["total_bytes"],
        )
    )
    return _to_record(_require_run(registry, run_id))

def derive(*, registry: Registry, run_id: UUID) -> ControlTotals:
    run = _require_run(registry, run_id)
    counts = registry.counts_by_state(batch_id=run.batch_id)
    metrics = registry.file_metrics(batch_id=run.batch_id)

    completed = counts[FileState.COMPLETED]
    archive_failed = counts[FileState.ARCHIVE_FAILED]
    abandoned = counts[FileState.ABANDONED]
    rejected = counts[FileState.REJECTED]
    outstanding = sum(counts.values()) - completed - archive_failed - abandoned - rejected

    attempts_sum = metrics["attempts_sum"]
    attempted_count = metrics["attempted_count"]

    return ControlTotals(
        source_count=run.source_count,
        source_bytes=run.source_bytes,
        target_count=completed,
        target_bytes=metrics["completed_bytes"],
        completed_count=completed,
        abandoned_count=abandoned,
        rejected_count=rejected,
        skipped_count=0,
        outstanding_count=outstanding,
        retry_count=attempts_sum - attempted_count,
        exception_count=abandoned + rejected + archive_failed,
        archive_failed_count=archive_failed,
    )

def close(
    *,
    registry: Registry,
    run_id: UUID,
    reconciled_target_count: int,
    reconciled_target_bytes: int | None = None,
    reconciled_bridge_count: int = 0,
) -> ControlRecord:
    run = _require_run(registry, run_id)
    provisional = derive(registry=registry, run_id=run_id)

    if provisional.outstanding_count != 0:
        outstanding_ids = [f.file_id for f in registry.outstanding(batch_id=run.batch_id)]
        raise RunNotSettledError(run_id, outstanding_ids)

    target_bytes = (
        reconciled_target_bytes
        if reconciled_target_bytes is not None
        else registry.file_metrics(batch_id=run.batch_id)["completed_bytes"]
    )
    final = ControlTotals(
        source_count=provisional.source_count,
        source_bytes=provisional.source_bytes,
        target_count=reconciled_target_count,
        target_bytes=target_bytes,
        completed_count=provisional.completed_count,
        abandoned_count=provisional.abandoned_count,
        rejected_count=provisional.rejected_count,
        skipped_count=provisional.skipped_count,
        outstanding_count=provisional.outstanding_count,
        retry_count=provisional.retry_count,
        exception_count=provisional.exception_count,
        archive_failed_count=provisional.archive_failed_count,
        bridge_count=reconciled_bridge_count,
    )
    if not final.balances:
        raise ControlBalanceError(run_id, final.unaccounted)

    clean = (
        final.abandoned_count == 0
        and final.rejected_count == 0
        and final.archive_failed_count == 0
        and final.target_count == final.completed_count
    )
    registry.update_batch_run(
        run_id=run_id,
        status=str(RunStatus.CLEAN if clean else RunStatus.EXCEPTIONS),
        finished_at=datetime.now(UTC).replace(tzinfo=None),
        reconciled_at=datetime.now(UTC).replace(tzinfo=None),
        target_count=final.target_count,
        target_bytes=final.target_bytes,
        completed_count=final.completed_count,
        abandoned_count=final.abandoned_count,
        rejected_count=final.rejected_count,
        skipped_count=final.skipped_count,
        outstanding_count=final.outstanding_count,
        retry_count=final.retry_count,
        exception_count=final.exception_count,
        archive_failed_count=final.archive_failed_count,
        bridge_count=final.bridge_count,
    )
    return _to_record(_require_run(registry, run_id))

def abort(*, registry: Registry, run_id: UUID, reason: str) -> ControlRecord:
    if not reason.strip():
        raise ValueError("abort requires a non-empty reason")

    _require_run(registry, run_id)
    registry.update_batch_run(
        run_id=run_id,
        status=str(RunStatus.ABORTED),
        finished_at=datetime.now(UTC).replace(tzinfo=None),
        notes=reason,
    )
    return _to_record(_require_run(registry, run_id))

def export(*, registry: Registry, run_id: UUID, output_path: str) -> str:
    run = _require_run(registry, run_id)
    exceptions = registry.files_in_states(
        states=[FileState.ABANDONED, FileState.REJECTED, FileState.ARCHIVE_FAILED],
        batch_id=run.batch_id,
    )

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "run_id",
                "batch_id",
                "run_seq",
                "trigger",
                "status",
                "started_at",
                "finished_at",
                "source_count",
                "source_bytes",
                "target_count",
                "target_bytes",
                "completed_count",
                "abandoned_count",
                "rejected_count",
                "skipped_count",
                "outstanding_count",
                "retry_count",
                "exception_count",
                "archive_failed_count",
                "bridge_count",
            ]
        )
        writer.writerow(
            [
                run.run_id,
                run.batch_id,
                run.run_seq,
                run.trigger,
                run.status,
                run.started_at,
                run.finished_at,
                run.source_count,
                run.source_bytes,
                run.target_count,
                run.target_bytes,
                run.completed_count,
                run.abandoned_count,
                run.rejected_count,
                run.skipped_count,
                run.outstanding_count,
                run.retry_count,
                run.exception_count,
                run.archive_failed_count,
                run.bridge_count,
            ]
        )
        writer.writerow([])
        writer.writerow(
            ["file_id", "source_database", "target_exposure_name", "state", "last_error"]
        )
        for file in exceptions:
            writer.writerow(
                [
                    file.file_id,
                    file.source_database,
                    file.target_exposure_name,
                    file.state,
                    file.last_error,
                ]
            )

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    registry.update_batch_run(run_id=run_id, evidence_uri=str(path), evidence_sha256=digest)
    return digest

def sign_off(*, registry: Registry, run_id: UUID, by: str) -> ControlRecord:
    run = _require_run(registry, run_id)
    if run.status == str(RunStatus.RUNNING):
        raise ValueError(f"run {run_id} is still open -- close it before sign-off")

    if run.status == str(RunStatus.EXCEPTIONS):
        exceptions = registry.files_in_states(
            states=[FileState.ABANDONED, FileState.ARCHIVE_FAILED], batch_id=run.batch_id
        )
        unexplained = [file.file_id for file in exceptions if not file.last_error]
        if unexplained:
            raise ValueError(
                f"{len(unexplained)} abandoned/archive-failed file(s) have no recorded "
                f"reason: {unexplained}"
            )

    registry.update_batch_run(
        run_id=run_id, signed_off_by=by, signed_off_at=datetime.now(UTC).replace(tzinfo=None)
    )
    return _to_record(_require_run(registry, run_id))

def verify(*, registry: Registry, run_id: UUID) -> tuple[bool, list[str]]:
    run = _require_run(registry, run_id)
    if run.status == str(RunStatus.RUNNING):
        raise ValueError(f"run {run_id} is still open -- nothing frozen to verify yet")

    fresh = derive(registry=registry, run_id=run_id)
    fields = (
        ("completed_count", run.completed_count, fresh.completed_count),
        ("abandoned_count", run.abandoned_count, fresh.abandoned_count),
        ("rejected_count", run.rejected_count, fresh.rejected_count),
        ("skipped_count", run.skipped_count, fresh.skipped_count),
        ("outstanding_count", run.outstanding_count, fresh.outstanding_count),
        ("retry_count", run.retry_count, fresh.retry_count),
        ("exception_count", run.exception_count, fresh.exception_count),
        ("archive_failed_count", run.archive_failed_count, fresh.archive_failed_count),
    )
    divergences = [
        f"{name}: stored {stored}, now {current}"
        for name, stored, current in fields
        if stored != current
    ]
    return not divergences, divergences

def history(*, registry: Registry, batch_id: str) -> list[ControlRecord]:
    return [_to_record(run) for run in registry.list_batch_runs(batch_id=batch_id)]

def _require_run(registry: Registry, run_id: UUID) -> BatchRun:
    run = registry.get_batch_run(run_id)
    if run is None:
        raise ValueError(f"No batch_run with run_id={run_id}")
    return run

def _to_record(run: BatchRun) -> ControlRecord:
    return ControlRecord(
        run_id=run.run_id,
        batch_id=run.batch_id,
        run_seq=run.run_seq,
        trigger=RunTrigger(run.trigger),
        initiated_by=run.initiated_by,
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=RunStatus(run.status),
        totals=ControlTotals(
            source_count=run.source_count,
            source_bytes=run.source_bytes,
            target_count=run.target_count or 0,
            target_bytes=run.target_bytes or 0,
            completed_count=run.completed_count,
            abandoned_count=run.abandoned_count,
            rejected_count=run.rejected_count,
            skipped_count=run.skipped_count,
            outstanding_count=run.outstanding_count,
            retry_count=run.retry_count,
            exception_count=run.exception_count,
            archive_failed_count=run.archive_failed_count,
            bridge_count=run.bridge_count or 0,
        ),
        evidence_uri=run.evidence_uri,
        evidence_sha256=run.evidence_sha256,
        reconciled_at=run.reconciled_at,
        signed_off_by=run.signed_off_by,
        signed_off_at=run.signed_off_at,
        notes=run.notes,
    )
