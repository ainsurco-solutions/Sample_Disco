from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx

from migration_hub.adapters.databridge.errors import (
    MissingPartETagError,
    UploadCredentialsExpiredError,
)
from migration_hub.adapters.storage.s3_uploader import multipart_chunksize

_UPLOAD_TIMEOUT_SECONDS = 3600.0

def upload_via_presigned_url(*, source: Path, url: str) -> None:
    with source.open("rb") as handle:
        response = httpx.put(url, content=handle, timeout=_UPLOAD_TIMEOUT_SECONDS)
    _raise_for_upload_status(response, source=source)

def upload_multipart(
    *,
    source: Path,
    get_part_url: Callable[[int], str],
    chunk_size: int | None = None,
) -> dict[int, str]:
    size = source.stat().st_size
    chunk = chunk_size if chunk_size is not None else multipart_chunksize(size)

    etags: dict[int, str] = {}
    with source.open("rb") as handle:
        part_number = 1
        while True:
            data = handle.read(chunk)
            if not data:
                break
            part_url = get_part_url(part_number)
            response = httpx.put(
                part_url,
                content=data,
                headers={"Content-Type": "application/octet-stream"},
                timeout=_UPLOAD_TIMEOUT_SECONDS,
            )
            _raise_for_upload_status(response, source=source)
            etag = response.headers.get("ETag", "").strip('"')
            if not etag:
                raise MissingPartETagError(part_number)
            etags[part_number] = etag
            part_number += 1

    return etags

def _raise_for_upload_status(response: httpx.Response, *, source: Path) -> None:
    if response.status_code == 403:
        raise UploadCredentialsExpiredError(
            f"Presigned PUT for {source.name} returned 403 -- the URL most likely "
            "expired. Request a fresh upload target rather than retrying."
        )
    response.raise_for_status()
