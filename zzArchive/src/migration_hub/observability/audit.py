from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from migration_hub.core.models import ApiTransaction, MigrationFile
from migration_hub.core.scrub import scrub_credentials
from migration_hub.observability.endpoints import endpoint_key
from migration_hub.observability.redaction import redact_mapping, redact_text

def _serialize(body: object | None) -> str | None:
    if body is None:
        return None
    if isinstance(body, str):
        return redact_text(body)
    return json.dumps(redact_mapping(body))

def record_call(
    *,
    engine: Engine,
    file_id: int | None,
    method: str,
    url: str,
    request_body: object | None = None,
    response_body: object | None = None,
    status_code: int | None = None,
    duration_ms: int | None = None,
    correlation_id: str | None = None,
) -> None:
    with Session(engine) as session, session.begin():
        session.add(
            ApiTransaction(
                file_id=file_id,
                method=method,
                url=scrub_credentials(url),
                status_code=status_code,
                duration_ms=duration_ms,
                request_body=_serialize(request_body),
                response_body=_serialize(response_body),
                correlation_id=correlation_id,
            )
        )

@dataclass
class _CallRecord:

    request_body: object | None = None
    response_body: object | None = None
    status_code: int | None = None
    correlation_id: str | None = None
    _error: str | None = field(default=None, init=False, repr=False)

@contextmanager
def audited_call(
    *, engine: Engine, file_id: int | None, method: str, url: str
) -> Iterator[_CallRecord]:
    record = _CallRecord()
    start = time.monotonic()
    try:
        yield record
    except Exception as exc:
        record._error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        record_call(
            engine=engine,
            file_id=file_id,
            method=method,
            url=url,
            request_body=record.request_body,
            response_body=record.response_body if record._error is None else record._error,
            status_code=record.status_code,
            duration_ms=duration_ms,
            correlation_id=record.correlation_id,
        )
        _log_call(
            file_id=file_id,
            method=method,
            url=url,
            status_code=record.status_code,
            duration_ms=duration_ms,
            response_body=record.response_body,
            error=record._error,
        )

_log = logging.getLogger(__name__)

_XML_CODE = re.compile(r"<Code>([^<]{1,80})</Code>")

_REASON_KEYS = ("code", "errorCode", "title", "message", "detail", "error")

def _reason(response_body: object) -> str:
    if isinstance(response_body, str):
        match = _XML_CODE.search(response_body)
        return match.group(1) if match else response_body.strip()[:120]
    if isinstance(response_body, dict):
        for key in _REASON_KEYS:
            value = response_body.get(key)
            if isinstance(value, str | int) and str(value):
                return str(value)[:120]
    return ""

def _log_call(
    *,
    file_id: int | None,
    method: str,
    url: str,
    status_code: int | None,
    duration_ms: int,
    response_body: object,
    error: str | None,
) -> None:
    who = f"file {file_id}" if file_id is not None else "no file"
    status = str(status_code) if status_code is not None else "no response"
    failed = error is not None or status_code is None or status_code >= 400
    reason = (error or _reason(response_body)) if failed else ""
    _log.log(
        logging.WARNING if failed else logging.INFO,
        "%s  %s %s  %s  %d ms%s",
        who,
        method,
        endpoint_key(url),
        status,
        duration_ms,
        f"  {reason}" if reason else "",
    )

@contextmanager
def maybe_audited_call(
    *, engine: Engine | None, file_id: int | None, method: str, url: str
) -> Iterator[_CallRecord]:
    if engine is None:
        yield _CallRecord()
        return
    with audited_call(engine=engine, file_id=file_id, method=method, url=url) as call:
        yield call

def export_for_support(*, engine: Engine, file_id: int) -> dict[str, object]:
    with Session(engine) as session:
        file = session.get(MigrationFile, file_id)
        if file is None:
            raise ValueError(f"No migration_file with file_id={file_id}")

        transactions = list(
            session.scalars(
                select(ApiTransaction)
                .where(ApiTransaction.file_id == file_id)
                .order_by(ApiTransaction.transaction_id)
            )
        )

        return {
            "file_id": file_id,
            "source_database": file.source_database,
            "target_exposure_name": file.target_exposure_name,
            "state": file.state,
            "instance_name": file.instance_name,
            "database_name": file.database_name,
            "job_id": file.job_id,
            "archive_job_id": file.archive_job_id,
            "last_error": file.last_error,
            "transactions": [
                {
                    "occurred_at": t.occurred_at.isoformat(),
                    "method": t.method,
                    "url": t.url,
                    "status_code": t.status_code,
                    "duration_ms": t.duration_ms,
                    "correlation_id": t.correlation_id,
                }
                for t in transactions
            ],
        }
