from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class PlatformSession:

    resource_group_id: str

@dataclass(frozen=True, slots=True)
class TargetDatabase:

    database_id: str
    database_name: str
    database_type: str
    server_name: str
    size_in_mb: float | None
