from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

MAX_NAME_LENGTH = 120

_ILLEGAL_CHARS = re.compile(r"[^A-Za-z0-9_]")
_VALID_NAME = re.compile(r"^[A-Za-z0-9_]+$")

def to_exposure_name(source_database: str) -> str:
    sanitized = _ILLEGAL_CHARS.sub("_", source_database)
    return sanitized[:MAX_NAME_LENGTH]

def find_collisions(names: Iterable[str]) -> dict[str, list[str]]:
    by_target: dict[str, list[str]] = defaultdict(list)
    for name in names:
        by_target[to_exposure_name(name)].append(name)
    return {target: sources for target, sources in by_target.items() if len(sources) > 1}

def is_valid(name: str) -> bool:
    return bool(name) and len(name) <= MAX_NAME_LENGTH and _VALID_NAME.fullmatch(name) is not None
