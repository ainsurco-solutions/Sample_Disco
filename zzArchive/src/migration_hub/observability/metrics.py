from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from migration_hub.core.models import ApiTransaction
from migration_hub.core.registry import Registry
from migration_hub.core.states import FileState
from migration_hub.observability.endpoints import STORAGE_UPLOAD_KEY as STORAGE_UPLOAD_KEY
from migration_hub.observability.endpoints import endpoint_key as endpoint_key

MIN_COMPLETIONS_FOR_ETA = 3

_MIN_MEASURED_SPAN = timedelta(minutes=1)

@dataclass(frozen=True, slots=True)
class ThroughputSnapshot:
    files_completed: int
    bytes_transferred: int
    window: timedelta
    batch_id: str | None = None

    @property
    def bytes_per_second(self) -> float:
        seconds = self.window.total_seconds()
        return self.bytes_transferred / seconds if seconds > 0 else 0.0

    @property
    def files_per_hour(self) -> float:
        hours = self.window.total_seconds() / 3600
        return self.files_completed / hours if hours > 0 else 0.0

@dataclass(frozen=True, slots=True)
class EndpointCallCount:
    endpoint: str
    calls: int
    errors: int

@dataclass(frozen=True, slots=True)
class ApiCallSummary:
    calls: int
    errors: int
    slow_calls: int
    window: timedelta
    slow_threshold_ms: int
    status_families: dict[str, int]
    top_endpoints: tuple[EndpointCallCount, ...]
    repeated_error_endpoints: tuple[EndpointCallCount, ...]
    batch_id: str | None = None

    @property
    def calls_per_minute(self) -> float:
        minutes = self.window.total_seconds() / 60
        return self.calls / minutes if minutes > 0 else 0.0

    @property
    def error_rate(self) -> float:
        return self.errors / self.calls if self.calls else 0.0

    @property
    def risk(self) -> Literal["normal", "watch", "investigate"]:
        if (
            self.calls_per_minute >= 180
            or (self.calls >= 20 and self.error_rate >= 0.25)
            or any(endpoint.errors >= 10 for endpoint in self.repeated_error_endpoints)
        ):
            return "investigate"
        if self.calls_per_minute >= 60 or (self.calls >= 10 and self.error_rate >= 0.10):
            return "watch"
        return "normal"

def throughput(
    *, registry: Registry, batch_id: str | None = None, window_hours: int = 24
) -> ThroughputSnapshot:
    window = timedelta(hours=window_hours)
    now = datetime.now(UTC).replace(tzinfo=None)
    since = now - window
    measured = registry.completed_transition_metrics_since(batch_id=batch_id, since=since)
    started = measured["first_started_at"]
    worked = window
    if isinstance(started, datetime):
        worked = min(window, max(now - started, _MIN_MEASURED_SPAN))
    return ThroughputSnapshot(
        files_completed=cast(int, measured["files_completed"]),
        bytes_transferred=cast(int, measured["bytes_transferred"]),
        window=worked,
        batch_id=batch_id,
    )

def projected_completion(*, remaining_bytes: int, snapshot: ThroughputSnapshot) -> timedelta | None:
    if remaining_bytes <= 0:
        return timedelta(0)
    if snapshot.files_completed < MIN_COMPLETIONS_FOR_ETA or snapshot.bytes_per_second <= 0:
        return None
    return timedelta(seconds=remaining_bytes / snapshot.bytes_per_second)

def failure_rate(*, registry: Registry, batch_id: str | None = None) -> float:
    counts = registry.counts_by_state(batch_id=batch_id)
    failed = (
        counts[FileState.FAILED] + counts[FileState.ABANDONED] + counts[FileState.ARCHIVE_FAILED]
    )
    attempted = registry.file_metrics(batch_id=batch_id)["attempted_count"]
    return failed / attempted if attempted else 0.0

def api_call_summary(
    *,
    registry: Registry,
    batch_id: str | None = None,
    window_minutes: int = 15,
    slow_threshold_ms: int = 30_000,
    top_n: int = 5,
) -> ApiCallSummary:
    window = timedelta(minutes=window_minutes)
    since = datetime.now(UTC).replace(tzinfo=None) - window
    transactions = registry.transactions_since(batch_id=batch_id, since=since)

    status_families: Counter[str] = Counter()
    endpoint_calls: Counter[str] = Counter()
    endpoint_errors: Counter[str] = Counter()
    errors = 0
    slow_calls = 0
    for transaction in transactions:
        family = status_family(transaction.status_code)
        status_families[family] += 1
        endpoint = endpoint_key(transaction.url)
        endpoint_calls[endpoint] += 1
        if _is_error(transaction):
            errors += 1
            endpoint_errors[endpoint] += 1
        if transaction.duration_ms is not None and transaction.duration_ms >= slow_threshold_ms:
            slow_calls += 1

    top_endpoints = tuple(
        EndpointCallCount(endpoint=e, calls=calls, errors=endpoint_errors[e])
        for e, calls in endpoint_calls.most_common(top_n)
    )
    repeated_error_endpoints = tuple(
        EndpointCallCount(endpoint=e, calls=endpoint_calls[e], errors=errors)
        for e, errors in endpoint_errors.most_common(top_n)
        if errors
    )
    return ApiCallSummary(
        calls=len(transactions),
        errors=errors,
        slow_calls=slow_calls,
        window=window,
        slow_threshold_ms=slow_threshold_ms,
        status_families=dict(status_families),
        top_endpoints=top_endpoints,
        repeated_error_endpoints=repeated_error_endpoints,
        batch_id=batch_id,
    )

def _is_error(transaction: ApiTransaction) -> bool:
    return transaction.status_code is not None and transaction.status_code >= 400

def status_family(status_code: int | None) -> str:
    if status_code is None:
        return "unknown"
    return f"{status_code // 100}xx"
