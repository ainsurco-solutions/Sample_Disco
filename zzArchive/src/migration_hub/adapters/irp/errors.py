from __future__ import annotations

from migration_hub.core.retry import AbandonError, HaltError, RestageError, RetryableError

class IrpError(RuntimeError):
    pass

class AuthenticationError(IrpError, HaltError):
    pass

class RateLimitedError(IrpError, RetryableError):

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        RuntimeError.__init__(self, message)
        self.retry_after = retry_after

class UploadTransientError(IrpError, RetryableError):
    pass

class UploadCredentialsExpiredError(IrpError, RestageError):
    pass

class UnknownJobStatusError(IrpError, AbandonError):

    def __init__(self, job_id: str, raw_status: str) -> None:
        super().__init__(f"Unrecognised status {raw_status!r} for job {job_id}")
        self.job_id = job_id
        self.raw_status = raw_status

class MissingLocationHeaderError(IrpError, AbandonError):
    pass

class ImportJobFailedError(IrpError, AbandonError):

    def __init__(self, job_id: str, detail: str | None) -> None:
        super().__init__(f"Import job {job_id} failed: {detail or '(no detail)'}")
        self.job_id = job_id
