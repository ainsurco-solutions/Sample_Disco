from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from migration_hub.core import checksum
from migration_hub.core.registry import Registry
from migration_hub.core.states import FileState

ROBOCOPY_FAILURE_THRESHOLD = 7

_ROBOCOPY = shutil.which("robocopy") or "robocopy"

class StagingError(RuntimeError):
    pass

def stage_file(
    *,
    source: Path,
    target: Path,
    expected_size: int,
    expected_checksum: str | None = None,
) -> None:
    if target.exists() and target.stat().st_size == expected_size:
        return

    target.parent.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            _ROBOCOPY,
            str(source.parent),
            str(target.parent),
            source.name,
            "/Z",
            "/J",
            "/NFL",
            "/NDL",
            "/NJH",
            "/NJS",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode > ROBOCOPY_FAILURE_THRESHOLD:
        raise StagingError(
            f"robocopy failed copying {source} -> {target.parent} "
            f"(exit code {result.returncode}):\n{result.stdout}\n{result.stderr}"
        )

    copied = target.parent / source.name
    if copied != target:
        copied.replace(target)

    actual_size = target.stat().st_size
    if actual_size != expected_size:
        raise StagingError(
            f"staged size mismatch for {target}: expected {expected_size}, got {actual_size}"
        )

    if expected_checksum is not None:
        actual_checksum = checksum.sha256_file(target)
        if actual_checksum != expected_checksum:
            raise StagingError(
                f"staged checksum mismatch for {target}: "
                f"expected {expected_checksum}, got {actual_checksum}"
            )

def stage_batch(
    *,
    registry: Registry,
    staging_root: Path,
    batch_id: str,
    stage_locally: bool = False,
) -> int:
    staged = 0
    for file in registry.outstanding(batch_id=batch_id):
        if FileState(file.state) is not FileState.VALIDATED:
            continue

        source = Path(file.source_path)
        if stage_locally:
            target = staging_root / batch_id / source.name
            stage_file(
                source=source,
                target=target,
                expected_size=file.size_bytes,
                expected_checksum=file.checksum,
            )
        else:
            target = source
            _verify_in_place(source, expected_size=file.size_bytes)

        registry.transition(
            file_id=file.file_id,
            to_state=FileState.STAGED,
            actor="stage_batch",
            staged_path=str(target),
        )
        staged += 1
    return staged

def _verify_in_place(source: Path, *, expected_size: int) -> None:
    if not source.exists():
        raise StagingError(f"source file no longer exists: {source}")
    actual_size = source.stat().st_size
    if actual_size != expected_size:
        raise StagingError(
            f"source size changed since discovery for {source}: "
            f"expected {expected_size}, got {actual_size} -- the backup may still "
            "be being written"
        )

def free_space_bytes(path: Path) -> int:
    existing = path
    while not existing.exists():
        if existing.parent == existing:
            raise FileNotFoundError(f"No existing ancestor found for {path}")
        existing = existing.parent
    return shutil.disk_usage(existing).free

def cleanup_completed(*, registry: Registry, staging_root: Path, batch_id: str) -> int:
    freed = 0
    for file in registry.completed_files(batch_id=batch_id):
        if file.staged_path is None:
            continue
        staged = Path(file.staged_path)
        if staged.exists() and _is_within(staged, staging_root):
            size = staged.stat().st_size
            staged.unlink()
            freed += size
    return freed

def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
