from __future__ import annotations

from migration_hub.core.retry import AbandonError, HaltError, RestageError, RetryableError

class DatabridgeError(RuntimeError):
    pass

class AuthenticationError(DatabridgeError, HaltError):
    pass

class RateLimitedError(DatabridgeError, RetryableError):

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        RuntimeError.__init__(self, message)
        self.retry_after = retry_after

class RequestInterruptedError(DatabridgeError, RetryableError):
    pass

class UploadCredentialsExpiredError(DatabridgeError, RestageError):
    pass

class UploadServerError(DatabridgeError, RetryableError):
    pass

class UploadInterruptedError(DatabridgeError, RetryableError):
    pass

class UploadRejectedError(DatabridgeError, AbandonError):
    pass

class MissingPartETagError(DatabridgeError, AbandonError):

    def __init__(self, part_number: int) -> None:
        super().__init__(f"Chunk {part_number} PUT returned no ETag header")
        self.part_number = part_number

class MissingUploadUriError(DatabridgeError, AbandonError):

    def __init__(self, keys: object) -> None:
        super().__init__(f"import response had neither backupUri nor mdfUri (keys: {keys})")

class MissingJobIdError(DatabridgeError, AbandonError):
    pass

class ImportJobFailedError(DatabridgeError, AbandonError):

    def __init__(self, job_id: str, detail: str | None) -> None:
        super().__init__(f"Import job {job_id} failed: {detail or '(no detail)'}")
        self.job_id = job_id

class UnknownJobStatusError(DatabridgeError, AbandonError):

    def __init__(self, job_id: str, raw_status: str) -> None:
        super().__init__(f"Unrecognised status {raw_status!r} for job {job_id}")
        self.job_id = job_id
        self.raw_status = raw_status
