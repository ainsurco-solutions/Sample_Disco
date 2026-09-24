from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum

class FailureAction(StrEnum):

    RETRY = "RETRY"
    """Transient. Back off and try the same step again."""

    RESTAGE = "RESTAGE"
    """Credentials or upload target are dead. Return to STAGED and start over."""

    ABANDON = "ABANDON"
    """Not recoverable by retry. Needs a human."""

    HALT = "HALT"
    """Systemic. Stop the batch -- retrying would multiply the problem."""

@dataclass(frozen=True, slots=True)
class Classification:
    action: FailureAction
    reason: str
    retry_after_seconds: float | None = None

class RetryableError(Exception):

    retry_after: float | None = None

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after

class RestageError(Exception):
    pass

class AbandonError(Exception):
    pass

ALREADY_ON_TARGET = "Already on Data Bridge"

class AlreadyOnTargetError(AbandonError):
    pass

class HaltError(Exception):
    pass

_DEFAULT_ACTION = FailureAction.ABANDON

def classify(exc: BaseException) -> Classification:
    if isinstance(exc, HaltError):
        return Classification(FailureAction.HALT, reason=str(exc) or type(exc).__name__)
    if isinstance(exc, RestageError):
        return Classification(FailureAction.RESTAGE, reason=str(exc) or type(exc).__name__)
    if isinstance(exc, RetryableError):
        return Classification(
            FailureAction.RETRY,
            reason=str(exc) or type(exc).__name__,
            retry_after_seconds=exc.retry_after,
        )
    if isinstance(exc, AbandonError):
        return Classification(FailureAction.ABANDON, reason=str(exc) or type(exc).__name__)
    return Classification(
        _DEFAULT_ACTION, reason=f"unrecognised error: {type(exc).__name__}: {exc}"
    )

def backoff_delay(
    attempt: int, *, base_seconds: float, max_seconds: float, jitter: bool = True
) -> float:
    delay = min(base_seconds * (2.0 ** max(attempt - 1, 0)), max_seconds)
    if jitter:
        delay = float(random.uniform(0, delay))
    return delay
