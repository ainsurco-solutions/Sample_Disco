from __future__ import annotations

import logging

def configure(*, level: str = "INFO", log_dir: str | None = None) -> None:
    raise NotImplementedError

def get_logger(name: str, *, file_id: int | None = None) -> logging.Logger:
    raise NotImplementedError
