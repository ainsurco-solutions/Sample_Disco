from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '2e4122b688e7'
down_revision: str | None = '9d2e6f4a1c8b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.drop_column('migration_file', 'staged_path')

def downgrade() -> None:
    op.add_column(
        'migration_file', sa.Column('staged_path', sa.String(length=1024), nullable=True)
    )
