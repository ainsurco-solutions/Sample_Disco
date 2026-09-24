from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from migration_hub.adapters.databridge.client import DatabridgeAdapter
from migration_hub.core.registry import Registry
from migration_hub.core.states import BatchState, FileState
from migration_hub.orchestration import batches

_log = logging.getLogger(__name__)

IDLE_SECONDS = 15.0

class _Dial:

    def __init__(self, read: Callable[[], int], batch_id: str) -> None:
        self._read = read
        self._batch_id = batch_id
        self._lock = threading.Lock()
        self._last: int | None = None

    def __call__(self) -> int:
        now = max(1, int(self._read()))
        with self._lock:
            if self._last is not None and now != self._last:
                _log.info("%s workers %d -> %d", self._batch_id, self._last, now)
            self._last = now
        return now

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
    active_workers: Callable[[], int] | None = None,
    idle_seconds: float = IDLE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[FileState, int]:
    results: list[dict[FileState, int]] = [{} for _ in range(thread_count)]
    errors: list[BaseException] = []

    def _run(index: int, worker_id: str) -> None:
        try:
            _run_worker(index, worker_id)
        except BaseException as exc:
            errors.append(exc)

    dial = _Dial(active_workers, batch_id) if active_workers is not None else None

    def _run_worker(index: int, worker_id: str) -> None:
        adapter = adapter_factory()
        try:

            def loop(may_claim: Callable[[], bool] | None = None) -> dict[FileState, int]:
                return batches.run_worker_loop(
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
                    may_claim=may_claim,
                )

            if dial is None:
                results[index] = loop()
                return
            results[index] = _dialled(index, dial, loop)
        finally:
            adapter.close()

    def _dialled(
        index: int,
        current: Callable[[], int],
        loop: Callable[[Callable[[], bool] | None], dict[FileState, int]],
    ) -> dict[FileState, int]:
        total: dict[FileState, int] = {}
        while batches.state_of(registry=registry, batch_id=batch_id) is not BatchState.PAUSED:
            if index < current():
                for state, count in loop(lambda: index < current()).items():
                    total[state] = total.get(state, 0) + count
                if index < current():
                    break
                continue
            if not _claimable(registry, batch_id):
                break
            sleep(idle_seconds)
        return total

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

def _claimable(registry: Registry, batch_id: str) -> bool:
    return registry.counts_by_state(batch_id=batch_id)[FileState.VALIDATED] > 0
