from __future__ import annotations

from enum import StrEnum

class FileState(StrEnum):

    DISCOVERED = "DISCOVERED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    IMPORTING = "IMPORTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"

TERMINAL_STATES: frozenset[FileState] = frozenset(
    {FileState.COMPLETED, FileState.REJECTED, FileState.ABANDONED}
)

CLAIMED_STATES: frozenset[FileState] = frozenset({FileState.UPLOADING, FileState.IMPORTING})

LEGAL_TRANSITIONS: dict[FileState, frozenset[FileState]] = {
    FileState.DISCOVERED: frozenset({FileState.VALIDATED, FileState.REJECTED}),
    FileState.VALIDATED: frozenset({FileState.UPLOADING, FileState.FAILED}),
    FileState.REJECTED: frozenset(),
    FileState.UPLOADING: frozenset({FileState.UPLOADED, FileState.VALIDATED, FileState.FAILED}),
    FileState.UPLOADED: frozenset({FileState.IMPORTING, FileState.FAILED}),
    FileState.IMPORTING: frozenset({FileState.VERIFYING, FileState.FAILED}),
    FileState.VERIFYING: frozenset({FileState.COMPLETED, FileState.FAILED}),
    FileState.COMPLETED: frozenset(),
    FileState.FAILED: frozenset(
        {
            FileState.VALIDATED,
            FileState.UPLOADING,
            FileState.IMPORTING,
            FileState.ABANDONED,
        }
    ),
    FileState.ABANDONED: frozenset({FileState.VALIDATED}),
}

class IllegalTransitionError(RuntimeError):

    def __init__(self, current: FileState, target: FileState) -> None:
        super().__init__(f"Illegal state transition: {current} -> {target}")
        self.current = current
        self.target = target

def assert_transition(current: FileState, target: FileState) -> None:
    if target not in LEGAL_TRANSITIONS.get(current, frozenset()):
        raise IllegalTransitionError(current, target)

class BatchState(StrEnum):

    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    DONE = "DONE"
