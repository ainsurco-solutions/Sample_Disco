from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from migration_hub.adapters.base import TargetDatabase

class _SearchesDatabases(Protocol):

    def search_databases(self) -> list[TargetDatabase]: ...

_FIELDNAMES = [
    "database_id",
    "database_name",
    "database_type",
    "server_name",
    "size_in_mb",
    "extracted_at",
]

def extract_target_list(adapter: _SearchesDatabases, *, output_path: Path) -> Path:
    databases = adapter.search_databases()
    extracted_at = datetime.now(UTC).isoformat()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDNAMES)
        writer.writeheader()
        for database in databases:
            writer.writerow(
                {
                    "database_id": database.database_id,
                    "database_name": database.database_name,
                    "database_type": database.database_type,
                    "server_name": database.server_name,
                    "size_in_mb": database.size_in_mb,
                    "extracted_at": extracted_at,
                }
            )

    return output_path
