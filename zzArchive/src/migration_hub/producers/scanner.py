from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from migration_hub.core.models import MigrationFile
from migration_hub.core.registry import Registry
from migration_hub.core.states import FileState
from migration_hub.producers.naming import to_exposure_name

def scan(
    *, source_root: Path, pattern: str = "*.bak", max_files: int | None = None
) -> Iterator[MigrationFile]:
    count = 0
    for path in sorted(source_root.rglob(pattern)):
        if not path.is_file():
            continue
        if max_files is not None and count >= max_files:
            return

        stat = path.stat()
        source_database = path.stem
        yield MigrationFile(
            batch_id="",
            source_database=source_database,
            source_path=str(path),
            target_exposure_name=to_exposure_name(source_database),
            size_bytes=stat.st_size,
            source_last_write=datetime.fromtimestamp(stat.st_mtime, tz=UTC).replace(tzinfo=None),
            state=str(FileState.DISCOVERED),
            attempts=0,
        )
        count += 1

def register(
    *,
    registry: Registry,
    source_root: Path,
    batch_id: str,
    pattern: str = "*.bak",
    max_files: int | None = None,
    max_concurrency: int = 1,
) -> int:
    registry.ensure_batch(batch_id=batch_id, max_concurrency=max_concurrency)
    candidates = list(scan(source_root=source_root, pattern=pattern, max_files=max_files))
    for file in candidates:
        file.batch_id = batch_id
    return registry.register(candidates)
