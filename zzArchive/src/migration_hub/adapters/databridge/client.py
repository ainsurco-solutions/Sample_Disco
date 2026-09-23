from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy.engine import Engine

from migration_hub.adapters.base import PlatformSession
from migration_hub.adapters.databridge import session as session_module
from migration_hub.adapters.databridge.errors import (
    AuthenticationError,
    DatabridgeError,
    MissingJobIdError,
    MissingUploadUriError,
    RateLimitedError,
    UnknownJobStatusError,
)
from migration_hub.adapters.storage import databridge_uploader
from migration_hub.observability.audit import audited_call

LARGE_FILE_THRESHOLD_BYTES = 5 * 1024**3

_FORMAT_CODES = {"mdf": 0, "bak": 1, "dacpac": 2}

_SUCCESS_STATUSES = {"SUCCESS", "Succeeded"}
_FAILURE_STATUSES = {"FAILED", "Failed", "CANCELLED", "Cancelled"}
_IN_PROGRESS_STATUSES = {"InProgress", "PENDING", "QUEUED", "RUNNING"}
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

class DatabridgeAdapter:

    def __init__(self, *, host: str, api_key: str, engine: Engine | None = None) -> None:
        self._host = host
        self._api_key = api_key
        self._engine = engine
        self._current_file_id: int | None = None
        self._client = httpx.Client(base_url=host, headers={"Authorization": api_key}, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def open_session(self) -> PlatformSession:
        return session_module.resolve(host=self._host, api_key=self._api_key)

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
    ) -> str:
        if file_extension not in _FORMAT_CODES:
            raise ValueError(f"unsupported file_extension: {file_extension!r}")
        format_code = _FORMAT_CODES[file_extension]
        file_size = source.stat().st_size

        if file_size < LARGE_FILE_THRESHOLD_BYTES:
            self._upload_small(
                instance_name=instance_name,
                database_name=database_name,
                format_code=format_code,
                source=source,
            )
        else:
            self._upload_large(
                instance_name=instance_name,
                database_name=database_name,
                file_extension=file_extension,
                source=source,
            )

        return self._trigger_import(
            instance_name=instance_name,
            database_name=database_name,
            format_code=format_code,
            group_ids=group_ids if group_ids is not None else [],
        )

    def _upload_small(
        self, *, instance_name: str, database_name: str, format_code: int, source: Path
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
        databridge_uploader.upload_via_presigned_url(source=source, url=upload_url)

    def _upload_large(
        self, *, instance_name: str, database_name: str, file_extension: str, source: Path
    ) -> None:
        base = (
            f"/databridge/v1/sql-instances/{instance_name}/databases/"
            f"{database_name}/{file_extension}"
        )

        init_response = self._request("POST", f"{base}/init-upload")
        upload_id = init_response.json()["uploadId"]

        def get_part_url(part_number: int) -> str:
            part_response = self._request("GET", f"{base}/upload-part/{upload_id}/{part_number}")
            return str(part_response.json())

        etags = databridge_uploader.upload_multipart(source=source, get_part_url=get_part_url)

        self._request(
            "POST",
            f"{base}/complete-upload",
            json={"uploadId": upload_id, "etags": {str(k): v for k, v in etags.items()}},
        )

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
        if raw_status not in _KNOWN_STATUSES:
            raise UnknownJobStatusError(job_id, raw_status)

        return DatabridgeJobStatus(
            job_id=job_id,
            raw_status=raw_status,
            is_terminal=raw_status in _SUCCESS_STATUSES or raw_status in _FAILURE_STATUSES,
            is_success=raw_status in _SUCCESS_STATUSES,
        )

    def database_exists(self, *, instance_name: str, database_name: str) -> bool:
        response = self._request("GET", f"/databridge/v1/sql-instances/{instance_name}/databases")
        return any(item.get("name") == database_name for item in response.json())

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

    def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        url = str(self._client.base_url.join(path))
        if self._engine is not None:
            with audited_call(
                engine=self._engine, file_id=self._current_file_id, method=method, url=url
            ) as call:
                call.request_body = kwargs.get("json")
                response = self._client.request(method, path, **kwargs)
                call.status_code = response.status_code
                call.response_body = _safe_json(response)
        else:
            response = self._client.request(method, path, **kwargs)

        _raise_for_status(response)
        return response

def _safe_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
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
    if response.status_code >= 400:
        raise DatabridgeError(
            f"{response.status_code} from {response.request.url}: {response.text}"
        )
