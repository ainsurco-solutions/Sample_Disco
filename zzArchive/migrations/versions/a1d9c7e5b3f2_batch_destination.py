from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1d9c7e5b3f2"
down_revision: str | None = "e8b2f4a6c1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column(
        "batch",
        sa.Column("destination", sa.String(length=16), nullable=False, server_default="VAULT"),
    )

def downgrade() -> None:
    with op.batch_alter_table("batch") as batch_op:
        batch_op.drop_column("destination")
