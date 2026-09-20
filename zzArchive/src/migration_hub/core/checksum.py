from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK_SIZE = 8 * 1024 * 1024

def sha256_file(path: Path, *, chunk_size: int = CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()

def files_match(source: Path, staged: Path) -> bool:
    if source.stat().st_size != staged.stat().st_size:
        return False
    return sha256_file(source) == sha256_file(staged)
