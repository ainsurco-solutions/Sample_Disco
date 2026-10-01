from __future__ import annotations

import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

LOCK_DIR = Path("var/locks")

_LOCK_OFFSET = 4096

_ATTEMPTS = 5
_RETRY_SECONDS = 0.2

class BatchBusyError(RuntimeError):

    def __init__(self, batch_id: str, holder: str | None) -> None:
        self.batch_id = batch_id
        self.holder = holder
        who = f" (pid {holder})" if holder else ""
        super().__init__(f"another pipeline{who} is already working on batch {batch_id!r}")

@contextmanager
def hold(
    batch_id: str,
    *,
    directory: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    with _open(batch_id, directory) as handle:
        for attempt in range(_ATTEMPTS):
            if _try_lock(handle):
                break
            if attempt + 1 < _ATTEMPTS:
                sleep(_RETRY_SECONDS)
        else:
            raise BatchBusyError(batch_id, _holder(handle))
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode())
            handle.flush()
            yield
        finally:
            _unlock(handle)

def holder(batch_id: str, *, directory: Path | None = None) -> str | None:
    if not _path(batch_id, directory).exists():
        return None
    with _open(batch_id, directory) as handle:
        if _try_lock(handle):
            _unlock(handle)
            return None
        return _holder(handle) or "?"

def _path(batch_id: str, directory: Path | None) -> Path:
    return (directory or LOCK_DIR) / f"batch-{re.sub(r'[^A-Za-z0-9._-]', '_', batch_id)}.lock"

@contextmanager
def _open(batch_id: str, directory: Path | None) -> Iterator[IO[bytes]]:
    path = _path(batch_id, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open("r+b") as handle:
        yield handle

def _holder(handle: IO[bytes]) -> str | None:
    os.lseek(handle.fileno(), 0, os.SEEK_SET)
    text = os.read(handle.fileno(), 32).decode(errors="replace").strip()
    return text or None

def _try_lock(handle: IO[bytes]) -> bool:
    try:
        if sys.platform == "win32":
            handle.seek(_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True

def _unlock(handle: IO[bytes]) -> None:
    if sys.platform == "win32":
        handle.seek(_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
