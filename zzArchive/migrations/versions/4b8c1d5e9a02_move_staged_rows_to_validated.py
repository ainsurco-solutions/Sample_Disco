from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = '4b8c1d5e9a02'
down_revision: str | None = '2e4122b688e7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.execute(
        "UPDATE migration_file SET state = 'VALIDATED' WHERE state = 'STAGED'"
    )

def downgrade() -> None:
    pass
