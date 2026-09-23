from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from migration_hub.adapters.databridge.client import DatabridgeJob

SOURCE = "GET /databridge/v1/sql-instances/{instance}/Jobs"

_QUEUED = {"enqueued"}
_RUNNING = {"processing"}
_FAILED = {"failed"}
_FINISHED = {"done", "succeeded", "cancelled", "deleted", "unavailable"}

_GROUPS = {"import database": "import", "archive database": "archive"}

class Recommendation(StrEnum):
    PUSH = "PUSH"
    HOLD = "HOLD"
    INVESTIGATE = "INVESTIGATE"
    UNKNOWN = "UNKNOWN"

@dataclass(frozen=True, slots=True)
class Thresholds:
    hold_active_imports: int = 10
    investigate_after_minutes: int = 360
    investigate_failures_24h: int = 5

@dataclass(slots=True)
class InstanceQueue:

    instance: str
    observed_at: datetime
    recommendation: Recommendation
    reasons: list[str] = field(default_factory=list)
    read_ok: bool = True
    error: str | None = None
    total_jobs: int = 0
    queued: dict[str, int] = field(default_factory=dict)
    running: dict[str, int] = field(default_factory=dict)
    ours_active: int = 0
    oldest_active_minutes: int | None = None
    failed_last_24h: int = 0
    unrecognised_statuses: dict[str, int] = field(default_factory=dict)
    source: str = SOURCE

    @property
    def active_imports(self) -> int:
        return self.queued.get("import", 0) + self.running.get("import", 0)

    @property
    def active(self) -> int:
        return sum(self.queued.values()) + sum(self.running.values())

def _group(job_type: str) -> str:
    return _GROUPS.get(job_type.strip().lower(), "other")

def summarise(
    instance: str,
    jobs: Iterable[DatabridgeJob],
    *,
    our_job_ids: Iterable[str],
    now: datetime,
    thresholds: Thresholds,
) -> InstanceQueue:
    ours = {job_id.lower() for job_id in our_job_ids}
    queued: Counter[str] = Counter()
    running: Counter[str] = Counter()
    unrecognised: Counter[str] = Counter()
    ours_active = 0
    failed_24h = 0
    oldest: datetime | None = None
    total = 0
    since = now - timedelta(hours=24)

    for job in jobs:
        total += 1
        status = job.status.strip().lower()
        if status in _QUEUED or status in _RUNNING:
            (queued if status in _QUEUED else running)[_group(job.type)] += 1
            if job.id.lower() in ours:
                ours_active += 1
            if job.create_time is not None and (oldest is None or job.create_time < oldest):
                oldest = job.create_time
        elif status in _FAILED:
            when = job.end_time or job.create_time
            if when is not None and when >= since:
                failed_24h += 1
        elif status not in _FINISHED:
            unrecognised[job.status] += 1

    summary = InstanceQueue(
        instance=instance,
        observed_at=now,
        recommendation=Recommendation.PUSH,
        total_jobs=total,
        queued=dict(queued),
        running=dict(running),
        ours_active=ours_active,
        oldest_active_minutes=(
            None if oldest is None else max(0, int((now - oldest).total_seconds() // 60))
        ),
        failed_last_24h=failed_24h,
        unrecognised_statuses=dict(unrecognised),
    )
    summary.recommendation, summary.reasons = recommend(summary, thresholds)
    return summary

def recommend(summary: InstanceQueue, thresholds: Thresholds) -> tuple[Recommendation, list[str]]:
    if not summary.read_ok:
        return Recommendation.UNKNOWN, [f"the job list could not be read: {summary.error}"]

    investigate: list[str] = []
    if (
        summary.oldest_active_minutes is not None
        and summary.oldest_active_minutes > thresholds.investigate_after_minutes
    ):
        investigate.append(
            f"oldest active job is {summary.oldest_active_minutes} min old "
            f"(over {thresholds.investigate_after_minutes})"
        )
    if summary.failed_last_24h >= thresholds.investigate_failures_24h:
        investigate.append(
            f"{summary.failed_last_24h} job(s) failed in the last 24 h "
            f"(threshold {thresholds.investigate_failures_24h})"
        )
    if summary.unrecognised_statuses:
        names = ", ".join(sorted(summary.unrecognised_statuses))
        investigate.append(f"status(es) nobody has classified: {names}")
    if investigate:
        return Recommendation.INVESTIGATE, investigate

    if summary.active_imports >= thresholds.hold_active_imports:
        return Recommendation.HOLD, [
            f"{summary.active_imports} import(s) queued or running "
            f"(limit {thresholds.hold_active_imports})"
        ]
    return Recommendation.PUSH, [
        f"{summary.active_imports} import(s) queued or running "
        f"(limit {thresholds.hold_active_imports})"
    ]

def unknown(instance: str, *, error: str, now: datetime) -> InstanceQueue:
    summary = InstanceQueue(
        instance=instance,
        observed_at=now,
        recommendation=Recommendation.UNKNOWN,
        read_ok=False,
        error=error,
    )
    summary.recommendation, summary.reasons = recommend(summary, Thresholds())
    return summary

@dataclass(slots=True)
class Snapshot:

    written_at: datetime
    ttl_minutes: int
    instances: list[InstanceQueue]

    def fresh_until(self) -> datetime:
        return self.written_at + timedelta(minutes=self.ttl_minutes)

def _to_json(snapshot: Snapshot) -> str:
    def instance(q: InstanceQueue) -> dict[str, object]:
        data = asdict(q)
        data["observed_at"] = q.observed_at.isoformat()
        data["recommendation"] = str(q.recommendation)
        return data

    return json.dumps(
        {
            "written_at": snapshot.written_at.isoformat(),
            "ttl_minutes": snapshot.ttl_minutes,
            "instances": [instance(q) for q in snapshot.instances],
        },
        indent=2,
    )

def read_snapshot(path: Path) -> Snapshot | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        instances = []
        for item in raw["instances"]:
            item = dict(item)
            item["observed_at"] = datetime.fromisoformat(item["observed_at"])
            item["recommendation"] = Recommendation(item["recommendation"])
            instances.append(InstanceQueue(**item))
        return Snapshot(
            written_at=datetime.fromisoformat(raw["written_at"]),
            ttl_minutes=int(raw["ttl_minutes"]),
            instances=instances,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None

def write_snapshot(path: Path, snapshot: Snapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_to_json(snapshot), encoding="utf-8")
    os.replace(tmp, path)

class TooSoonError(RuntimeError):

    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        super().__init__(
            f"last read at {snapshot.written_at:%H:%M:%S} UTC; the next is allowed "
            f"from {snapshot.fresh_until():%H:%M:%S} UTC ({snapshot.ttl_minutes}-minute limit)"
        )

def run_check(
    *,
    instances: Sequence[str],
    fetch: Callable[[str], Iterable[DatabridgeJob]],
    our_job_ids: Iterable[str],
    snapshot_path: Path,
    now: datetime,
    ttl_minutes: int,
    thresholds: Thresholds,
) -> Snapshot:
    previous = read_snapshot(snapshot_path)
    if previous is not None and now < previous.fresh_until():
        raise TooSoonError(previous)

    ids = list(our_job_ids)
    summaries: list[InstanceQueue] = []
    for name in instances:
        try:
            jobs = list(fetch(name))
        except Exception as exc:
            summaries.append(unknown(name, error=_short(exc), now=now))
            continue
        summaries.append(summarise(name, jobs, our_job_ids=ids, now=now, thresholds=thresholds))

    snapshot = Snapshot(written_at=now, ttl_minutes=ttl_minutes, instances=summaries)
    write_snapshot(snapshot_path, snapshot)
    return snapshot

def _short(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 200 else text[:197] + "..."
