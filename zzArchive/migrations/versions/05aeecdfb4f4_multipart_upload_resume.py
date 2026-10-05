from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

revision: str = "05aeecdfb4f4"
down_revision: str | None = "b7e4c2d9f1a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UTC_TIMESTAMP = sa.DateTime().with_variant(mssql.DATETIME2(), "mssql")

def utc_now() -> sa.sql.elements.TextClause:
    if op.get_bind().dialect.name == "sqlite":
        return sa.text("CURRENT_TIMESTAMP")
    return sa.text("sysutcdatetime()")

def upgrade() -> None:
    op.add_column("migration_file", sa.Column("bytes_at_start", sa.BigInteger(), nullable=True))
    op.create_table(
        "multipart_upload",
        sa.Column("file_id", sa.BigInteger(), nullable=False),
        sa.Column("upload_id", sa.String(length=512), nullable=False),
        sa.Column("chunk_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_size", sa.BigInteger(), nullable=False),
        sa.Column("source_mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("instance_name", sa.String(length=256), nullable=False),
        sa.Column("database_name", sa.String(length=256), nullable=False),
        sa.Column("file_extension", sa.String(length=16), nullable=False),
        sa.Column("created_at", UTC_TIMESTAMP, server_default=utc_now(), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["migration_file.file_id"]),
        sa.PrimaryKeyConstraint("file_id"),
    )
    op.create_table(
        "multipart_upload_part",
        sa.Column("file_id", sa.BigInteger(), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("etag", sa.String(length=256), nullable=False),
        sa.Column("completed_at", UTC_TIMESTAMP, server_default=utc_now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["file_id"], ["multipart_upload.file_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("file_id", "part_number"),
    )

def downgrade() -> None:
    op.drop_table("multipart_upload_part")
    op.drop_table("multipart_upload")
    with op.batch_alter_table("migration_file") as batch_op:
        batch_op.drop_column("bytes_at_start")
