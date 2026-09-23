from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5a7e3d91f20"
down_revision: str | None = "4b8c1d5e9a02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column(
        "migration_file",
        sa.Column("archive_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "batch_run",
        sa.Column("archive_failed_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("batch_run", sa.Column("bridge_count", sa.Integer(), nullable=True))

    op.execute(
        "UPDATE migration_file SET state = 'ARCHIVING' "
        "WHERE state = 'COMPLETED' AND instance_name IS NOT NULL "
        "AND archived_at IS NULL AND archive_job_id IS NOT NULL"
    )
    op.execute(
        "UPDATE migration_file SET state = 'BRIDGED' "
        "WHERE state = 'COMPLETED' AND instance_name IS NOT NULL "
        "AND archived_at IS NULL AND archive_job_id IS NULL"
    )

def downgrade() -> None:
    op.execute(
        "UPDATE migration_file SET state = 'COMPLETED' "
        "WHERE state IN ('BRIDGED', 'ARCHIVING', 'ARCHIVE_FAILED')"
    )
    op.drop_column("batch_run", "bridge_count")
    op.drop_column("batch_run", "archive_failed_count")
    op.drop_column("migration_file", "archive_attempts")
