from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from sqlalchemy import Engine, create_engine, event, text

_LOG = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 5_000

_UNC_PREFIX = ("\\\\", "//")

class RegistryPathError(RuntimeError):
    pass

def _sqlite_file(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite"):
        return None
    raw = unquote(urlparse(database_url).path or "")
    if not raw or raw.strip("/") in ("", ":memory:"):
        return None

    body = raw[1:] if raw.startswith("/") else raw
    if body.startswith(("\\\\", "//")):
        return Path("\\\\" + body.lstrip("/\\"))

    return Path(body)

def is_network_url(database_url: str) -> bool:
    if not database_url.startswith("sqlite"):
        return False
    raw = unquote(urlparse(database_url).path or "")
    body = (raw[1:] if raw.startswith("/") else raw).replace("\\", "/")
    if not body.startswith("/"):
        return False
    host = body.lstrip("/").split("/", 1)[0]
    return bool(host) and not host.endswith(":")

_SYNCED_FOLDER_MARKERS = ("onedrive", "dropbox", "google drive", "sharepoint", "box sync")

def is_synced_path(path: Path) -> bool:
    lowered = [part.lower() for part in path.parts]
    return any(
        marker in part for part in lowered for marker in _SYNCED_FOLDER_MARKERS
    )

def _reject_synced_path(path: Path, *, allow_network: bool) -> None:
    if allow_network or not is_synced_path(path):
        return
    raise RegistryPathError(
        f"The registry path {str(path)!r} is inside a file-sync folder. A sync "
        "client rewrites the file from another process, which corrupts SQLite "
        "the same way a network share does -- and it looks like local disk "
        "while doing it. Use a genuinely local directory such as "
        "D:/MigrationHub/, and let the sync folder hold a backup copy rather "
        "than the live registry. Set MIGRATION_HUB_ALLOW_NETWORK_REGISTRY=yes "
        "to override knowingly."
    )

def _reject_network_path(database_url: str, *, allow_network: bool) -> None:
    if allow_network or not is_network_url(database_url):
        return
    raise RegistryPathError(
            f"The registry path in {database_url!r} is a network share. SQLite "
            "corrupts over SMB/CIFS: file locking is unreliable and WAL is "
            "unavailable, so the concurrency guarantee disappears silently. "
            "Put the file on local disk and back it up to the share instead. "
            "Set MIGRATION_HUB_ALLOW_NETWORK_REGISTRY=yes to override "
            "knowingly."
    )

def _prepare_sqlite_file(path: Path, database_url: str, *, allow_network: bool) -> None:
    _reject_network_path(database_url, allow_network=allow_network)
    _reject_synced_path(path, allow_network=allow_network)
    parent = path.parent
    if parent and not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RegistryPathError(
                f"Cannot create the registry directory {parent!r}: {exc}"
            ) from exc

ALLOW_NETWORK_ENV = "MIGRATION_HUB_ALLOW_NETWORK_REGISTRY"

def _network_allowed(explicit: bool) -> bool:
    if explicit:
        return True
    return os.environ.get(ALLOW_NETWORK_ENV, "").strip().lower() in {"yes", "true", "1"}

def create_registry_engine(
    database_url: str,
    *,
    allow_network: bool = False,
    **kwargs: object,
) -> Engine:
    allowed = _network_allowed(allow_network)
    sqlite_path = _sqlite_file(database_url)
    if sqlite_path is not None:
        _prepare_sqlite_file(sqlite_path, database_url, allow_network=allowed)

    engine = create_engine(database_url, **kwargs)
    if engine.dialect.name != "sqlite":
        return engine

    @event.listens_for(engine, "connect")
    def _configure(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    if not allowed:
        _assert_wal(engine, sqlite_path)
    elif sqlite_path is not None:
        _warn_degraded(engine, sqlite_path)
    return engine

def _warn_degraded(engine: Engine, path: Path) -> None:
    with engine.connect() as conn:
        mode = str(conn.execute(text("PRAGMA journal_mode")).scalar()).lower()

    if mode == "wal":
        _LOG.warning(
            "Registry at %s is on a synced or network path, allowed by %s. "
            "WAL was granted, so concurrency is intact -- but a sync client "
            "rewriting the file from another process can still corrupt it. "
            "Back it up regularly and do not run a second worker against it.",
            path,
            ALLOW_NETWORK_ENV,
        )
        return

    _LOG.warning(
        "Registry at %s fell back to %r journalling instead of WAL, allowed "
        "by %s. Readers now block the writer, and the concurrency the claim "
        "mechanism assumes is weaker than it was tested under. Single worker "
        "only, and treat the file as at risk.",
        path,
        mode,
        ALLOW_NETWORK_ENV,
    )

def _assert_wal(engine: Engine, path: Path | None) -> None:
    if path is None:
        return
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    if str(mode).lower() != "wal":
        raise RegistryPathError(
            f"SQLite refused WAL mode at {path} and fell back to {mode!r}. "
            "That usually means the file is on a filesystem without shared "
            "memory -- a network share. Rollback journalling does not give the "
            "concurrency the claim mechanism assumes, so this is refused "
            "rather than run."
        )
