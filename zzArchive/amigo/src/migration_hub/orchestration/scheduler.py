from __future__ import annotations

import logging
import time
from collections.abc import Callable

from migration_hub.consumers import tasks

logger = logging.getLogger(__name__)

def run_forever(
    *,
    poll_interval_seconds: float,
    reap_interval_seconds: float,
    poll_fn: Callable[[], int] = tasks.poll_running_imports,
    reap_fn: Callable[[], int] = tasks.reap,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    max_ticks: int | None = None,
    on_result: Callable[[str, int], None] | None = None,
) -> None:
    now = clock()
    due_poll = now
    due_reap = now
    ticks = 0

    while max_ticks is None or ticks < max_ticks:
        now = clock()
        if now >= due_poll:
            _run_tick("poll_running_imports", poll_fn, on_result=on_result)
            due_poll = now + poll_interval_seconds
        if now >= due_reap:
            _run_tick("reap", reap_fn, on_result=on_result)
            due_reap = now + reap_interval_seconds

        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break

        next_due = min(due_poll, due_reap)
        wait = next_due - clock()
        if wait > 0:
            sleep(wait)

def _run_tick(
    name: str, fn: Callable[[], int], *, on_result: Callable[[str, int], None] | None
) -> None:
    try:
        result = fn()
    except Exception:
        logger.exception("scheduled task %r failed", name)
        return
    if on_result is not None:
        on_result(name, result)
