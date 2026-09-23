from __future__ import annotations

from urllib.parse import urlsplit

_ID_SEGMENTS: dict[str, tuple[str, ...]] = {
    "sql-instances": ("{instance}",),
    "databases": ("{database}",),
    "jobs": ("{job}",),
    "upload-part": ("{upload}", "{part}"),
    "entitlements": ("{entitlement}",),
}

_VENDOR_ROOTS = frozenset({"databridge", "platform"})

STORAGE_UPLOAD_KEY = "(presigned storage upload)"

def endpoint_key(url: str) -> str:
    parts = [part for part in urlsplit(url).path.split("/") if part]
    if not parts or parts[0].lower() not in _VENDOR_ROOTS:
        return STORAGE_UPLOAD_KEY

    templated: list[str] = []
    i = 0
    while i < len(parts):
        templated.append(parts[i])
        for placeholder in _ID_SEGMENTS.get(parts[i].lower(), ()):
            if i + 1 >= len(parts):
                break
            templated.append(placeholder)
            i += 1
        i += 1
    return "/" + "/".join(templated)
