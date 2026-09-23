from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TextIO

from migration_hub.observability.redaction import redact_text

ROOT_LOGGER = "migration_hub"

_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
_DATE_FORMAT = "%H:%M:%S"

_OURS = "_migration_hub_handler"

class _CurrentStderrHandler(logging.StreamHandler):

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, value: TextIO) -> None:
        pass

class RedactingFormatter(logging.Formatter):

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))

def configure(
    *, level: str = "INFO", log_dir: str | Path | None = None, stream: TextIO | None = None
) -> None:
    root = logging.getLogger(ROOT_LOGGER)
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        if getattr(handler, _OURS, False):
            root.removeHandler(handler)
            handler.close()

    formatter = RedactingFormatter(_FORMAT, datefmt=_DATE_FORMAT)
    handlers: list[logging.Handler] = [
        logging.StreamHandler(stream) if stream is not None else _CurrentStderrHandler()
    ]
    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        handlers.append(
            logging.FileHandler(directory / f"migration-hub-{stamp}.log", encoding="utf-8")
        )
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _OURS, True)
        root.addHandler(handler)

def get_logger(
    name: str, *, file_id: int | None = None
) -> logging.Logger | logging.LoggerAdapter[logging.Logger]:
    logger = logging.getLogger(name if name.startswith(ROOT_LOGGER) else f"{ROOT_LOGGER}.{name}")
    if file_id is None:
        return logger
    return _FileAdapter(logger, {"file_id": file_id})

class _FileAdapter(logging.LoggerAdapter[logging.Logger]):
    def process(self, msg: object, kwargs: object) -> tuple[object, object]:
        assert self.extra is not None
        return f"file {self.extra['file_id']}  {msg}", kwargs

class Heartbeat:

    def __init__(
        self,
        logger: logging.Logger | logging.LoggerAdapter[logging.Logger],
        *,
        every_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._every = every_seconds
        self._clock = clock
        self._started: float | None = None
        self._last = 0.0

    def beat(self, describe: Callable[[float], str]) -> None:
        now = self._clock()
        if self._started is None:
            self._started = self._last = now
            return
        if now - self._last >= self._every:
            self._last = now
            self._logger.info(describe((now - self._started) / 60))
