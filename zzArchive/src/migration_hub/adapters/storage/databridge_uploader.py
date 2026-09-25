from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterator
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

_PUT_HEADERS: dict[str, str] = {"Content-Type": "application/octet-stream"}

_REQUEST_FAULT_CODES = frozenset({"SignatureDoesNotMatch"})

_XML_CODE = re.compile(r"<Code>([^<]{1,80})</Code>")

_log = logging.getLogger(__name__)

PROGRESS_EVERY_SECONDS = 60.0

REPORT_EVERY_SECONDS = 10.0

ProgressCallback = Callable[[int], None]

def _report(on_progress: ProgressCallback | None, sent: int) -> None:
    if on_progress is None:
        return
    try:
        on_progress(sent)
    except Exception as exc:
        _log.warning("upload progress not recorded: %s", exc)

class _ProgressReader:

    def __init__(
        self,
        handle: IO[bytes],
        *,
        total: int,
        label: str,
        file_id: int | None,
        every_seconds: float = PROGRESS_EVERY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        on_progress: ProgressCallback | None = None,
        report_every_seconds: float = REPORT_EVERY_SECONDS,
    ) -> None:
        self._handle = handle
        self._total = total
        self._label = label
        self._who = f"file {file_id}" if file_id is not None else "no file"
        self._every = every_seconds
        self._clock = clock
        self._sent = 0
        self._next = clock() + every_seconds
        self._on_progress = on_progress
        self._report_every = report_every_seconds
        self._next_report = clock() + report_every_seconds

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        self._sent += len(chunk)
        if chunk and self._on_progress is not None and self._clock() >= self._next_report:
            self._next_report = self._clock() + self._report_every
            _report(self._on_progress, self._sent)
        if chunk and self._clock() >= self._next:
            self._next = self._clock() + self._every
            mb = 1024 * 1024
            _log.info(
                "%s  PUT %s  %.0f/%.0f MB (%d%%)",
                self._who,
                self._label,
                self._sent / mb,
                self._total / mb,
                int(100 * self._sent / self._total) if self._total else 100,
            )
        return chunk

    def fileno(self) -> int:
        return self._handle.fileno()

    def __iter__(self) -> Iterator[bytes]:
        return iter(lambda: self.read(64 * 1024), b"")

def upload_via_presigned_url(
    *,
    source: Path,
    url: str,
    engine: Engine | None = None,
    file_id: int | None = None,
    on_progress: ProgressCallback | None = None,
) -> None:
    size = source.stat().st_size
    with source.open("rb") as handle:
        _put(
            url,
            _ProgressReader(
                handle, total=size, label=source.name, file_id=file_id, on_progress=on_progress
            ),
            source=source,
            part=None,
            size=size,
            engine=engine,
            file_id=file_id,
            headers=_PUT_HEADERS,
        )

def upload_multipart(
    *,
    source: Path,
    get_part_url: Callable[[int], str],
    chunk_size: int | None = None,
    engine: Engine | None = None,
    file_id: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    on_progress: ProgressCallback | None = None,
) -> dict[int, str]:
    size = source.stat().st_size
    chunk = chunk_size if chunk_size is not None else multipart_chunksize(size)
    parts = max(1, -(-size // chunk))
    started = clock()
    sent = 0

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
                headers=_PUT_HEADERS,
            )
            etag = response.headers.get("ETag", "").strip('"')
            if not etag:
                raise MissingPartETagError(part_number)
            etags[part_number] = etag
            sent += len(data)
            _log_part(file_id, source.name, part_number, parts, sent, size, clock() - started)
            _report(on_progress, sent)
            part_number += 1

    return etags

def _log_part(
    file_id: int | None,
    name: str,
    part: int,
    parts: int,
    sent: int,
    size: int,
    elapsed: float,
) -> None:
    mb = 1024 * 1024
    left = ""
    if part >= 2 and part < parts and sent:
        remaining = elapsed * (size - sent) / sent
        left = (
            f"  ~{remaining / 60:.0f} min left"
            if remaining >= 60
            else f"  ~{remaining:.0f} s left"
        )
    _log.info(
        "%s  part %d/%d uploaded  %s/%s MB (%d%%)%s  %s",
        f"file {file_id}" if file_id is not None else "no file",
        part,
        parts,
        f"{sent / mb:,.0f}",
        f"{size / mb:,.0f}",
        int(100 * sent / size) if size else 100,
        left,
        name,
    )

def _put(
    url: str,
    content: bytes | IO[bytes] | _ProgressReader,
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
    if status < 400:
        return
    match = _XML_CODE.search(response.text)
    code = match.group(1) if match else None
    said = f" ({code})" if code else ""
    if status == 403 and code in _REQUEST_FAULT_CODES:
        raise UploadRejectedError(
            f"Presigned PUT for {label} returned 403{said} -- the request does not "
            "match what the URL was signed for (a header or the body). A fresh URL "
            "would be refused the same way; this is a fault in the upload code."
        )
    if status == 403:
        raise UploadCredentialsExpiredError(
            f"Presigned PUT for {label} returned 403{said} -- the URL most likely "
            "expired. Request a fresh upload target rather than retrying."
        )
    if status >= 500:
        raise UploadServerError(f"Presigned PUT for {label} returned {status}{said}")
    raise UploadRejectedError(f"Presigned PUT for {label} returned {status}{said}")
