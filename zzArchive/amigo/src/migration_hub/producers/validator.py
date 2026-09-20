from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from migration_hub.core import checksum
from migration_hub.core.registry import Registry
from migration_hub.core.states import FileState
from migration_hub.producers import naming

@dataclass(frozen=True, slots=True)
class ValidationResult:
    ok: bool
    failures: tuple[str, ...] = ()
    checksum: str | None = None

def validate_source(
    *, path: Path, expected_size: int, target_name: str, compute_checksum: bool = False
) -> ValidationResult:
    failures: list[str] = []
    digest: str | None = None

    if not path.exists():
        failures.append(f"source file does not exist: {path}")
    elif not path.is_file():
        failures.append(f"source path is not a file: {path}")
    else:
        if path.suffix.lower() != ".bak":
            failures.append(f"unexpected extension: {path.suffix!r} (expected .bak)")

        actual_size = path.stat().st_size
        if actual_size == 0:
            failures.append("file is zero-length")
        elif actual_size != expected_size:
            failures.append(
                f"size mismatch: discovered {expected_size}, now {actual_size} "
                "(source changed since discovery)"
            )

        if compute_checksum:
            try:
                digest = checksum.sha256_file(path)
            except OSError as exc:
                failures.append(f"could not read file to compute checksum: {exc}")

    if not naming.is_valid(target_name):
        failures.append(f"invalid target exposure name: {target_name!r}")

    return ValidationResult(ok=not failures, failures=tuple(failures), checksum=digest)

def validate_batch(
    *, registry: Registry, batch_id: str, compute_checksum: bool = False
) -> dict[int, ValidationResult]:
    discovered = [
        f
        for f in registry.outstanding(batch_id=batch_id)
        if FileState(f.state) is FileState.DISCOVERED
    ]

    collisions = naming.find_collisions(f.target_exposure_name for f in discovered)

    results: dict[int, ValidationResult] = {}
    for file in discovered:
        result = validate_source(
            path=Path(file.source_path),
            expected_size=file.size_bytes,
            target_name=file.target_exposure_name,
            compute_checksum=compute_checksum,
        )
        if result.ok and file.target_exposure_name in collisions:
            sources = collisions[file.target_exposure_name]
            result = ValidationResult(
                ok=False,
                failures=(f"target name collision with: {', '.join(sources)}",),
            )

        results[file.file_id] = result
        if result.ok:
            registry.transition(
                file_id=file.file_id,
                to_state=FileState.VALIDATED,
                actor="validator",
                checksum=result.checksum,
            )
        else:
            registry.transition(
                file_id=file.file_id,
                to_state=FileState.REJECTED,
                actor="validator",
                detail="; ".join(result.failures),
            )

    return results
