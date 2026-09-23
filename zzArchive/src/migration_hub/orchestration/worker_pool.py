from __future__ import annotations

import threading
from collections.abc import Callable

from migration_hub.adapters.databridge.client import DatabridgeAdapter
from migration_hub.core.registry import Registry
from migration_hub.core.states import FileState
from migration_hub.orchestration import batches

def run_worker_pool(
    *,
    registry: Registry,
    adapter_factory: Callable[[], DatabridgeAdapter],
    batch_id: str,
    thread_count: int,
    dry_run: bool,
    max_attempts: int,
    poll_interval_seconds: float,
    max_poll_minutes: float,
    instance_name: str | None,
    instances: dict[str, str] | None = None,
) -> dict[FileState, int]:
    results: list[dict[FileState, int]] = [{} for _ in range(thread_count)]

    def _run(index: int, worker_id: str) -> None:
        adapter = adapter_factory()
        try:
            results[index] = batches.run_worker_loop(
                registry=registry,
                adapter=adapter,
                batch_id=batch_id,
                worker_id=worker_id,
                dry_run=dry_run,
                max_attempts=max_attempts,
                poll_interval_seconds=poll_interval_seconds,
                max_poll_minutes=max_poll_minutes,
                instance_name=instance_name,
                instances=instances,
            )
        finally:
            adapter.close()

    threads = [
        threading.Thread(target=_run, args=(i, f"auto-{i}"), name=f"migrate-worker-{i}")
        for i in range(thread_count)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    merged: dict[FileState, int] = {}
    for outcomes in results:
        for state, count in outcomes.items():
            merged[state] = merged.get(state, 0) + count
    return merged
