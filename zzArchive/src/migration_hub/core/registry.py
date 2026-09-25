from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import PureWindowsPath
from uuid import UUID

from sqlalchemy import bindparam, case, func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from migration_hub.core.models import (
    ApiTransaction,
    Batch,
    BatchRun,
    MigrationEvent,
    MigrationFile,
)
from migration_hub.core.scrub import scrub_credentials, scrub_optional
from migration_hub.core.states import (
    CLAIMED_STATES,
    SETTLED_STATES,
    TERMINAL_STATES,
    BatchDestination,
    BatchState,
    FileState,
    assert_transition,
)

_log = logging.getLogger(__name__)

_LEVEL_BY_STATE: dict[FileState, int] = {
    FileState.ABANDONED: logging.ERROR,
    FileState.FAILED: logging.WARNING,
    FileState.ARCHIVE_FAILED: logging.WARNING,
    FileState.REJECTED: logging.WARNING,
}

def _describe(file: MigrationFile) -> str:
    name = PureWindowsPath(file.source_path).name
    size = f"{file.size_bytes / 1024**2:,.1f} MB" if file.size_bytes else "size unknown"
    return f"{file.batch_id} file {file.file_id} ({name}, {size})"

def _log_claim(
    claimed: MigrationFile | None, *, from_state: FileState, to_state: FileState
) -> MigrationFile | None:
    if claimed is not None:
        _log.info(
            "%s  %s -> %s  attempt %d",
            _describe(claimed),
            from_state,
            to_state,
            claimed.attempts,
        )
    return claimed

class Registry:

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def ensure_batch(
        self,
        *,
        batch_id: str,
        description: str | None = None,
        max_concurrency: int = 1,
        destination: BatchDestination = BatchDestination.VAULT,
    ) -> None:
        try:
            with Session(self._engine) as session, session.begin():
                existing = session.get(Batch, batch_id)
                if existing is None:
                    session.add(
                        Batch(
                            batch_id=batch_id,
                            description=description,
                            state="PLANNED",
                            max_concurrency=max_concurrency,
                            destination=str(destination),
                        )
                    )
        except IntegrityError:
            pass

    def get(self, file_id: int) -> MigrationFile | None:
        with Session(self._engine) as session:
            file = session.get(MigrationFile, file_id)
            if file is not None:
                session.expunge(file)
            return file

    def record_identifiers(self, *, file_id: int, actor: str, **fields: object) -> None:
        fields = _scrub_last_error(fields)
        with Session(self._engine) as session, session.begin():
            file = session.get(MigrationFile, file_id)
            if file is None:
                raise ValueError(f"No migration_file with file_id={file_id}")
            for name, value in fields.items():
                if not hasattr(file, name):
                    raise ValueError(f"Unknown migration_file column: {name!r}")
                setattr(file, name, value)
            session.add(
                MigrationEvent(
                    file_id=file_id,
                    from_state=file.state,
                    to_state=file.state,
                    actor=actor,
                    detail=f"recorded {', '.join(fields)}",
                )
            )

    def register(self, files: Sequence[MigrationFile]) -> int:
        if not files:
            return 0

        with Session(self._engine) as session, session.begin():
            incoming_paths = [f.source_path for f in files]
            existing_paths = set(
                session.scalars(
                    select(MigrationFile.source_path).where(
                        MigrationFile.source_path.in_(incoming_paths)
                    )
                )
            )
            new_files = [f for f in files if f.source_path not in existing_paths]
            for file in new_files:
                session.add(file)
            session.flush()
            return len(new_files)

    def known_source_paths(self, *, paths: Sequence[str]) -> set[str]:
        if not paths:
            return set()
        with Session(self._engine) as session:
            return set(
                session.scalars(
                    select(MigrationFile.source_path).where(MigrationFile.source_path.in_(paths))
                )
            )

    def claim_next(
        self,
        *,
        batch_id: str,
        from_state: FileState,
        to_state: FileState,
        worker_id: str,
        exclude_file_ids: Iterable[int] = (),
    ) -> MigrationFile | None:
        assert_transition(from_state, to_state)

        if self._engine.dialect.name == "sqlite":
            return _log_claim(
                self._claim_next_sqlite(
                    batch_id=batch_id,
                    from_state=from_state,
                    to_state=to_state,
                    worker_id=worker_id,
                    exclude_file_ids=exclude_file_ids,
                ),
                from_state=from_state,
                to_state=to_state,
            )

        exclude_clause = "AND file_id NOT IN :exclude_file_ids" if exclude_file_ids else ""
        claim_sql = text(
            f"""
            UPDATE TOP (1) migration_file
               SET state      = :to_state,
                   claimed_by = :worker_id,
                   claimed_at = SYSUTCDATETIME(),
                   attempts   = attempts + 1
            OUTPUT inserted.*
             WHERE file_id = (
                    SELECT TOP (1) file_id
                      FROM migration_file WITH (UPDLOCK, READPAST)
                     WHERE state    = :from_state
                       AND batch_id = :batch_id
                       {exclude_clause}
                     ORDER BY file_id
                 );
            """
        )
        if exclude_file_ids:
            claim_sql = claim_sql.bindparams(bindparam("exclude_file_ids", expanding=True))

        params: dict[str, object] = {
            "to_state": str(to_state),
            "worker_id": worker_id,
            "from_state": str(from_state),
            "batch_id": batch_id,
        }
        if exclude_file_ids:
            params["exclude_file_ids"] = list(exclude_file_ids)

        with Session(self._engine) as session, session.begin():
            row = (
                session.execute(claim_sql, params)
                .mappings()
                .first()
            )
            if row is None:
                return None

            file_id = row["file_id"]
            session.add(
                MigrationEvent(
                    file_id=file_id,
                    from_state=str(from_state),
                    to_state=str(to_state),
                    actor=worker_id,
                    detail="claimed",
                )
            )
            claimed = session.get(MigrationFile, file_id)
            session.flush()
            session.expunge(claimed)
        return _log_claim(claimed, from_state=from_state, to_state=to_state)

    def _claim_next_sqlite(
        self,
        *,
        batch_id: str,
        from_state: FileState,
        to_state: FileState,
        worker_id: str,
        exclude_file_ids: Iterable[int] = (),
    ) -> MigrationFile | None:
        exclude = list(exclude_file_ids)
        select_sql = (
            "SELECT file_id FROM migration_file "
            "WHERE state = :from_state AND batch_id = :batch_id"
        )
        if exclude:
            placeholders = ",".join(f":x{i}" for i in range(len(exclude)))
            select_sql += f" AND file_id NOT IN ({placeholders})"
        select_sql += " ORDER BY file_id LIMIT 1"

        params: dict[str, object] = {
            "from_state": str(from_state),
            "batch_id": batch_id,
        }
        params.update({f"x{i}": v for i, v in enumerate(exclude)})

        with Session(self._engine) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                row = session.execute(text(select_sql), params).first()
                if row is None:
                    session.rollback()
                    return None

                file_id = int(row[0])
                session.execute(
                    text(
                        "UPDATE migration_file "
                        "SET state = :to_state, claimed_by = :worker_id, "
                        "    claimed_at = :claimed_at, attempts = attempts + 1 "
                        "WHERE file_id = :file_id"
                    ),
                    {
                        "to_state": str(to_state),
                        "worker_id": worker_id,
                        "claimed_at": datetime.now(UTC).replace(tzinfo=None),
                        "file_id": file_id,
                    },
                )
                session.add(
                    MigrationEvent(
                        file_id=file_id,
                        from_state=str(from_state),
                        to_state=str(to_state),
                        actor=worker_id,
                        detail="claimed",
                    )
                )
                session.commit()
            except Exception:
                session.rollback()
                raise

        with Session(self._engine) as session:
            claimed = session.get(MigrationFile, file_id)
            if claimed is not None:
                session.expunge(claimed)
            return claimed

    def stale_claims(self, *, timeout_minutes: int) -> Sequence[MigrationFile]:
        cutoff = datetime.now(UTC) - timedelta(minutes=timeout_minutes)

        with Session(self._engine) as session:
            stale = list(
                session.scalars(
                    select(MigrationFile).where(
                        MigrationFile.state.in_([str(s) for s in CLAIMED_STATES]),
                        MigrationFile.claimed_at < cutoff,
                    )
                )
            )
            session.expunge_all()
            return stale

    def record_upload_progress(self, *, file_id: int, bytes_sent: int, start: bool = False) -> None:
        now = datetime.now(UTC).replace(tzinfo=None)
        values: dict[str, object] = {"bytes_sent": bytes_sent, "bytes_sent_at": now}
        if start:
            values["upload_started_at"] = now
        with Session(self._engine) as session, session.begin():
            session.execute(
                update(MigrationFile).where(MigrationFile.file_id == file_id).values(**values)
            )

    def refresh_claims(self, *, worker_id: str) -> int:
        with Session(self._engine) as session, session.begin():
            result = session.execute(
                update(MigrationFile)
                .where(
                    MigrationFile.claimed_by == worker_id,
                    MigrationFile.state.in_([str(s) for s in CLAIMED_STATES]),
                )
                .values(claimed_at=datetime.now(UTC).replace(tzinfo=None))
            )
            return int(getattr(result, "rowcount", 0) or 0)

    def claimed_count(self, *, batch_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(MigrationFile)
            .where(
                MigrationFile.batch_id == batch_id,
                MigrationFile.state.in_([str(s) for s in CLAIMED_STATES]),
            )
        )
        with Session(self._engine) as session:
            return session.scalar(stmt) or 0

    def transition(
        self,
        *,
        file_id: int,
        to_state: FileState,
        actor: str,
        detail: str | None = None,
        **fields: object,
    ) -> None:
        with Session(self._engine) as session, session.begin():
            file = session.get(MigrationFile, file_id)
            if file is None:
                raise ValueError(f"No migration_file with file_id={file_id}")

            from_state = FileState(file.state)
            assert_transition(from_state, to_state)
            detail = scrub_optional(detail)
            fields = _scrub_last_error(fields)

            for name, value in fields.items():
                if not hasattr(file, name):
                    raise ValueError(f"Unknown migration_file column: {name!r}")
                setattr(file, name, value)

            file.state = str(to_state)
            if to_state not in CLAIMED_STATES:
                file.claimed_by = None
                file.claimed_at = None
            if detail is not None and to_state in (
                FileState.FAILED,
                FileState.ABANDONED,
                FileState.REJECTED,
                FileState.ARCHIVE_FAILED,
            ):
                file.last_error = detail

            session.add(
                MigrationEvent(
                    file_id=file_id,
                    from_state=str(from_state),
                    to_state=str(to_state),
                    actor=actor,
                    detail=detail,
                )
            )
            summary = _describe(file)

        restaged = from_state is FileState.UPLOADING and to_state is FileState.VALIDATED
        level = logging.WARNING if restaged else _LEVEL_BY_STATE.get(to_state, logging.INFO)
        _log.log(
            level,
            "%s  %s -> %s%s",
            summary,
            from_state,
            to_state,
            f": {detail}" if detail else "",
        )

    def record_failure(
        self,
        *,
        file_id: int,
        error: str,
        retryable: bool,
        max_attempts: int,
        actor: str = "worker",
    ) -> FileState:
        with Session(self._engine) as session:
            file = session.get(MigrationFile, file_id)
            if file is None:
                raise ValueError(f"No migration_file with file_id={file_id}")
            attempts = file.attempts

        self.transition(
            file_id=file_id,
            to_state=FileState.FAILED,
            actor=actor,
            detail=error,
        )

        if retryable and attempts < max_attempts:
            return FileState.FAILED

        self.transition(
            file_id=file_id,
            to_state=FileState.ABANDONED,
            actor=actor,
            detail=(
                error
                if not retryable
                else f"attempts exhausted ({attempts}/{max_attempts}): {error}"
            ),
        )
        return FileState.ABANDONED

    def counts_by_state(self, *, batch_id: str | None = None) -> dict[FileState, int]:
        stmt = select(MigrationFile.state, func.count()).group_by(MigrationFile.state)
        if batch_id is not None:
            stmt = stmt.where(MigrationFile.batch_id == batch_id)

        counts = dict.fromkeys(FileState, 0)
        with Session(self._engine) as session:
            for state, count in session.execute(stmt):
                try:
                    counts[FileState(state)] = count
                except ValueError:
                    continue
        return counts

    def unknown_states(self, *, batch_id: str | None = None) -> dict[str, int]:
        stmt = select(MigrationFile.state, func.count()).group_by(MigrationFile.state)
        if batch_id is not None:
            stmt = stmt.where(MigrationFile.batch_id == batch_id)

        known = {str(s) for s in FileState}
        with Session(self._engine) as session:
            return {
                state: count
                for state, count in session.execute(stmt)
                if state not in known
            }

    def outstanding(self, *, batch_id: str | None = None) -> Sequence[MigrationFile]:
        stmt = select(MigrationFile).where(
            MigrationFile.state.not_in([str(s) for s in SETTLED_STATES])
        )
        if batch_id is not None:
            stmt = stmt.where(MigrationFile.batch_id == batch_id)
        stmt = stmt.order_by(MigrationFile.file_id)

        with Session(self._engine) as session:
            results = list(session.scalars(stmt))
            session.expunge_all()
            return results

    def completed_files(self, *, batch_id: str | None = None) -> Sequence[MigrationFile]:
        stmt = select(MigrationFile).where(MigrationFile.state == str(FileState.COMPLETED))
        if batch_id is not None:
            stmt = stmt.where(MigrationFile.batch_id == batch_id)
        stmt = stmt.order_by(MigrationFile.file_id)

        with Session(self._engine) as session:
            results = list(session.scalars(stmt))
            session.expunge_all()
            return results

    def pending_archives(
        self, *, batch_id: str | None = None, include_failed: bool = True
    ) -> Sequence[MigrationFile]:
        states = [FileState.BRIDGED, FileState.ARCHIVING]
        if include_failed:
            states.append(FileState.ARCHIVE_FAILED)
        stmt = select(MigrationFile).where(
            MigrationFile.state.in_([str(s) for s in states]),
            MigrationFile.instance_name.is_not(None),
        )
        if batch_id is not None:
            stmt = stmt.where(MigrationFile.batch_id == batch_id)
        stmt = stmt.order_by(MigrationFile.file_id)

        with Session(self._engine) as session:
            results = list(session.scalars(stmt))
            session.expunge_all()
            return results

    def get_batch(self, batch_id: str) -> Batch | None:
        with Session(self._engine) as session:
            batch = session.get(Batch, batch_id)
            if batch is not None:
                session.expunge(batch)
            return batch

    def batch_destination(self, batch_id: str) -> BatchDestination:
        with Session(self._engine) as session:
            batch = session.get(Batch, batch_id)
            return BatchDestination(batch.destination) if batch else BatchDestination.VAULT

    def set_batch_destination(self, *, batch_id: str, destination: BatchDestination) -> None:
        with Session(self._engine) as session, session.begin():
            batch = session.get(Batch, batch_id)
            if batch is None:
                raise ValueError(f"No batch {batch_id!r}")
            batch.destination = str(destination)

    def set_batch_state(self, *, batch_id: str, state: BatchState) -> None:
        with Session(self._engine) as session, session.begin():
            batch = session.get(Batch, batch_id)
            if batch is None:
                raise ValueError(f"No batch {batch_id!r}")
            batch.state = str(state)
            if state is BatchState.RUNNING and batch.started_at is None:
                batch.started_at = datetime.now(UTC).replace(tzinfo=None)
            if state is BatchState.DONE:
                batch.finished_at = datetime.now(UTC).replace(tzinfo=None)

    def set_batch_workers(self, *, batch_id: str, workers: int) -> None:
        with Session(self._engine) as session, session.begin():
            batch = session.get(Batch, batch_id)
            if batch is None:
                raise ValueError(f"No batch {batch_id!r}")
            batch.max_concurrency = max(1, int(workers))

    def list_batches(self) -> Sequence[Batch]:
        with Session(self._engine) as session:
            results = list(session.scalars(select(Batch).order_by(Batch.created_at.desc())))
            session.expunge_all()
            return results

    def files_in_states(
        self, *, states: Sequence[FileState], batch_id: str | None = None
    ) -> Sequence[MigrationFile]:
        stmt = select(MigrationFile).where(MigrationFile.state.in_([str(s) for s in states]))
        if batch_id is not None:
            stmt = stmt.where(MigrationFile.batch_id == batch_id)
        stmt = stmt.order_by(MigrationFile.file_id)

        with Session(self._engine) as session:
            results = list(session.scalars(stmt))
            session.expunge_all()
            return results

    def recent_events(
        self, *, batch_id: str | None = None, file_id: int | None = None, limit: int = 50
    ) -> Sequence[MigrationEvent]:
        stmt = select(MigrationEvent).order_by(MigrationEvent.event_id.desc()).limit(limit)
        if file_id is not None:
            stmt = stmt.where(MigrationEvent.file_id == file_id)
        if batch_id is not None:
            stmt = stmt.join(MigrationFile).where(MigrationFile.batch_id == batch_id)

        with Session(self._engine) as session:
            results = list(session.scalars(stmt))
            session.expunge_all()
            return results

    def recent_transactions(
        self, *, batch_id: str | None = None, file_id: int | None = None, limit: int = 50
    ) -> Sequence[ApiTransaction]:
        stmt = select(ApiTransaction).order_by(ApiTransaction.transaction_id.desc()).limit(limit)
        if file_id is not None:
            stmt = stmt.where(ApiTransaction.file_id == file_id)
        if batch_id is not None:
            stmt = stmt.join(MigrationFile).where(MigrationFile.batch_id == batch_id)

        with Session(self._engine) as session:
            results = list(session.scalars(stmt))
            session.expunge_all()
            return results

    def transactions_since(
        self, *, since: datetime, batch_id: str | None = None
    ) -> Sequence[ApiTransaction]:
        stmt = (
            select(ApiTransaction)
            .where(ApiTransaction.occurred_at >= since)
            .order_by(ApiTransaction.transaction_id.desc())
        )
        if batch_id is not None:
            stmt = stmt.join(MigrationFile).where(MigrationFile.batch_id == batch_id)

        with Session(self._engine) as session:
            results = list(session.scalars(stmt))
            session.expunge_all()
            return results

    def batch_files(self, *, batch_id: str) -> Sequence[MigrationFile]:
        stmt = (
            select(MigrationFile)
            .where(MigrationFile.batch_id == batch_id)
            .order_by(MigrationFile.file_id)
        )
        with Session(self._engine) as session:
            results = list(session.scalars(stmt))
            session.expunge_all()
            return results

    def batch_events(self, *, batch_id: str) -> Sequence[MigrationEvent]:
        stmt = (
            select(MigrationEvent)
            .join(MigrationFile)
            .where(MigrationFile.batch_id == batch_id)
            .order_by(MigrationEvent.event_id)
        )
        with Session(self._engine) as session:
            results = list(session.scalars(stmt))
            session.expunge_all()
            return results

    def batch_transactions(self, *, batch_id: str) -> Sequence[ApiTransaction]:
        stmt = (
            select(ApiTransaction)
            .join(MigrationFile, ApiTransaction.file_id == MigrationFile.file_id)
            .where(MigrationFile.batch_id == batch_id)
            .order_by(ApiTransaction.transaction_id)
        )
        with Session(self._engine) as session:
            results = list(session.scalars(stmt))
            session.expunge_all()
            return results

    def count_transitions_since(
        self, *, batch_id: str | None, to_state: FileState, since: datetime
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(MigrationEvent)
            .join(MigrationFile)
            .where(
                MigrationEvent.to_state == str(to_state),
                MigrationEvent.occurred_at >= since,
            )
        )
        if batch_id is not None:
            stmt = stmt.where(MigrationFile.batch_id == batch_id)
        with Session(self._engine) as session:
            return session.scalar(stmt) or 0

    def completed_transition_metrics_since(
        self, *, batch_id: str | None, since: datetime
    ) -> dict[str, int | datetime | None]:
        stmt = (
            select(
                func.count(MigrationEvent.event_id),
                func.coalesce(func.sum(MigrationFile.size_bytes), 0),
                func.min(MigrationFile.created_at),
                func.min(MigrationEvent.occurred_at),
            )
            .select_from(MigrationEvent)
            .join(MigrationFile)
            .where(
                MigrationEvent.to_state == str(FileState.COMPLETED),
                MigrationEvent.occurred_at >= since,
            )
        )
        if batch_id is not None:
            stmt = stmt.where(MigrationFile.batch_id == batch_id)

        with Session(self._engine) as session:
            files_completed, bytes_transferred, first_created, first_completed = session.execute(
                stmt
            ).one()
        starts = [t for t in (first_created, first_completed) if t is not None]
        return {
            "files_completed": int(files_completed),
            "bytes_transferred": int(bytes_transferred),
            "first_started_at": min(starts) if starts else None,
        }

    def file_terminal_times(self, *, batch_id: str) -> dict[int, datetime]:
        stmt = (
            select(MigrationEvent.file_id, MigrationEvent.occurred_at)
            .join(MigrationFile)
            .where(
                MigrationFile.batch_id == batch_id,
                MigrationEvent.to_state.in_([str(s) for s in TERMINAL_STATES]),
            )
        )
        with Session(self._engine) as session:
            return dict(session.execute(stmt).tuples().all())

    def earliest_file_created_at(self, *, batch_id: str) -> datetime | None:
        with Session(self._engine) as session:
            return session.scalar(
                select(func.min(MigrationFile.created_at)).where(MigrationFile.batch_id == batch_id)
            )

    def known_job_ids(self) -> set[str]:
        with Session(self._engine) as session:
            rows = session.execute(select(MigrationFile.job_id, MigrationFile.archive_job_id)).all()
        return {str(value).lower() for row in rows for value in row if value}

    def completed_exposure_names(self, *, batch_id: str | None = None) -> Sequence[str]:
        stmt = select(MigrationFile.target_exposure_name).where(
            MigrationFile.state == str(FileState.COMPLETED)
        )
        if batch_id is not None:
            stmt = stmt.where(MigrationFile.batch_id == batch_id)

        with Session(self._engine) as session:
            return list(session.scalars(stmt))

    def file_metrics(self, *, batch_id: str | None) -> dict[str, int]:
        stmt = select(
            func.count(),
            func.coalesce(func.sum(MigrationFile.size_bytes), 0),
            func.coalesce(
                func.sum(
                    case(
                        (MigrationFile.state == str(FileState.COMPLETED), MigrationFile.size_bytes),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(func.sum(MigrationFile.attempts), 0),
            func.coalesce(func.sum(case((MigrationFile.attempts >= 1, 1), else_=0)), 0),
        )
        if batch_id is not None:
            stmt = stmt.where(MigrationFile.batch_id == batch_id)

        with Session(self._engine) as session:
            total_count, total_bytes, completed_bytes, attempts_sum, attempted_count = (
                session.execute(stmt).one()
            )
        return {
            "total_count": total_count,
            "total_bytes": total_bytes,
            "completed_bytes": completed_bytes,
            "attempts_sum": attempts_sum,
            "attempted_count": attempted_count,
        }

    def bytes_by_state(self, *, batch_id: str) -> dict[str, int]:
        stmt = (
            select(MigrationFile.state, func.coalesce(func.sum(MigrationFile.size_bytes), 0))
            .where(MigrationFile.batch_id == batch_id)
            .group_by(MigrationFile.state)
        )
        with Session(self._engine) as session:
            return {str(state): int(total) for state, total in session.execute(stmt)}

    def remaining_work_metrics(self, *, batch_id: str | None) -> dict[str, int]:
        progress_states = (
            FileState.VALIDATED,
            FileState.UPLOADING,
            FileState.UPLOADED,
            FileState.IMPORTING,
            FileState.VERIFYING,
            FileState.BRIDGED,
            FileState.ARCHIVING,
        )
        needs_review = (
            ~MigrationFile.state.in_([str(state) for state in progress_states])
            | ~Batch.state.in_([str(BatchState.PLANNED), str(BatchState.RUNNING)])
            | ~Batch.destination.in_([str(destination) for destination in BatchDestination])
            | (MigrationFile.size_bytes <= 0)
        )
        stmt = (
            select(
                func.count(),
                func.coalesce(func.sum(MigrationFile.size_bytes), 0),
                func.coalesce(func.sum(case((needs_review, 1), else_=0)), 0),
                func.coalesce(
                    func.sum(case((needs_review, 0), else_=MigrationFile.size_bytes)), 0
                ),
            )
            .select_from(MigrationFile)
            .join(Batch, Batch.batch_id == MigrationFile.batch_id)
            .where(
                Batch.state != str(BatchState.DONE),
                MigrationFile.state != str(FileState.COMPLETED),
                ~(
                    (Batch.destination == str(BatchDestination.BRIDGE))
                    & (MigrationFile.state == str(FileState.BRIDGED))
                ),
            )
        )
        if batch_id is not None:
            stmt = stmt.where(MigrationFile.batch_id == batch_id)
        with Session(self._engine) as session:
            count, size, review, ready_bytes = session.execute(stmt).one()
        return {
            "remaining_count": count,
            "remaining_bytes": size,
            "review_count": review,
            "ready_count": count - review,
            "ready_bytes": ready_bytes,
        }

    def create_batch_run(self, *, run: BatchRun) -> None:
        with Session(self._engine) as session, session.begin():
            session.add(run)

    def get_batch_run(self, run_id: UUID) -> BatchRun | None:
        with Session(self._engine) as session:
            run = session.get(BatchRun, run_id)
            if run is not None:
                session.expunge(run)
            return run

    def list_batch_runs(self, *, batch_id: str) -> Sequence[BatchRun]:
        stmt = select(BatchRun).where(BatchRun.batch_id == batch_id).order_by(BatchRun.run_seq)
        with Session(self._engine) as session:
            results = list(session.scalars(stmt))
            session.expunge_all()
            return results

    def next_run_seq(self, *, batch_id: str) -> int:
        stmt = select(func.max(BatchRun.run_seq)).where(BatchRun.batch_id == batch_id)
        with Session(self._engine) as session:
            highest = session.scalar(stmt)
        return (highest or 0) + 1

    def update_batch_run(self, *, run_id: UUID, **fields: object) -> None:
        with Session(self._engine) as session, session.begin():
            run = session.get(BatchRun, run_id)
            if run is None:
                raise ValueError(f"No batch_run with run_id={run_id}")
            for name, value in fields.items():
                if not hasattr(run, name):
                    raise ValueError(f"Unknown batch_run column: {name!r}")
                setattr(run, name, value)

def _scrub_last_error(fields: dict[str, object]) -> dict[str, object]:
    value = fields.get("last_error")
    if isinstance(value, str):
        return {**fields, "last_error": scrub_credentials(value)}
    return fields
