from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from migration_hub.api.deps import get_registry, get_settings
from migration_hub.config.settings import Settings
from migration_hub.core.registry import Registry

router = APIRouter(prefix="/health", tags=["health"])

@router.get("/live")
def liveness() -> dict[str, str]:
    return {"status": "ok"}

@router.get("/ready")
def readiness(
    registry: Registry = Depends(get_registry), settings: Settings = Depends(get_settings)
) -> dict[str, object]:
    try:
        registry.list_batches()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"registry unreachable: {exc}") from exc
    return {"status": "ok", "environment": settings.environment}
