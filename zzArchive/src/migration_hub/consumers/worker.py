from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from migration_hub.adapters.databridge.client import DatabridgeAdapter
from migration_hub.adapters.databridge.errors import ImportJobFailedError
from migration_hub.core.registry import Registry
from migration_hub.core.retry import FailureAction, classify
from migration_hub.core.routing import resolve_instance
from migration_hub.core.states import FileState

class SourceChangedSinceDiscoveryError(RuntimeError):

    def __init__(self, *, file_id: int, expected: int, actual: int) -> None:
        super().__init__(
            f"file_id={file_id}: source size changed since discovery "
            f"(expected {expected}, now {actual}) -- it may still be being written"
        )

class MigrationWorker:

    def __init__(
        self,
        *,
        registry: Registry,
        adapter: DatabridgeAdapter,
        worker_id: str,
        dry_run: bool = False,
        max_attempts: int = 3,
        poll_interval_seconds: float = 5.0,
        max_poll_minutes: float = 30.0,
        instance_name: str | None = None,
        instances: dict[str, str] | None = None,
        archive: Callable[[int, str | None], FileState] | None = None,
    ) -> None:
        self._registry = registry
        self._archive = archive
        self._adapter = adapter
        self._worker_id = worker_id
        self._dry_run = dry_run
        self._max_attempts = max_attempts
        self._poll_interval_seconds = poll_interval_seconds
        self._max_poll_minutes = max_poll_minutes
        self._instance_name = instance_name
        self._instances = dict(instances or {})
        self._dry_run_seen: set[int] = set()

    def run_once(self, *, batch_id: str) -> FileState | None:
        file = self._registry.claim_next(
            batch_id=batch_id,
            from_state=FileState.VALIDATED,
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
                to_state=FileState.VALIDATED,
                actor=self._worker_id,
                detail="dry run: claimed and released, nothing sent to the vendor",
            )
            return FileState.VALIDATED

        try:
            session = self._adapter.open_session()

            job_id = self._upload_and_import(
                file.file_id,
                source_path=file.source_path,
                database_name=file.source_database,
                expected_size_bytes=file.size_bytes,
            )
            self._poll(file.file_id, job_id=job_id)
            self._verify(
                file.file_id,
                database_name=file.source_database,
                source_path=file.source_path,
            )
        except BaseException as exc:
            return self._handle_failure(file.file_id, exc)

        if self._archive is None:
            return FileState.BRIDGED
        with self._adapter.bound_to_file(file.file_id):
            return self._archive(file.file_id, session.resource_group_id)

    def _resolve_instance(self, source_path: str) -> str:
        if self._instances:
            return resolve_instance(source_path=source_path, mapping=self._instances)
        if not self._instance_name:
            raise ValueError(
                "no Data Bridge configured: set databridge_instances (server -> "
                "instance) or databridge_instance_name"
            )
        return self._instance_name

    def _upload_and_import(
        self, file_id: int, *, source_path: str, database_name: str, expected_size_bytes: int
    ) -> str:
        instance_name = self._resolve_instance(source_path)

        current_size = Path(source_path).stat().st_size
        if current_size != expected_size_bytes:
            raise SourceChangedSinceDiscoveryError(
                file_id=file_id, expected=expected_size_bytes, actual=current_size
            )

        self._registry.record_identifiers(
            file_id=file_id,
            actor=self._worker_id,
            instance_name=instance_name,
            database_name=database_name,
        )

        with self._adapter.bound_to_file(file_id):
            job_id = self._adapter.upload_and_import(
                instance_name=instance_name,
                database_name=database_name,
                file_extension="bak",
                source=Path(source_path),
            )

        self._registry.transition(
            file_id=file_id,
            to_state=FileState.UPLOADED,
            actor=self._worker_id,
            uploaded_at=datetime.now(UTC).replace(tzinfo=None),
        )
        self._registry.transition(
            file_id=file_id, to_state=FileState.IMPORTING, actor=self._worker_id, job_id=job_id
        )
        return job_id

    def _poll(self, file_id: int, *, job_id: str) -> None:
        deadline = time.monotonic() + self._max_poll_minutes * 60

        while True:
            with self._adapter.bound_to_file(file_id):
                status = self._adapter.poll_job(job_id=job_id)

            if status.is_terminal:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"job {job_id} for file_id={file_id} did not reach a terminal "
                    f"state within {self._max_poll_minutes} minutes"
                )
            time.sleep(self._poll_interval_seconds)

        if not status.is_success:
            raise ImportJobFailedError(job_id, status.raw_status)

        self._registry.transition(
            file_id=file_id, to_state=FileState.VERIFYING, actor=self._worker_id
        )

    def _verify(self, file_id: int, *, database_name: str, source_path: str) -> None:
        instance_name = self._resolve_instance(source_path)

        with self._adapter.bound_to_file(file_id):
            exists = self._adapter.database_exists(
                instance_name=instance_name, database_name=database_name
            )

        if not exists:
            raise LookupError(
                f"job for file_id={file_id} reported success but {database_name!r} "
                "is not visible on the platform yet"
            )

        self._registry.transition(
            file_id=file_id, to_state=FileState.BRIDGED, actor=self._worker_id
        )

    def _handle_failure(self, file_id: int, exc: BaseException) -> FileState:
        classification = classify(exc)

        if classification.action is FailureAction.RESTAGE:
            file = self._registry.get(file_id)
            if file is not None and file.attempts < self._max_attempts:
                self._registry.transition(
                    file_id=file_id,
                    to_state=FileState.VALIDATED,
                    actor=self._worker_id,
                    detail=classification.reason,
                )
                return FileState.VALIDATED

        retryable = classification.action in (FailureAction.RETRY, FailureAction.RESTAGE)
        resulting_state = self._registry.record_failure(
            file_id=file_id,
            error=classification.reason,
            retryable=retryable,
            max_attempts=self._max_attempts,
        )

        if classification.action is FailureAction.HALT:
            raise exc

        return resulting_state
