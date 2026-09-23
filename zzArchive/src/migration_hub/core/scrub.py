from __future__ import annotations

import re
from typing import Final

REDACTED_QUERY: Final[str] = "?<redacted>"

_SIGNATURE_MARKERS: Final[tuple[str, ...]] = (
    "X-Amz-Signature=",
    "X-Amz-Credential=",
    "X-Amz-Security-Token=",
    "Signature=",
    "AWSAccessKeyId=",
)

_URL: Final[re.Pattern[str]] = re.compile(r"https?://[^\s'\"<>]+")

def _scrub_url(match: re.Match[str]) -> str:
    url = match.group(0)
    base, sep, query = url.partition("?")
    if sep and any(marker in query for marker in _SIGNATURE_MARKERS):
        return base + REDACTED_QUERY
    return url

def scrub_credentials(text: str) -> str:
    return _URL.sub(_scrub_url, text)

def scrub_optional(text: str | None) -> str | None:
    return None if text is None else scrub_credentials(text)
