from __future__ import annotations

from migration_hub.adapters.base import MigrationTargetAdapter
from migration_hub.core.registry import Registry

def reconcile_target_count(
    *, registry: Registry, adapter: MigrationTargetAdapter, batch_id: str
) -> tuple[int, int]:
    completed = registry.completed_files(batch_id=batch_id)
    confirmed_count = 0
    confirmed_bytes = 0
    for file in completed:
        with adapter.bound_to_file(file.file_id):
            if adapter.exposure_exists(exposure_name=file.target_exposure_name):
                confirmed_count += 1
                confirmed_bytes += file.size_bytes
    return confirmed_count, confirmed_bytes
