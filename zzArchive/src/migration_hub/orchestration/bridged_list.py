from __future__ import annotations

import csv
import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from migration_hub.adapters.databridge.client import DatabridgeAdapter
from migration_hub.core.models import MigrationFile
from migration_hub.core.registry import Registry
from migration_hub.core.states import BatchDestination, FileState

SOURCE_SCHEME = "databridge://"

class Outcome(StrEnum):

    DATA_VAULT = "DATA_VAULT"
    DATA_BRIDGE = "DATA_BRIDGE"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    ALREADY_REGISTERED = "ALREADY_REGISTERED"
    CHECK_FAILED = "CHECK_FAILED"

    @property
    def registers(self) -> bool:
        return self in (Outcome.DATA_VAULT, Outcome.DATA_BRIDGE)

@dataclass(frozen=True, slots=True)
class ListEntry:
    database_name: str
    instance_name: str | None = None

@dataclass(frozen=True, slots=True)
class Finding:
    entry: ListEntry
    outcome: Outcome
    instance_name: str | None = None
    detail: str = ""

class ListFormatError(ValueError):
    pass

def parse_list(text: str) -> list[ListEntry]:
    entries: dict[str, ListEntry] = {}
    for number, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        cells = [c.strip() for c in row]
        if not cells or not cells[0] or cells[0].startswith("#"):
            continue
        if number == 1 and cells[0].lower() == "database_name":
            continue
        if len(cells) > 2 and any(cells[2:]):
            raise ListFormatError(f"line {number}: expected database_name[,instance_name]")
        entry = ListEntry(cells[0], (cells[1] if len(cells) > 1 else "") or None)
        previous = entries.get(entry.database_name.lower())
        if previous is None:
            entries[entry.database_name.lower()] = entry
        elif previous.instance_name != entry.instance_name:
            raise ListFormatError(
                f"line {number}: {entry.database_name!r} is listed twice with different instances"
            )
    if not entries:
        raise ListFormatError("the list has no database names")
    return list(entries.values())

def check_list(
    entries: Sequence[ListEntry],
    *,
    registry: Registry,
    adapter: DatabridgeAdapter,
    instances: Sequence[str],
) -> list[Finding]:
    taken = registry.taken_exposure_names(names=[e.database_name for e in entries])
    wanted = sorted(set(instances) | {e.instance_name for e in entries if e.instance_name})
    listed: dict[str, set[str] | Exception] = {}
    for instance in wanted:
        try:
            listed[instance] = adapter.list_databases(instance_name=instance)
        except Exception as exc:
            listed[instance] = exc

    findings: list[Finding] = []
    for entry in entries:
        if entry.database_name in taken:
            findings.append(
                Finding(entry, Outcome.ALREADY_REGISTERED, detail=taken[entry.database_name])
            )
            continue
        findings.append(_locate(entry, adapter=adapter, listed=listed, instances=instances))
    return findings

def _locate(
    entry: ListEntry,
    *,
    adapter: DatabridgeAdapter,
    listed: dict[str, set[str] | Exception],
    instances: Sequence[str],
) -> Finding:
    try:
        if adapter.archive_exists(database_name=entry.database_name):
            return Finding(entry, Outcome.DATA_VAULT)
    except Exception as exc:
        return Finding(entry, Outcome.CHECK_FAILED, detail=f"Data Vault: {_why(exc)}")

    hits: list[str] = []
    for instance in [entry.instance_name] if entry.instance_name else list(instances):
        names = listed.get(instance)
        if isinstance(names, Exception):
            return Finding(entry, Outcome.CHECK_FAILED, detail=f"{instance}: {_why(names)}")
        if names is not None and entry.database_name in names:
            hits.append(instance)
    if len(hits) == 1:
        return Finding(entry, Outcome.DATA_BRIDGE, instance_name=hits[0])
    if hits:
        return Finding(entry, Outcome.AMBIGUOUS, detail=", ".join(hits))
    return Finding(entry, Outcome.NOT_FOUND)

def _why(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"

def register_findings(
    findings: Sequence[Finding],
    *,
    registry: Registry,
    batch_id: str,
    actor: str,
    max_concurrency: int = 1,
    now: Callable[[], datetime] = lambda: datetime.now(UTC).replace(tzinfo=None),
) -> int:
    existing = registry.get_batch(batch_id)
    if existing is not None and existing.destination != str(BatchDestination.VAULT):
        raise ValueError(
            f"batch {batch_id!r} goes to Data Bridge only; a list batch goes to Data Vault"
        )
    registry.ensure_batch(
        batch_id=batch_id, max_concurrency=max_concurrency, destination=BatchDestination.VAULT
    )
    rows: list[tuple[MigrationFile, str]] = []
    for finding in findings:
        if not finding.outcome.registers:
            continue
        name = finding.entry.database_name
        in_vault = finding.outcome is Outcome.DATA_VAULT
        instance = finding.instance_name or finding.entry.instance_name
        file = MigrationFile(
            batch_id=batch_id,
            source_database=name,
            source_path=f"{SOURCE_SCHEME}{instance or 'data-vault'}/{name}",
            target_exposure_name=name,
            database_name=name,
            instance_name=instance,
            size_bytes=0,
            state=str(FileState.COMPLETED if in_vault else FileState.BRIDGED),
            archived_at=now() if in_vault else None,
        )
        detail = (
            "registered from list: already in Data Vault"
            if in_vault
            else f"registered from list: found on Data Bridge {instance}, archive only"
        )
        rows.append((file, detail))
    return registry.register_found(rows, actor=actor)
