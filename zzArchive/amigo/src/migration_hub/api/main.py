from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

from migration_hub.api.routers import batches, files, health
from migration_hub.config.settings import Settings
from migration_hub.core.engine import create_registry_engine
from migration_hub.core.registry import Registry

load_dotenv()

def create_app(*, settings: Settings | None = None) -> FastAPI:
    if settings is None:
        environment = os.environ.get("MIGRATION_HUB_ENV", "dev")
        settings = Settings.load(environment=environment, config_dir=Path("config"))

    app = FastAPI(title="Migration Hub control plane", description=__doc__)
    app.state.settings = settings
    app.state.registry = Registry(create_registry_engine(settings.database_url))

    app.include_router(health.router)
    app.include_router(batches.router)
    app.include_router(files.router)
    return app

def serve() -> None:
    import uvicorn

    uvicorn.run(
        create_app(),
        host=os.environ.get("MIGRATION_HUB_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("MIGRATION_HUB_API_PORT", "8000")),
    )

if __name__ == "__main__":
    serve()
