from __future__ import annotations

from migration_hub.adapters.databridge.client import DatabridgeAdapter
from migration_hub.core.registry import Registry

def reconcile_target_count(
    *, registry: Registry, adapter: DatabridgeAdapter, batch_id: str
) -> tuple[int, int]:
    completed = registry.completed_files(batch_id=batch_id)
    confirmed_count = 0
    confirmed_bytes = 0
    for file in completed:
        with adapter.bound_to_file(file.file_id):
            confirmed = adapter.database_exists(
                instance_name=file.instance_name, database_name=file.database_name
            )
        if confirmed:
            confirmed_count += 1
            confirmed_bytes += file.size_bytes
    return confirmed_count, confirmed_bytes
