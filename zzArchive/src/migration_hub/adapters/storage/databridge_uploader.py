from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import IO

import httpx
from sqlalchemy.engine import Engine

from migration_hub.adapters.databridge.errors import (
    MissingPartETagError,
    UploadCredentialsExpiredError,
    UploadInterruptedError,
    UploadRejectedError,
    UploadServerError,
)
from migration_hub.adapters.storage.s3_uploader import multipart_chunksize
from migration_hub.observability.audit import maybe_audited_call

_UPLOAD_TIMEOUT_SECONDS = 3600.0

def upload_via_presigned_url(
    *, source: Path, url: str, engine: Engine | None = None, file_id: int | None = None
) -> None:
    size = source.stat().st_size
    with source.open("rb") as handle:
        _put(url, handle, source=source, part=None, size=size, engine=engine, file_id=file_id)

def upload_multipart(
    *,
    source: Path,
    get_part_url: Callable[[int], str],
    chunk_size: int | None = None,
    engine: Engine | None = None,
    file_id: int | None = None,
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
            response = _put(
                part_url,
                data,
                source=source,
                part=part_number,
                size=len(data),
                engine=engine,
                file_id=file_id,
                headers={"Content-Type": "application/octet-stream"},
            )
            etag = response.headers.get("ETag", "").strip('"')
            if not etag:
                raise MissingPartETagError(part_number)
            etags[part_number] = etag
            part_number += 1

    return etags

def _put(
    url: str,
    content: bytes | IO[bytes],
    *,
    source: Path,
    part: int | None,
    size: int,
    engine: Engine | None,
    file_id: int | None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    label = source.name if part is None else f"{source.name} part {part}"
    with maybe_audited_call(engine=engine, file_id=file_id, method="PUT", url=url) as call:
        call.request_body = {"file": source.name, "part": part, "bytes": size}
        try:
            response = httpx.put(
                url, content=content, headers=headers, timeout=_UPLOAD_TIMEOUT_SECONDS
            )
        except httpx.TransportError as exc:
            raise UploadInterruptedError(
                f"PUT of {label} got no response: {type(exc).__name__}"
            ) from exc
        call.status_code = response.status_code
        call.response_body = (
            {"etag": response.headers.get("ETag")} if response.is_success else response.text[:2000]
        )
    _raise_for_upload_status(response, label=label)
    return response

def _raise_for_upload_status(response: httpx.Response, *, label: str) -> None:
    status = response.status_code
    if status == 403:
        raise UploadCredentialsExpiredError(
            f"Presigned PUT for {label} returned 403 -- the URL most likely "
            "expired. Request a fresh upload target rather than retrying."
        )
    if status >= 500:
        raise UploadServerError(f"Presigned PUT for {label} returned {status}")
    if status >= 400:
        raise UploadRejectedError(f"Presigned PUT for {label} returned {status}")
