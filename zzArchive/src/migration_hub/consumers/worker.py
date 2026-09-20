from __future__ import annotations

import time
from pathlib import Path

from migration_hub.adapters.base import MigrationTargetAdapter, PlatformSession, UploadTarget
from migration_hub.adapters.irp.errors import ImportJobFailedError
from migration_hub.core.registry import Registry
from migration_hub.core.retry import FailureAction, classify
from migration_hub.core.states import FileState

class MigrationWorker:

    def __init__(
        self,
        *,
        registry: Registry,
        adapter: MigrationTargetAdapter,
        worker_id: str,
        dry_run: bool = False,
        max_attempts: int = 3,
        poll_interval_seconds: float = 5.0,
        max_poll_minutes: float = 30.0,
    ) -> None:
        self._registry = registry
        self._adapter = adapter
        self._worker_id = worker_id
        self._dry_run = dry_run
        self._max_attempts = max_attempts
        self._poll_interval_seconds = poll_interval_seconds
        self._max_poll_minutes = max_poll_minutes
        self._session: PlatformSession | None = None
        self._dry_run_seen: set[int] = set()

    def run_once(self, *, batch_id: str) -> FileState | None:
        file = self._registry.claim_next(
            batch_id=batch_id,
            from_state=FileState.STAGED,
            to_state=FileState.UPLOADING,
            worker_id=self._worker_id,
            exclude_file_ids=self._dry_run_seen,
        )
        if file is None:
            return None

        if self._dry_run:
            self._dry_run_seen.add(file.file_id)
            self._registry.transition(
                file_id=file.file_id,
                to_state=FileState.STAGED,
                actor=self._worker_id,
                detail="dry run: claimed and released, nothing sent to the vendor",
            )
            return FileState.STAGED

        try:
            if self._session is None:
                self._session = self._adapter.open_session()

            target = self._upload(file.file_id, staged_path=file.staged_path)
            job_id = self._import(
                file.file_id,
                target=target,
                exposure_name=file.target_exposure_name,
            )
            self._poll(file.file_id, job_id=job_id)
            self._verify(file.file_id, exposure_name=file.target_exposure_name)
        except BaseException as exc:
            return self._handle_failure(file.file_id, exc)
        return FileState.COMPLETED

    def _upload(self, file_id: int, *, staged_path: str | None) -> UploadTarget:
        if staged_path is None:
            raise ValueError(f"file_id={file_id} has no staged_path -- was it staged?")

        with self._adapter.bound_to_file(file_id):
            target = self._adapter.request_upload_target(file_extension="bak")

        self._registry.record_identifiers(
            file_id=file_id, actor=self._worker_id, folder_id=target.folder_id
        )

        with self._adapter.bound_to_file(file_id):
            self._adapter.upload(source=Path(staged_path), target=target)

        self._registry.transition(
            file_id=file_id, to_state=FileState.UPLOADED, actor=self._worker_id
        )
        return target

    def _import(self, file_id: int, *, target: UploadTarget, exposure_name: str) -> str:
        assert self._session is not None

        with self._adapter.bound_to_file(file_id):
            exposure_set = self._adapter.create_exposure_set(name="Automated")

        with self._adapter.bound_to_file(file_id):
            job_id = self._adapter.submit_import(
                session=self._session,
                upload_target=target,
                exposure_set=exposure_set,
                exposure_name=exposure_name,
            )

        self._registry.record_identifiers(
            file_id=file_id,
            actor=self._worker_id,
            job_id=job_id,
            exposure_set_id=exposure_set.exposure_set_id,
            exposure_set_uri=exposure_set.resource_uri,
            server_id=self._session.server_id,
        )
        self._registry.transition(
            file_id=file_id, to_state=FileState.IMPORTING, actor=self._worker_id
        )
        return job_id

    def _poll(self, file_id: int, *, job_id: str) -> None:
        assert self._session is not None
        deadline = time.monotonic() + self._max_poll_minutes * 60

        while True:
            with self._adapter.bound_to_file(file_id):
                status = self._adapter.poll_import(session=self._session, job_id=job_id)

            if status.is_terminal:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"job {job_id} for file_id={file_id} did not reach a terminal "
                    f"state within {self._max_poll_minutes} minutes"
                )
            time.sleep(self._poll_interval_seconds)

        if not status.is_success:
            raise ImportJobFailedError(job_id, status.detail or status.raw_status)

        self._registry.transition(
            file_id=file_id, to_state=FileState.VERIFYING, actor=self._worker_id
        )

    def _verify(self, file_id: int, *, exposure_name: str) -> None:
        with self._adapter.bound_to_file(file_id):
            exists = self._adapter.exposure_exists(exposure_name=exposure_name)

        if not exists:
            raise LookupError(
                f"job for file_id={file_id} reported SUCCESS but {exposure_name!r} "
                "is not visible on the platform yet"
            )

        self._registry.transition(
            file_id=file_id, to_state=FileState.COMPLETED, actor=self._worker_id
        )

    def _handle_failure(self, file_id: int, exc: BaseException) -> FileState:
        classification = classify(exc)

        if classification.action is FailureAction.RESTAGE:
            self._registry.transition(
                file_id=file_id,
                to_state=FileState.STAGED,
                actor=self._worker_id,
                detail=classification.reason,
            )
            return FileState.STAGED

        retryable = classification.action is FailureAction.RETRY
        resulting_state = self._registry.record_failure(
            file_id=file_id,
            error=classification.reason,
            retryable=retryable,
            max_attempts=self._max_attempts,
        )

        if classification.action is FailureAction.HALT:
            raise exc

        return resulting_state
