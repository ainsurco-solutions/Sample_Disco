from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from migration_hub.api.deps import get_registry
from migration_hub.core.registry import Registry
from migration_hub.core.states import FileState
from migration_hub.orchestration import batches as batch_ops

router = APIRouter(prefix="/batches", tags=["batches"])

class CreateBatchRequest(BaseModel):
    batch_id: str
    description: str | None = None
    max_concurrency: int = 1

class BatchSummary(BaseModel):
    batch_id: str
    state: str
    description: str | None
    counts: dict[str, int]

def _summary(
    registry: Registry, *, batch_id: str, state: str, description: str | None
) -> BatchSummary:
    counts = registry.counts_by_state(batch_id=batch_id)
    return BatchSummary(
        batch_id=batch_id,
        state=state,
        description=description,
        counts={s.value: counts[s] for s in FileState},
    )

def _require_batch(registry: Registry, batch_id: str) -> BatchSummary:
    batch = registry.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"no batch {batch_id!r}")
    return _summary(
        registry, batch_id=batch.batch_id, state=batch.state, description=batch.description
    )

@router.get("", response_model=list[BatchSummary])
def list_batches(registry: Registry = Depends(get_registry)) -> list[BatchSummary]:
    return [
        _summary(registry, batch_id=b.batch_id, state=b.state, description=b.description)
        for b in registry.list_batches()
    ]

@router.post("", response_model=BatchSummary, status_code=201)
def create_batch(
    body: CreateBatchRequest, registry: Registry = Depends(get_registry)
) -> BatchSummary:
    batch_ops.create_batch(
        registry=registry,
        batch_id=body.batch_id,
        description=body.description,
        max_concurrency=body.max_concurrency,
    )
    return _require_batch(registry, body.batch_id)

@router.get("/{batch_id}", response_model=BatchSummary)
def get_batch(batch_id: str, registry: Registry = Depends(get_registry)) -> BatchSummary:
    return _require_batch(registry, batch_id)

@router.post("/{batch_id}/pause", response_model=BatchSummary)
def pause_batch(batch_id: str, registry: Registry = Depends(get_registry)) -> BatchSummary:
    _require_batch(registry, batch_id)
    batch_ops.pause(registry=registry, batch_id=batch_id)
    return _require_batch(registry, batch_id)

@router.post("/{batch_id}/resume", response_model=BatchSummary)
def resume_batch(batch_id: str, registry: Registry = Depends(get_registry)) -> BatchSummary:
    _require_batch(registry, batch_id)
    batch_ops.resume(registry=registry, batch_id=batch_id)
    return _require_batch(registry, batch_id)
