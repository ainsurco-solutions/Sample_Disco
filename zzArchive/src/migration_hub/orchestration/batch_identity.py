from __future__ import annotations

import hashlib
import re
from pathlib import Path, PureWindowsPath

_MAX_BATCH_ID_LENGTH = 64
_HASH_SUFFIX_LENGTH = 8
_MAX_SLUG_LENGTH = _MAX_BATCH_ID_LENGTH - _HASH_SUFFIX_LENGTH - 1

_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")

def derive_batch_id(source_root: Path) -> str:
    normalized = _normalize(source_root)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:_HASH_SUFFIX_LENGTH]
    slug = _slugify(PureWindowsPath(normalized).name) or "batch"
    return f"{slug[:_MAX_SLUG_LENGTH]}-{digest}"

def _normalize(source_root: Path) -> str:
    text = str(PureWindowsPath(str(source_root))).rstrip("\\").rstrip("/")
    return text.lower()

def _slugify(name: str) -> str:
    return _NON_SLUG_CHARS.sub("-", name.lower()).strip("-")
