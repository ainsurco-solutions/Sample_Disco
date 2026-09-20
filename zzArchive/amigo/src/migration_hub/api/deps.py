from __future__ import annotations

from fastapi import Request

from migration_hub.config.settings import Settings
from migration_hub.core.registry import Registry

def get_registry(request: Request) -> Registry:
    return request.app.state.registry

def get_settings(request: Request) -> Settings:
    return request.app.state.settings
