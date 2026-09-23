from __future__ import annotations

from typing import NamedTuple

from migration_hub.adapters.databridge.client import DatabridgeAdapter
from migration_hub.core.registry import Registry
from migration_hub.core.states import FileState

class ReconciledTargets(NamedTuple):

    target_count: int
    target_bytes: int
    bridge_count: int
    lost_file_ids: list[int]

def reconcile_targets(
    *, registry: Registry, adapter: DatabridgeAdapter, batch_id: str
) -> ReconciledTargets:
    target_count = 0
    target_bytes = 0
    for file in registry.completed_files(batch_id=batch_id):
        if file.database_name is None:
            continue
        with adapter.bound_to_file(file.file_id):
            if adapter.archive_exists(database_name=file.database_name):
                target_count += 1
                target_bytes += file.size_bytes

    bridge_count = 0
    lost: list[int] = []
    for file in registry.files_in_states(states=[FileState.ARCHIVE_FAILED], batch_id=batch_id):
        if file.instance_name is None or file.database_name is None:
            lost.append(file.file_id)
            continue
        with adapter.bound_to_file(file.file_id):
            on_bridge = adapter.database_exists(
                instance_name=file.instance_name, database_name=file.database_name
            )
            in_vault = not on_bridge and adapter.archive_exists(database_name=file.database_name)
        if on_bridge:
            bridge_count += 1
        elif not in_vault:
            lost.append(file.file_id)

    return ReconciledTargets(target_count, target_bytes, bridge_count, lost)
