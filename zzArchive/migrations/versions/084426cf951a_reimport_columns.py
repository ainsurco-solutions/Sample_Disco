from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "084426cf951a"
down_revision: str | None = "f3a8d6c2b9e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column(
        "migration_file",
        sa.Column("reimport_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "migration_file",
        sa.Column("reimport_attempts", sa.Integer(), nullable=False, server_default="0"),
    )

def downgrade() -> None:
    with op.batch_alter_table("migration_file") as batch_op:
        batch_op.drop_column("reimport_attempts")
        batch_op.drop_column("reimport_pending")
