from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from sqlalchemy.engine import Engine

from migration_hub.adapters.base import (
    ExposureSet,
    JobStatus,
    PlatformSession,
    TargetDatabase,
    UploadTarget,
)
from migration_hub.adapters.irp import session as irp_session
from migration_hub.adapters.irp.errors import (
    AuthenticationError,
    IrpError,
    MissingLocationHeaderError,
    RateLimitedError,
    UnknownJobStatusError,
)
from migration_hub.adapters.storage import s3_uploader
from migration_hub.observability.audit import audited_call

_TERMINAL_STATUSES = {"FINISHED", "FAILED", "CANCELLED"}
_KNOWN_STATUSES = {
    "PENDING",
    "QUEUED",
    "RUNNING",
    "CANCELLING",
    "CANCEL_REQUESTED",
    *_TERMINAL_STATUSES,
}

class IrpAdapter:

    def __init__(
        self, *, host: str, api_key: str, entitlement: str, engine: Engine | None = None
    ) -> None:
        self._host = host
        self._api_key = api_key
        self._entitlement = entitlement
        self._engine = engine
        self._session: PlatformSession | None = None
        self._current_file_id: int | None = None
        self._client = httpx.Client(base_url=host, headers={"Authorization": api_key}, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    @contextmanager
    def bound_to_file(self, file_id: int | None) -> Iterator[None]:
        previous = self._current_file_id
        self._current_file_id = file_id
        try:
            yield
        finally:
            self._current_file_id = previous

    def open_session(self) -> PlatformSession:
        self._session = irp_session.resolve(
            host=self._host, api_key=self._api_key, entitlement=self._entitlement
        )
        return self._session

    def request_upload_target(self, *, file_extension: str = "bak") -> UploadTarget:
        response = self._request(
            "POST",
            "/platform/import/v1/folders",
            json={"folderType": "EDM", "properties": {"fileExtension": file_extension}},
        )
        body = response.json()
        exposure_file = body["uploadDetails"]["exposureFile"]
        decoded = s3_uploader.decode_presign_params(exposure_file["presignParams"])
        return UploadTarget(
            folder_id=body["folderId"],
            upload_url=exposure_file["uploadUrl"],
            file_uri=exposure_file["fileUri"],
            access_key_id=decoded["accessKeyId"],
            secret_access_key=decoded["secretAccessKey"],
            session_token=decoded["sessionToken"],
            region=decoded["region"],
        )

    def upload(self, *, source: Path, target: UploadTarget) -> None:
        s3_uploader.upload_file(source=source, target=target)

    def create_exposure_set(self, *, name: str) -> ExposureSet:
        response = self._request(
            "POST",
            "/platform/riskdata/v1/exposuresets",
            json={"exposureSetName": name},
        )
        location = response.headers.get("Location")
        if not location:
            raise MissingLocationHeaderError(
                "POST /platform/riskdata/v1/exposuresets returned no Location header"
            )
        location = str(location)
        exposure_set_id = location.rstrip("/").rsplit("/", 1)[-1]
        return ExposureSet(exposure_set_id=exposure_set_id, resource_uri=location)

    def submit_import(
        self,
        *,
        session: PlatformSession,
        upload_target: UploadTarget,
        exposure_set: ExposureSet,
        exposure_name: str,
    ) -> str:
        payload = {
            "importType": "EDM",
            "resourceUri": exposure_set.resource_uri,
            "settings": {
                "folderId": upload_target.folder_id,
                "exposureName": exposure_name,
                "serverId": session.server_id,
            },
        }
        response = self._request(
            "POST",
            "/platform/import/v1/jobs",
            json=payload,
            headers={"x-rms-resource-group-id": session.resource_group_id},
        )
        location = response.headers.get("Location")
        if not location:
            raise MissingLocationHeaderError(
                "POST /platform/import/v1/jobs returned no Location header"
            )
        return str(location).rstrip("/").rsplit("/", 1)[-1]

    def poll_import(self, *, session: PlatformSession, job_id: str) -> JobStatus:
        response = self._request(
            "GET",
            f"/platform/import/v1/jobs/{job_id}",
            headers={"x-rms-resource-group-id": session.resource_group_id},
        )
        raw_status = str(response.json().get("status"))
        if raw_status not in _KNOWN_STATUSES:
            raise UnknownJobStatusError(job_id, raw_status)

        return JobStatus(
            job_id=job_id,
            raw_status=raw_status,
            is_terminal=raw_status in _TERMINAL_STATUSES,
            is_success=raw_status == "FINISHED",
        )

    def exposure_exists(self, *, exposure_name: str) -> bool:
        response = self._request(
            "GET",
            "/platform/riskdata/v1/exposures",
            params={"filter": f"exposureName = '{exposure_name}'"},
        )
        return len(response.json()) > 0

    def search_databases(
        self, *, page_size: int = 1000, max_pages: int = 100
    ) -> list[TargetDatabase]:
        databases: list[TargetDatabase] = []
        seen_ids: set[str] = set()
        offset = 0

        for _ in range(max_pages):
            response = self._request(
                "GET",
                "/platform/admindata/v1/databases",
                params={"limit": page_size, "offset": offset},
            )
            page = response.json()
            if isinstance(page, dict):
                for key in ("searchItems", "items", "data", "results"):
                    if isinstance(page.get(key), list):
                        page = page[key]
                        break
                else:
                    raise IrpError(
                        "GET /platform/admindata/v1/databases returned an object, "
                        f"not the documented array. Keys: {', '.join(sorted(page))}"
                    )

            for item in page:
                database_id = str(item.get("databaseId") or "").strip()
                if database_id and database_id in seen_ids:
                    continue
                if database_id:
                    seen_ids.add(database_id)
                size = item.get("sizeInMb")
                databases.append(
                    TargetDatabase(
                        database_id=database_id,
                        database_name=str(item.get("databaseName") or "").strip(),
                        database_type=str(item.get("databaseType") or "").strip(),
                        server_name=str(item.get("serverName") or "").strip(),
                        size_in_mb=float(size) if size is not None else None,
                    )
                )

            if len(page) < page_size:
                break
            offset += page_size
        else:
            raise IrpError(
                f"GET /platform/admindata/v1/databases returned more than "
                f"{max_pages * page_size} rows without ending. Stopped rather than "
                "looping."
            )

        return databases

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
        raise IrpError(f"{response.status_code} from {response.request.url}: {response.text}")
