from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
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
from migration_hub.core.retry import FailureAction, backoff_delay, classify, retry_call
from migration_hub.observability.audit import debug_timing_enabled, maybe_audited_call

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

def _part_done(
    on_part_done: Callable[[int, str], None] | None, part_number: int, etag: str
) -> None:
    if on_part_done is None:
        return
    try:
        on_part_done(part_number, etag)
    except Exception as exc:
        _log.warning("part %d not saved for resume: %s", part_number, exc)

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
    retry_attempts: int = 1,
    retry_base_seconds: float = 2.0,
    retry_max_seconds: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    size = source.stat().st_size

    def attempt() -> None:
        with source.open("rb") as handle:
            _put(
                url,
                _ProgressReader(
                    handle,
                    total=size,
                    label=source.name,
                    file_id=file_id,
                    on_progress=on_progress,
                ),
                source=source,
                part=None,
                size=size,
                engine=engine,
                file_id=file_id,
                headers=_PUT_HEADERS,
            )

    retry_call(
        attempt,
        attempts=retry_attempts,
        base_seconds=retry_base_seconds,
        max_seconds=retry_max_seconds,
        sleep=sleep,
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
    retry_attempts: int = 1,
    retry_base_seconds: float = 2.0,
    retry_max_seconds: float = 30.0,
    part_url_refresh_attempts: int = 1,
    sleep: Callable[[float], None] = time.sleep,
    read_ahead: bool = False,
    done: Mapping[int, str] | None = None,
    on_part_done: Callable[[int, str], None] | None = None,
) -> dict[int, str]:
    size = source.stat().st_size
    chunk = chunk_size if chunk_size is not None else multipart_chunksize(size)
    parts = max(1, -(-size // chunk))
    etags = held_prefix(done or {}, parts)
    first = len(etags) + 1
    if first > parts:
        return etags
    skipped = (first - 1) * chunk
    started = clock()
    sent = skipped
    if skipped:
        _report(on_progress, sent)

    debug_timing = debug_timing_enabled()
    with source.open("rb") as handle:
        if skipped:
            handle.seek(skipped)
        with _chunk_reader(handle, chunk, read_ahead=read_ahead, timed=debug_timing) as next_chunk:
            part_number = first
            while True:
                data, read_ms, wait_ms = next_chunk()
                if not data:
                    break
                etag = _put_part_with_url_refresh(
                    get_part_url,
                    data,
                    source=source,
                    part_number=part_number,
                    engine=engine,
                    file_id=file_id,
                    retry_attempts=retry_attempts,
                    retry_base_seconds=retry_base_seconds,
                    retry_max_seconds=retry_max_seconds,
                    part_url_refresh_attempts=part_url_refresh_attempts,
                    sleep=sleep,
                )
                etags[part_number] = etag
                _part_done(on_part_done, part_number, etag)
                sent += len(data)
                del data
                if debug_timing:
                    _log.info(
                        "file %s part %d read_ms=%.0f%s",
                        file_id if file_id is not None else "-",
                        part_number,
                        read_ms,
                        f" wait_ms={wait_ms:.0f}" if wait_ms is not None else "",
                    )
                _log_part(
                    file_id,
                    source.name,
                    part_number,
                    parts,
                    sent,
                    size,
                    clock() - started,
                    first=first,
                    skipped=skipped,
                )
                _report(on_progress, sent)
                part_number += 1

    return etags

def held_prefix(done: Mapping[int, str], parts: int) -> dict[int, str]:
    held: dict[int, str] = {}
    part = 1
    while part <= parts and done.get(part):
        held[part] = done[part]
        part += 1
    return held

_Chunk = tuple[bytes, float | None, float | None]

def _read(handle: IO[bytes], size: int, timed: bool) -> tuple[bytes, float | None]:
    if not timed:
        return handle.read(size), None
    started = time.monotonic()
    data = handle.read(size)
    return data, (time.monotonic() - started) * 1000

@contextmanager
def _chunk_reader(
    handle: IO[bytes], size: int, *, read_ahead: bool, timed: bool
) -> Iterator[Callable[[], _Chunk]]:
    if not read_ahead:

        def sequential() -> _Chunk:
            data, read_ms = _read(handle, size, timed)
            return data, read_ms, None

        yield sequential
        return

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="read-ahead") as pool:
        pending: Future[tuple[bytes, float | None]] = pool.submit(_read, handle, size, timed)

        def ahead() -> _Chunk:
            nonlocal pending
            waited = time.monotonic() if timed else 0.0
            data, read_ms = pending.result()
            wait_ms = (time.monotonic() - waited) * 1000 if timed else None
            if data:
                pending = pool.submit(_read, handle, size, timed)
            return data, read_ms, wait_ms

        yield ahead

def _put_part_with_url_refresh(
    get_part_url: Callable[[int], str],
    data: bytes,
    *,
    source: Path,
    part_number: int,
    engine: Engine | None,
    file_id: int | None,
    retry_attempts: int,
    retry_base_seconds: float,
    retry_max_seconds: float,
    part_url_refresh_attempts: int,
    sleep: Callable[[float], None],
) -> str:
    part_url = get_part_url(part_number)
    for refresh_attempt in range(1, part_url_refresh_attempts + 1):

        def put_this_part(url: str = part_url) -> httpx.Response:
            return _put(
                url,
                data,
                source=source,
                part=part_number,
                size=len(data),
                engine=engine,
                file_id=file_id,
                headers=_PUT_HEADERS,
            )

        try:
            response = retry_call(
                put_this_part,
                attempts=retry_attempts,
                base_seconds=retry_base_seconds,
                max_seconds=retry_max_seconds,
                sleep=sleep,
            )
        except UploadCredentialsExpiredError:
            if refresh_attempt >= part_url_refresh_attempts:
                raise
            part_url = get_part_url(part_number)
            continue
        except Exception as exc:
            if (
                classify(exc).action is not FailureAction.RETRY
                or refresh_attempt >= part_url_refresh_attempts
            ):
                raise
            sleep(backoff_delay(1, base_seconds=retry_max_seconds, max_seconds=retry_max_seconds))
            part_url = get_part_url(part_number)
            continue
        etag = str(response.headers.get("ETag", "")).strip('"')
        if not etag:
            raise MissingPartETagError(part_number)
        return etag
    raise AssertionError("unreachable: loop always returns or raises")

def _log_part(
    file_id: int | None,
    name: str,
    part: int,
    parts: int,
    sent: int,
    size: int,
    elapsed: float,
    *,
    first: int = 1,
    skipped: int = 0,
) -> None:
    mb = 1024 * 1024
    left = ""
    if part >= first + 1 and part < parts and sent > skipped:
        remaining = elapsed * (size - sent) / (sent - skipped)
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
