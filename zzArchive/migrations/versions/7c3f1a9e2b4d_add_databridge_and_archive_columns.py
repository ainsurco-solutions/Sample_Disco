from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

UTC_TIMESTAMP = sa.DateTime().with_variant(mssql.DATETIME2(), "mssql")

revision: str = '7c3f1a9e2b4d'
down_revision: str | None = '3405822f52ca'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column('migration_file', sa.Column('instance_name', sa.String(length=256), nullable=True))
    op.add_column('migration_file', sa.Column('database_name', sa.String(length=256), nullable=True))
    op.add_column('migration_file', sa.Column('uploaded_at', UTC_TIMESTAMP, nullable=True))
    op.add_column('migration_file', sa.Column('archive_job_id', sa.String(length=128), nullable=True))
    op.add_column('migration_file', sa.Column('archived_at', UTC_TIMESTAMP, nullable=True))
    op.add_column(
        'migration_file', sa.Column('archive_expiration_date', UTC_TIMESTAMP, nullable=True)
    )

def downgrade() -> None:
    op.drop_column('migration_file', 'archive_expiration_date')
    op.drop_column('migration_file', 'archived_at')
    op.drop_column('migration_file', 'archive_job_id')
    op.drop_column('migration_file', 'uploaded_at')
    op.drop_column('migration_file', 'database_name')
    op.drop_column('migration_file', 'instance_name')
