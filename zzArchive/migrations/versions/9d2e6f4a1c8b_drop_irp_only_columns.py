from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '9d2e6f4a1c8b'
down_revision: str | None = '7c3f1a9e2b4d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.drop_column('migration_file', 'server_id')
    op.drop_column('migration_file', 'exposure_set_uri')
    op.drop_column('migration_file', 'exposure_set_id')
    op.drop_column('migration_file', 'folder_id')

def downgrade() -> None:
    op.add_column('migration_file', sa.Column('folder_id', sa.String(length=128), nullable=True))
    op.add_column(
        'migration_file', sa.Column('exposure_set_id', sa.String(length=128), nullable=True)
    )
    op.add_column(
        'migration_file', sa.Column('exposure_set_uri', sa.String(length=512), nullable=True)
    )
    op.add_column('migration_file', sa.Column('server_id', sa.Integer(), nullable=True))
