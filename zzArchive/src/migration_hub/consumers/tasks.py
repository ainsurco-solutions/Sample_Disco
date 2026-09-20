from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.engine import Engine

from migration_hub.adapters.irp.client import IrpAdapter
from migration_hub.config.settings import Settings
from migration_hub.consumers.worker import MigrationWorker
from migration_hub.core.engine import create_registry_engine
from migration_hub.core.registry import Registry
from migration_hub.core.states import FileState
from migration_hub.orchestration import reaper

def _load_settings() -> Settings:
    environment = os.environ.get("MIGRATION_HUB_ENV", "dev")
    return Settings.load(environment=environment, config_dir=Path("config"))

def _registry(settings: Settings, *, engine: Engine) -> Registry:
    return Registry(engine)

def _adapter(settings: Settings, *, engine: Engine) -> IrpAdapter:
    return IrpAdapter(
        host=settings.api_host,
        api_key=settings.api_key(),
        entitlement=settings.entitlement,
        engine=engine,
    )

def process_next(batch_id: str) -> bool:
    settings = _load_settings()
    engine = create_registry_engine(settings.database_url)
    try:
        registry = _registry(settings, engine=engine)

        batch = registry.get_batch(batch_id)
        if (
            batch is not None
            and registry.claimed_count(batch_id=batch_id) >= batch.max_concurrency
        ):
            return False

        adapter = _adapter(settings, engine=engine)
        worker = MigrationWorker(
            registry=registry,
            adapter=adapter,
            worker_id=f"scheduler-{os.getpid()}",
            dry_run=settings.dry_run,
            max_attempts=settings.max_attempts,
            poll_interval_seconds=settings.poll_interval_seconds,
            max_poll_minutes=settings.max_poll_minutes,
        )
        try:
            outcome = worker.run_once(batch_id=batch_id)
        finally:
            adapter.close()
        return outcome is not None
    finally:
        engine.dispose()

def poll_running_imports() -> int:
    settings = _load_settings()
    engine = create_registry_engine(settings.database_url)
    try:
        registry = _registry(settings, engine=engine)

        importing = [
            f
            for f in registry.stale_claims(timeout_minutes=settings.claim_timeout_minutes)
            if FileState(f.state) is FileState.IMPORTING and f.job_id is not None
        ]
        if not importing:
            return 0

        adapter = _adapter(settings, engine=engine)
        advanced = 0
        try:
            session = adapter.open_session()
            for file in importing:
                if reaper.reconcile_importing(
                    registry=registry,
                    adapter=adapter,
                    session=session,
                    file_id=file.file_id,
                    job_id=file.job_id,
                    exposure_name=file.target_exposure_name,
                    max_attempts=settings.max_attempts,
                    actor="scheduler-poll",
                ):
                    advanced += 1
        finally:
            adapter.close()
        return advanced
    finally:
        engine.dispose()

def reap(timeout_minutes: int | None = None) -> int:
    settings = _load_settings()
    engine = create_registry_engine(settings.database_url)
    try:
        registry = _registry(settings, engine=engine)
        adapter = _adapter(settings, engine=engine)
        try:
            return reaper.reap_stale_claims(
                registry=registry,
                adapter=adapter,
                timeout_minutes=(
                    settings.claim_timeout_minutes if timeout_minutes is None else timeout_minutes
                ),
                max_attempts=settings.max_attempts,
                actor="scheduler-reap",
            )
        finally:
            adapter.close()
    finally:
        engine.dispose()
