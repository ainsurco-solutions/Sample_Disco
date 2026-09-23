from __future__ import annotations

import datetime as _dt
import sqlite3
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import functions

UTC_TIMESTAMP = DateTime().with_variant(DATETIME2, "mssql")

UTC_NOW = func.now()

@compiles(functions.now, "mssql")
def _mssql_utc_now(element: object, compiler: object, **kw: object) -> str:
    return "sysutcdatetime()"

AUTO_PK = BigInteger().with_variant(Integer, "sqlite")

def _register_sqlite_datetime_handlers() -> None:
    sqlite3.register_adapter(_dt.date, lambda value: value.isoformat())
    sqlite3.register_adapter(_dt.datetime, lambda value: value.isoformat(sep=" "))

    def _convert_date(raw: bytes) -> _dt.date:
        return _dt.date.fromisoformat(raw.decode())

    def _convert_timestamp(raw: bytes) -> _dt.datetime:
        return _dt.datetime.fromisoformat(raw.decode())

    sqlite3.register_converter("date", _convert_date)
    sqlite3.register_converter("timestamp", _convert_timestamp)

_register_sqlite_datetime_handlers()

class Base(DeclarativeBase):
    pass

class MigrationFile(Base):

    __tablename__ = "migration_file"
    __table_args__ = (
        Index("ix_migration_file_state_batch_id", "state", "batch_id"),
    )

    file_id: Mapped[int] = mapped_column(AUTO_PK, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(64), ForeignKey("batch.batch_id"), nullable=False)

    source_database: Mapped[str] = mapped_column(String(256), nullable=False)
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    target_exposure_name: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)

    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64), default=None)
    source_last_write: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, default=None)

    state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    claimed_by: Mapped[str | None] = mapped_column(String(128), default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, default=None)

    job_id: Mapped[str | None] = mapped_column(String(128), default=None)
    instance_name: Mapped[str | None] = mapped_column(String(256), default=None)
    database_name: Mapped[str | None] = mapped_column(String(256), default=None)

    uploaded_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, default=None)
    archive_job_id: Mapped[str | None] = mapped_column(String(128), default=None)
    archived_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, default=None)
    archive_expiration_date: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, default=None)
    archive_attempts: Mapped[int] = mapped_column(default=0, server_default="0")

    last_error: Mapped[str | None] = mapped_column(String(None), default=None)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=UTC_NOW)
    updated_at: Mapped[datetime] = mapped_column(
        UTC_TIMESTAMP, server_default=UTC_NOW, onupdate=UTC_NOW
    )

class MigrationEvent(Base):

    __tablename__ = "migration_event"

    event_id: Mapped[int] = mapped_column(AUTO_PK, primary_key=True, autoincrement=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("migration_file.file_id"), index=True, nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=UTC_NOW)
    from_state: Mapped[str | None] = mapped_column(String(32), default=None)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(None), default=None)

class ApiTransaction(Base):

    __tablename__ = "api_transaction"

    transaction_id: Mapped[int] = mapped_column(AUTO_PK, primary_key=True, autoincrement=True)
    file_id: Mapped[int | None] = mapped_column(
        ForeignKey("migration_file.file_id"), index=True, default=None
    )
    occurred_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=UTC_NOW)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    status_code: Mapped[int | None] = mapped_column(default=None)
    duration_ms: Mapped[int | None] = mapped_column(default=None)
    request_body: Mapped[str | None] = mapped_column(String(None), default=None)
    response_body: Mapped[str | None] = mapped_column(String(None), default=None)
    correlation_id: Mapped[str | None] = mapped_column(String(128), index=True, default=None)

class Batch(Base):

    __tablename__ = "batch"

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str | None] = mapped_column(String(512), default=None)
    wave: Mapped[int | None] = mapped_column(default=None)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    max_concurrency: Mapped[int] = mapped_column(default=1, server_default="1")
    destination: Mapped[str] = mapped_column(
        String(16), nullable=False, default="VAULT", server_default="VAULT"
    )
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=UTC_NOW)
    started_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, default=None)

class BatchRun(Base):

    __tablename__ = "batch_run"
    __table_args__ = (UniqueConstraint("batch_id", "run_seq"),)

    run_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), ForeignKey("batch.batch_id"), nullable=False)
    run_seq: Mapped[int] = mapped_column(nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    initiated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP, server_default=UTC_NOW)
    finished_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, default=None)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    source_count: Mapped[int] = mapped_column(nullable=False)
    source_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    target_count: Mapped[int | None] = mapped_column(default=None)
    target_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)

    completed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    abandoned_count: Mapped[int] = mapped_column(default=0, server_default="0")
    rejected_count: Mapped[int] = mapped_column(default=0, server_default="0")
    skipped_count: Mapped[int] = mapped_column(default=0, server_default="0")
    outstanding_count: Mapped[int] = mapped_column(default=0, server_default="0")
    archive_failed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    bridge_count: Mapped[int | None] = mapped_column(default=None)

    retry_count: Mapped[int] = mapped_column(default=0, server_default="0")
    exception_count: Mapped[int] = mapped_column(default=0, server_default="0")

    evidence_uri: Mapped[str | None] = mapped_column(String(1024), default=None)
    evidence_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    reconciled_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, default=None)
    signed_off_by: Mapped[str | None] = mapped_column(String(128), default=None)
    signed_off_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, default=None)
    notes: Mapped[str | None] = mapped_column(String(None), default=None)
