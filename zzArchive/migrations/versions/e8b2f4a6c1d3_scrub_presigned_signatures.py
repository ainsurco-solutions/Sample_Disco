from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8b2f4a6c1d3"
down_revision: str | None = "c5a7e3d91f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MARKERS = (
    "X-Amz-Signature=",
    "X-Amz-Credential=",
    "X-Amz-Security-Token=",
    "Signature=",
    "AWSAccessKeyId=",
)
_URL = re.compile(r"https?://[^\s'\"<>]+")

_TARGETS = (
    ("migration_file", "file_id", ("last_error",)),
    ("migration_event", "event_id", ("detail",)),
    ("api_transaction", "transaction_id", ("url", "response_body")),
)

def _scrub(text: str) -> str:
    def one(match: re.Match[str]) -> str:
        base, sep, query = match.group(0).partition("?")
        if sep and any(marker in query for marker in _MARKERS):
            return base + "?<redacted>"
        return match.group(0)

    return _URL.sub(one, text)

def upgrade() -> None:
    bind = op.get_bind()
    for table, key, columns in _TARGETS:
        for column in columns:
            rows = bind.execute(
                sa.text(
                    f"SELECT {key}, {column} FROM {table} "
                    f"WHERE {column} LIKE '%Signature=%' OR {column} LIKE '%AWSAccessKeyId=%'"
                )
            ).fetchall()
            for row_id, value in rows:
                cleaned = _scrub(value)
                if cleaned != value:
                    bind.execute(
                        sa.text(f"UPDATE {table} SET {column} = :v WHERE {key} = :k"),
                        {"v": cleaned, "k": row_id},
                    )

def downgrade() -> None:
    pass
