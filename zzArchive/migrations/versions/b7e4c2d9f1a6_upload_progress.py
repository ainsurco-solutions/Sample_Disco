from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e4c2d9f1a6"
down_revision: str | None = "a1d9c7e5b3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column("migration_file", sa.Column("bytes_sent", sa.BigInteger(), nullable=True))
    op.add_column("migration_file", sa.Column("upload_started_at", sa.DateTime(), nullable=True))
    op.add_column("migration_file", sa.Column("bytes_sent_at", sa.DateTime(), nullable=True))

def downgrade() -> None:
    with op.batch_alter_table("migration_file") as batch_op:
        batch_op.drop_column("bytes_sent_at")
        batch_op.drop_column("upload_started_at")
        batch_op.drop_column("bytes_sent")
