from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy.engine import Engine

from migration_hub.adapters.base import PlatformSession
from migration_hub.adapters.databridge.errors import (
    AuthenticationError,
    DatabaseAlreadyExistsError,
    DatabridgeError,
    MissingJobIdError,
    MissingUploadUriError,
    RateLimitedError,
    RequestInterruptedError,
    RequestRejectedError,
    ServerError,
    UnknownJobStatusError,
)
from migration_hub.adapters.storage import databridge_uploader
from migration_hub.adapters.storage.s3_uploader import multipart_chunksize
from migration_hub.core.registry import Registry, SavedUpload
from migration_hub.core.retry import retry_call
from migration_hub.observability import audit
from migration_hub.observability.audit import audited_call

_log = logging.getLogger(__name__)

LARGE_FILE_THRESHOLD_BYTES = 5 * 1024**3

_FORMAT_CODES = {"mdf": 0, "bak": 1, "dacpac": 2}

_SUCCESS_STATUSES = {"done", "succeeded", "success"}
_FAILURE_STATUSES = {"failed", "cancelled", "deleted", "unavailable"}
_IN_PROGRESS_STATUSES = {"enqueued", "processing", "inprogress", "pending", "queued", "running"}
_KNOWN_STATUSES = _SUCCESS_STATUSES | _FAILURE_STATUSES | _IN_PROGRESS_STATUSES

@dataclass(frozen=True, slots=True)
class SqlInstance:

    name: str
    uid: str
    endpoint: str

@dataclass(frozen=True, slots=True)
class DatabridgeJobStatus:
    job_id: str
    raw_status: str
    is_terminal: bool
    is_success: bool

@dataclass(frozen=True, slots=True)
class DatabridgeJob:

    id: str
    type: str
    status: str
    resource_name: str
    create_time: datetime | None
    end_time: datetime | None
    owner: str
    resource_type: str

class DatabridgeAdapter:

    def __init__(
        self,
        *,
        host: str,
        api_key: str,
        engine: Engine | None = None,
        inline_retry_attempts: int = 1,
        inline_retry_base_seconds: float = 2.0,
        inline_retry_max_seconds: float = 30.0,
        part_url_refresh_attempts: int = 1,
        read_ahead: bool = False,
        multipart_threshold_bytes: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._host = host
        self._api_key = api_key
        self._engine = engine
        self._current_file_id: int | None = None
        self._client = httpx.Client(base_url=host, headers={"Authorization": api_key}, timeout=30.0)
        self._inline_retry_attempts = inline_retry_attempts
        self._inline_retry_base_seconds = inline_retry_base_seconds
        self._inline_retry_max_seconds = inline_retry_max_seconds
        self._part_url_refresh_attempts = part_url_refresh_attempts
        self._read_ahead = read_ahead
        self._multipart_threshold_bytes = multipart_threshold_bytes
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def open_session(self, *, entitlement: str = "RI-DATAVAULT") -> PlatformSession:
        response = self._request(
            "GET", f"/platform/tenantdata/v1/entitlements/{entitlement}/resourcegroups"
        )
        resource_groups = response.json()
        if not resource_groups:
            raise DatabridgeError(f"No resource groups returned for entitlement {entitlement!r}")
        resource_group_id = resource_groups[0]["resourceGroupId"]
        return PlatformSession(resource_group_id=resource_group_id)

    @contextmanager
    def bound_to_file(self, file_id: int | None) -> Iterator[None]:
        previous = self._current_file_id
        self._current_file_id = file_id
        try:
            yield
        finally:
            self._current_file_id = previous

    def list_sql_instances(self) -> list[SqlInstance]:
        response = self._request("GET", "/databridge/v1/sql-instances")
        return [
            SqlInstance(name=item["name"], uid=item["uid"], endpoint=item["endpoint"])
            for item in response.json()
        ]

    def upload_and_import(
        self,
        *,
        instance_name: str,
        database_name: str,
        file_extension: str,
        source: Path,
        group_ids: list[str] | None = None,
        on_progress: Callable[[int], None] | None = None,
    ) -> str:
        if file_extension not in _FORMAT_CODES:
            raise ValueError(f"unsupported file_extension: {file_extension!r}")
        format_code = _FORMAT_CODES[file_extension]
        file_size = source.stat().st_size
        threshold = min(
            self._multipart_threshold_bytes or LARGE_FILE_THRESHOLD_BYTES,
            LARGE_FILE_THRESHOLD_BYTES,
        )

        if file_size < threshold:
            self._upload_small(
                instance_name=instance_name,
                database_name=database_name,
                format_code=format_code,
                source=source,
                on_progress=on_progress,
            )
        else:
            self._upload_large(
                instance_name=instance_name,
                database_name=database_name,
                file_extension=file_extension,
                source=source,
                on_progress=on_progress,
            )

        return self._trigger_import(
            instance_name=instance_name,
            database_name=database_name,
            format_code=format_code,
            group_ids=group_ids if group_ids is not None else [],
        )

    def _upload_small(
        self,
        *,
        instance_name: str,
        database_name: str,
        format_code: int,
        source: Path,
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        response = self._request(
            "GET",
            f"/databridge/v1/sql-instances/{instance_name}/Databases/{database_name}/import",
            params={"importFrom": format_code},
        )
        body = response.json()
        upload_url = body.get("backupUri") or body.get("mdfUri")
        if not upload_url:
            raise MissingUploadUriError(sorted(body))
        databridge_uploader.upload_via_presigned_url(
            source=source,
            url=upload_url,
            engine=self._engine,
            file_id=self._current_file_id,
            on_progress=on_progress,
            retry_attempts=self._inline_retry_attempts,
            retry_base_seconds=self._inline_retry_base_seconds,
            retry_max_seconds=self._inline_retry_max_seconds,
            sleep=self._sleep,
        )

    def _upload_large(
        self,
        *,
        instance_name: str,
        database_name: str,
        file_extension: str,
        source: Path,
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        base = (
            f"/databridge/v1/sql-instances/{instance_name}/databases/"
            f"{database_name}/{file_extension}"
        )
        stat = source.stat()
        chunk = multipart_chunksize(stat.st_size)
        file_id = self._current_file_id
        registry = (
            Registry(self._engine) if self._engine is not None and file_id is not None else None
        )

        saved = self._resumable(
            registry, stat=stat, chunk=chunk, target=(instance_name, database_name, file_extension)
        )
        if saved is not None and registry is not None:
            self._record_resume(registry, base, saved, size=stat.st_size, chunk=chunk)
            try:
                self._send_multipart(
                    saved.upload_id,
                    registry,
                    base=base,
                    source=source,
                    chunk=chunk,
                    done=saved.etags,
                    on_progress=on_progress,
                )
                return
            except RequestRejectedError as exc:
                _log.warning(
                    "file %s saved uploadId refused (%d) -- starting a fresh upload",
                    file_id,
                    exc.status_code,
                )

        init_response = self._request("POST", f"{base}/init-upload")
        upload_id = init_response.json()["uploadId"]
        if registry is not None and file_id is not None:
            _quietly(
                "save the upload for resume",
                lambda: registry.save_upload(
                    file_id=file_id,
                    upload_id=upload_id,
                    chunk_bytes=chunk,
                    source_size=stat.st_size,
                    source_mtime_ns=stat.st_mtime_ns,
                    instance_name=instance_name,
                    database_name=database_name,
                    file_extension=file_extension,
                ),
            )
        self._send_multipart(
            upload_id,
            registry,
            base=base,
            source=source,
            chunk=chunk,
            done=None,
            on_progress=on_progress,
        )

    def _resumable(
        self,
        registry: Registry | None,
        *,
        stat: os.stat_result,
        chunk: int,
        target: tuple[str, str, str],
    ) -> SavedUpload | None:
        file_id = self._current_file_id
        if registry is None or file_id is None:
            return None
        saved = _quietly("read the saved upload", lambda: registry.saved_upload(file_id=file_id))
        if saved is None:
            return None
        then = (saved.instance_name, saved.database_name, saved.file_extension)
        changed = [
            what
            for what, now, before in (
                ("target", target, then),
                ("size", stat.st_size, saved.source_size),
                ("mtime", stat.st_mtime_ns, saved.source_mtime_ns),
                ("chunk size", chunk, saved.chunk_bytes),
            )
            if now != before
        ]
        if not changed:
            return saved
        _log.info(
            "file %s saved upload not resumed (%s changed) -- starting a fresh upload",
            file_id,
            ", ".join(changed),
        )
        _quietly("clear the saved upload", lambda: registry.clear_saved_upload(file_id=file_id))
        return None

    def _record_resume(
        self, registry: Registry, base: str, saved: SavedUpload, *, size: int, chunk: int
    ) -> None:
        parts = max(1, -(-size // chunk))
        held = len(databridge_uploader.held_prefix(saved.etags, parts))
        skipped = min(held * chunk, size)
        _log.info(
            "file %s resuming upload from part %d/%d, %s MB already on Data Bridge",
            self._current_file_id,
            held + 1,
            parts,
            f"{skipped / 1024**2:,.0f}",
        )
        file_id = self._current_file_id
        if file_id is not None:
            _quietly(
                "restart the progress figures",
                lambda: registry.record_upload_progress(
                    file_id=file_id, bytes_sent=skipped, start=True
                ),
            )
        engine = self._engine
        if engine is None:
            return
        _quietly(
            "record the resume",
            lambda: audit.record_call(
                engine=engine,
                file_id=self._current_file_id,
                method="RESUME",
                url=str(self._client.base_url.join(f"{base}/resume-upload")),
                request_body={
                    "upload_id": saved.upload_id,
                    "from_part": held + 1,
                    "parts": parts,
                    "bytes_skipped": skipped,
                },
            ),
        )

    def _send_multipart(
        self,
        upload_id: str,
        registry: Registry | None,
        *,
        base: str,
        source: Path,
        chunk: int,
        done: dict[int, str] | None,
        on_progress: Callable[[int], None] | None,
    ) -> None:
        file_id = self._current_file_id

        def get_part_url(part_number: int) -> str:
            part_response = self._request("GET", f"{base}/upload-part/{upload_id}/{part_number}")
            try:
                return str(part_response.json())
            except ValueError:
                return part_response.text.strip()

        def save_part(part_number: int, etag: str) -> None:
            if registry is not None and file_id is not None:
                registry.record_upload_part(file_id=file_id, part_number=part_number, etag=etag)

        try:
            etags = databridge_uploader.upload_multipart(
                source=source,
                get_part_url=get_part_url,
                chunk_size=chunk,
                engine=self._engine,
                file_id=file_id,
                on_progress=on_progress,
                retry_attempts=self._inline_retry_attempts,
                retry_base_seconds=self._inline_retry_base_seconds,
                retry_max_seconds=self._inline_retry_max_seconds,
                part_url_refresh_attempts=self._part_url_refresh_attempts,
                sleep=self._sleep,
                read_ahead=self._read_ahead,
                done=done,
                on_part_done=save_part,
            )
            self._request(
                "POST",
                f"{base}/complete-upload",
                json={"uploadId": upload_id, "etags": {str(n): etags[n] for n in sorted(etags)}},
            )
        except RequestRejectedError:
            self._forget_upload(registry)
            raise
        self._forget_upload(registry)

    def _forget_upload(self, registry: Registry | None) -> None:
        file_id = self._current_file_id
        if registry is not None and file_id is not None:
            _quietly("clear the saved upload", lambda: registry.clear_saved_upload(file_id=file_id))

    def _trigger_import(
        self, *, instance_name: str, database_name: str, format_code: int, group_ids: list[str]
    ) -> str:
        response = self._request(
            "POST",
            f"/databridge/v1/sql-instances/{instance_name}/Databases/{database_name}/import",
            params={"importFrom": format_code},
            headers={"accept": "application/json", "content-type": "application/json"},
            json={"groupIds": group_ids},
        )
        location = response.headers.get("Location")
        if location:
            return str(location).rstrip("/").rsplit("/", 1)[-1]
        try:
            body = response.json()
        except ValueError:
            body = {}
        job_id = body.get("jobId") if isinstance(body, dict) else None
        if not job_id:
            raise MissingJobIdError(
                "Import trigger returned neither a Location header nor a jobId body field"
            )
        return str(job_id)

    def poll_job(self, *, job_id: str) -> DatabridgeJobStatus:
        response = self._request("GET", f"/databridge/v1/Jobs/{job_id}")
        try:
            body = response.json()
        except ValueError:
            body = response.text.strip()
        raw_status = str(body.get("status") if isinstance(body, dict) else body)
        status = raw_status.strip().lower()
        if status not in _KNOWN_STATUSES:
            raise UnknownJobStatusError(job_id, raw_status)

        return DatabridgeJobStatus(
            job_id=job_id,
            raw_status=raw_status,
            is_terminal=status in _SUCCESS_STATUSES or status in _FAILURE_STATUSES,
            is_success=status in _SUCCESS_STATUSES,
        )

    def list_instance_jobs(self, *, instance_name: str) -> list[DatabridgeJob]:
        response = self._request(
            "GET", f"/databridge/v1/sql-instances/{instance_name}/Jobs", store_body=False
        )
        body = response.json()
        if not isinstance(body, list):
            raise DatabridgeError(
                f"job list for {instance_name!r} is not a list: {type(body).__name__}"
            )
        return [_job_from_item(item) for item in body if isinstance(item, dict)]

    def database_exists(self, *, instance_name: str, database_name: str) -> bool:
        return database_name in self.list_databases(instance_name=instance_name)

    def list_databases(self, *, instance_name: str) -> set[str]:
        response = self._request("GET", f"/databridge/v1/sql-instances/{instance_name}/databases")
        return {str(item["name"]) for item in response.json() if item.get("name")}

    def archive_database(
        self,
        *,
        instance_name: str,
        database_name: str,
        resource_group_id: str | None = None,
        expiration_date: str | None = None,
    ) -> str:
        headers = {"content-type": "application/json"}
        if resource_group_id:
            headers["x-rms-resource-group-id"] = resource_group_id
        body: dict[str, str] = {}
        if expiration_date is not None:
            body["expirationDate"] = expiration_date

        response = self._request(
            "POST",
            f"/databridge/v1/sql-instances/{instance_name}/Databases/{database_name}/archive",
            headers=headers,
            json=body,
        )
        job_id = response.json().get("jobId")
        if not job_id:
            raise MissingJobIdError("Archive trigger returned no jobId body field")
        return str(job_id)

    def archive_exists(self, *, database_name: str) -> bool:
        response = self._request(
            "GET",
            "/platform/admindata/v1/archives",
            params={"filter": f'archiveName="{database_name}"', "limit": 100, "offset": 0},
        )
        rows = response.json()
        return isinstance(rows, list) and len(rows) > 0

    def probe_get(self, path: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        return self.probe_request("GET", path, params=params)

    def probe_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | dict[str, int] | None = None,
        json: object = None,
    ) -> httpx.Response:
        url = str(self._client.base_url.join(path))
        kwargs: dict[str, object] = {"params": params}
        if json is not None:
            kwargs["json"] = json
        if self._engine is None:
            return self._client.request(method, path, **kwargs)
        with audited_call(engine=self._engine, file_id=None, method=method, url=url) as call:
            call.request_body = json
            response = self._client.request(method, path, **kwargs)
            call.status_code = response.status_code
            call.response_body = _safe_json(response)
        return response

    def _send(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise RequestInterruptedError(
                f"{method} {path} got no response: {type(exc).__name__}"
            ) from exc

    def _request(
        self, method: str, path: str, *, store_body: bool = True, **kwargs: object
    ) -> httpx.Response:
        return retry_call(
            lambda: self._request_once(method, path, store_body=store_body, **kwargs),
            attempts=self._inline_retry_attempts,
            base_seconds=self._inline_retry_base_seconds,
            max_seconds=self._inline_retry_max_seconds,
            sleep=self._sleep,
        )

    def _request_once(
        self, method: str, path: str, *, store_body: bool = True, **kwargs: object
    ) -> httpx.Response:
        url = str(self._client.base_url.join(path))
        if self._engine is not None:
            with audited_call(
                engine=self._engine, file_id=self._current_file_id, method=method, url=url
            ) as call:
                call.request_body = kwargs.get("json")
                response = self._send(method, path, **kwargs)
                call.status_code = response.status_code
                if store_body or response.status_code >= 400:
                    call.response_body = _safe_json(response)
                else:
                    call.response_body = f"({len(response.content):,} bytes, body not stored)"
        else:
            response = self._send(method, path, **kwargs)

        _raise_for_status(response)
        return response

def _quietly[T](what: str, action: Callable[[], T]) -> T | None:
    try:
        return action()
    except Exception as exc:
        _log.warning("could not %s: %s", what, exc)
        return None

def _safe_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return response.text

def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

def _job_from_item(item: dict[str, object]) -> DatabridgeJob:
    def text(key: str) -> str:
        value = item.get(key)
        return "" if value is None else str(value)

    return DatabridgeJob(
        id=text("id"),
        type=text("type"),
        status=text("status"),
        resource_name=text("resourceName"),
        create_time=_parse_time(item.get("createTime")),
        end_time=_parse_time(item.get("endTime")),
        owner=text("owner"),
        resource_type=text("resourceType"),
    )

def _is_import_call(response: httpx.Response) -> bool:
    path = response.request.url.path.lower()
    return path.endswith("/import") or path.endswith("/init-upload")

def _vendor_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return str(body["message"])
    return response.text

def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise AuthenticationError(f"401 from {response.request.url}")
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise RateLimitedError(
            f"429 from {response.request.url}",
            retry_after=float(retry_after) if retry_after else None,
        )
    if response.status_code == 409 and _is_import_call(response) and (
        "already exists" in response.text.lower()
    ):
        raise DatabaseAlreadyExistsError(str(response.request.url), _vendor_message(response))
    if response.status_code >= 500:
        raise ServerError(f"{response.status_code} from {response.request.url}")
    if response.status_code >= 400:
        raise RequestRejectedError(
            f"{response.status_code} from {response.request.url}: {response.text}",
            status_code=response.status_code,
        )
