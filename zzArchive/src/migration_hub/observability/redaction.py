from __future__ import annotations

import json
import re
from typing import Any, Final

SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "accesskeyid",
        "secretaccesskey",
        "sessiontoken",
        "presignparams",
        "authorization",
        "apikey",
        "api_key",
        "x-api-key",
        "password",
        "token",
    }
)

SENSITIVE_HEADERS: Final[frozenset[str]] = frozenset(
    {"authorization", "x-api-key", "cookie", "set-cookie"}
)

REDACTED: Final[str] = "<redacted>"

def redact_mapping(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: REDACTED if key.lower() in SENSITIVE_KEYS else redact_mapping(value)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [redact_mapping(item) for item in payload]
    return payload

def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: REDACTED if key.lower() in SENSITIVE_HEADERS else value
        for key, value in headers.items()
    }

_SENSITIVE_FIELD_PATTERN = re.compile(
    r'"(' + "|".join(re.escape(k) for k in SENSITIVE_KEYS) + r')"\s*:\s*"(?:[^"\\]|\\.)*"',
    re.IGNORECASE,
)

def redact_text(body: str) -> str:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return _SENSITIVE_FIELD_PATTERN.sub(lambda m: f'"{m.group(1)}": "{REDACTED}"', body)
    return json.dumps(redact_mapping(parsed))
