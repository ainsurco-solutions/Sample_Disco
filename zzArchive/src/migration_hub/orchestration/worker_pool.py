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
    group_ids: list[str] | None = None,
    archive_after_bridge: bool = False,
    max_archive_attempts: int | None = None,
) -> dict[FileState, int]:
    results: list[dict[FileState, int]] = [{} for _ in range(thread_count)]
    errors: list[BaseException] = []

    def _run(index: int, worker_id: str) -> None:
        try:
            _run_worker(index, worker_id)
        except BaseException as exc:
            errors.append(exc)

    def _run_worker(index: int, worker_id: str) -> None:
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
                group_ids=group_ids,
                archive_after_bridge=archive_after_bridge,
                max_archive_attempts=max_archive_attempts,
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
    if errors:
        raise errors[0]

    merged: dict[FileState, int] = {}
    for outcomes in results:
        for state, count in outcomes.items():
            merged[state] = merged.get(state, 0) + count
    return merged
