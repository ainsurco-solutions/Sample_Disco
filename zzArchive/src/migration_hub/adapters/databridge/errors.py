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

class UploadCredentialsExpiredError(DatabridgeError, RestageError):
    pass

class MissingPartETagError(DatabridgeError, AbandonError):

    def __init__(self, part_number: int) -> None:
        super().__init__(f"Chunk {part_number} PUT returned no ETag header")
        self.part_number = part_number

class MissingJobIdError(DatabridgeError, AbandonError):
    pass

class UnknownJobStatusError(DatabridgeError, AbandonError):

    def __init__(self, job_id: str, raw_status: str) -> None:
        super().__init__(f"Unrecognised status {raw_status!r} for job {job_id}")
        self.job_id = job_id
        self.raw_status = raw_status
