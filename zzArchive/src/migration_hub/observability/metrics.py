from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

@dataclass(frozen=True, slots=True)
class ThroughputSnapshot:
    files_completed: int
    bytes_transferred: int
    window: timedelta

    @property
    def bytes_per_second(self) -> float:
        raise NotImplementedError

def throughput(*, batch_id: str | None = None, window_hours: int = 24) -> ThroughputSnapshot:
    raise NotImplementedError

def projected_completion(*, remaining_bytes: int) -> timedelta:
    raise NotImplementedError

def failure_rate(*, batch_id: str) -> float:
    raise NotImplementedError
