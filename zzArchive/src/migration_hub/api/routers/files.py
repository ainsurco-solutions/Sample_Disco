from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from migration_hub.api.deps import get_registry
from migration_hub.core.models import ApiTransaction, MigrationEvent, MigrationFile
from migration_hub.core.registry import Registry
from migration_hub.core.states import FileState
from migration_hub.orchestration import batches as batch_ops

router = APIRouter(tags=["files"])

class FileSummary(BaseModel):
    file_id: int
    batch_id: str
    source_database: str
    source_path: str
    target_exposure_name: str
    size_bytes: int
    state: str
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime

class EventSummary(BaseModel):
    event_id: int
    file_id: int
    from_state: str | None
    to_state: str
    actor: str
    detail: str | None
    occurred_at: datetime

class TransactionSummary(BaseModel):
    transaction_id: int
    method: str
    url: str
    status_code: int | None
    duration_ms: int | None
    occurred_at: datetime

class FileDiagnostics(BaseModel):
    file: FileSummary
    events: list[EventSummary]
    transactions: list[TransactionSummary]

class RetryResult(BaseModel):
    file_id: int
    requeued: bool
    worker_pid: int

def _file_summary(file: MigrationFile) -> FileSummary:
    return FileSummary(
        file_id=file.file_id,
        batch_id=file.batch_id,
        source_database=file.source_database,
        source_path=file.source_path,
        target_exposure_name=file.target_exposure_name,
        size_bytes=file.size_bytes,
        state=file.state,
        attempts=file.attempts,
        last_error=file.last_error,
        created_at=file.created_at,
        updated_at=file.updated_at,
    )

def _event_summary(event: MigrationEvent) -> EventSummary:
    return EventSummary(
        event_id=event.event_id,
        file_id=event.file_id,
        from_state=event.from_state,
        to_state=event.to_state,
        actor=event.actor,
        detail=event.detail,
        occurred_at=event.occurred_at,
    )

def _transaction_summary(transaction: ApiTransaction) -> TransactionSummary:
    return TransactionSummary(
        transaction_id=transaction.transaction_id,
        method=transaction.method,
        url=transaction.url,
        status_code=transaction.status_code,
        duration_ms=transaction.duration_ms,
        occurred_at=transaction.occurred_at,
    )

def _require_file(registry: Registry, file_id: int) -> MigrationFile:
    file = registry.get(file_id)
    if file is None:
        raise HTTPException(status_code=404, detail=f"no file {file_id}")
    return file

@router.get("/batches/{batch_id}/files", response_model=list[FileSummary])
def list_files(
    batch_id: str,
    state: FileState | None = None,
    registry: Registry = Depends(get_registry),
) -> list[FileSummary]:
    states = [state] if state is not None else list(FileState)
    files = registry.files_in_states(states=states, batch_id=batch_id)
    return [_file_summary(f) for f in files]

@router.get("/files/{file_id}", response_model=FileSummary)
def get_file(file_id: int, registry: Registry = Depends(get_registry)) -> FileSummary:
    return _file_summary(_require_file(registry, file_id))

@router.get("/files/{file_id}/events", response_model=list[EventSummary])
def file_events(file_id: int, registry: Registry = Depends(get_registry)) -> list[EventSummary]:
    _require_file(registry, file_id)
    events = registry.recent_events(file_id=file_id, limit=500)
    return [_event_summary(e) for e in events]

@router.get("/files/{file_id}/diagnostics", response_model=FileDiagnostics)
def file_diagnostics(
    file_id: int, registry: Registry = Depends(get_registry)
) -> FileDiagnostics:
    file = _require_file(registry, file_id)
    events = registry.recent_events(file_id=file_id, limit=500)
    transactions = registry.recent_transactions(file_id=file_id, limit=500)
    return FileDiagnostics(
        file=_file_summary(file),
        events=[_event_summary(e) for e in events],
        transactions=[_transaction_summary(t) for t in transactions],
    )

@router.post("/files/{file_id}/retry", response_model=RetryResult)
def retry_file(file_id: int, registry: Registry = Depends(get_registry)) -> RetryResult:
    file = _require_file(registry, file_id)
    try:
        outcome = batch_ops.retry_files(
            registry,
            batch_id=file.batch_id,
            file_ids=[file_id],
            actor="api",
            worker_count=1,
            max_files_per_worker=1,
        )
    except batch_ops.NotRetryableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RetryResult(file_id=file_id, requeued=True, worker_pid=outcome.workers[0].pid)
