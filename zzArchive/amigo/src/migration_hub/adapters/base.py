from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

@dataclass(frozen=True, slots=True)
class PlatformSession:

    resource_group_id: str
    server_id: int

@dataclass(frozen=True, slots=True)
class UploadTarget:

    folder_id: str
    upload_url: str
    file_uri: str
    access_key_id: str
    secret_access_key: str
    session_token: str
    region: str

    def __repr__(self) -> str:
        return f"UploadTarget(folder_id={self.folder_id!r}, region=<redacted>, ...)"

@dataclass(frozen=True, slots=True)
class ExposureSet:

    exposure_set_id: str
    resource_uri: str

@dataclass(frozen=True, slots=True)
class JobStatus:

    job_id: str
    raw_status: str
    is_terminal: bool
    is_success: bool
    detail: str | None = None

class MigrationTargetAdapter(Protocol):

    def open_session(self) -> PlatformSession:
        ...

    def bound_to_file(self, file_id: int | None) -> AbstractContextManager[None]:
        ...

    def request_upload_target(self, *, file_extension: str) -> UploadTarget:
        ...

    def upload(self, *, source: Path, target: UploadTarget) -> None:
        ...

    def create_exposure_set(self, *, name: str) -> ExposureSet:
        ...

    def submit_import(
        self,
        *,
        session: PlatformSession,
        upload_target: UploadTarget,
        exposure_set: ExposureSet,
        exposure_name: str,
    ) -> str:
        ...

    def poll_import(self, *, session: PlatformSession, job_id: str) -> JobStatus:
        ...

    def exposure_exists(self, *, exposure_name: str) -> bool:
        ...
