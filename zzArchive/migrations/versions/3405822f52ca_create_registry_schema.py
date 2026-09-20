from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mssql

UTC_TIMESTAMP = sa.DateTime().with_variant(mssql.DATETIME2(), "mssql")
AUTO_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

UTC_NOW_MSSQL = sa.text("sysutcdatetime()")
UTC_NOW_SQLITE = sa.text("CURRENT_TIMESTAMP")

def utc_now() -> sa.sql.elements.TextClause:
    bind = op.get_bind()
    return UTC_NOW_SQLITE if bind.dialect.name == "sqlite" else UTC_NOW_MSSQL

revision: str = '3405822f52ca'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table('batch',
    sa.Column('batch_id', sa.String(length=64), nullable=False),
    sa.Column('description', sa.String(length=512), nullable=True),
    sa.Column('wave', sa.Integer(), nullable=True),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('max_concurrency', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', UTC_TIMESTAMP, server_default=utc_now(), nullable=False),
    sa.Column('started_at', UTC_TIMESTAMP, nullable=True),
    sa.Column('finished_at', UTC_TIMESTAMP, nullable=True),
    sa.PrimaryKeyConstraint('batch_id')
    )
    op.create_table('batch_run',
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('batch_id', sa.String(length=64), nullable=False),
    sa.Column('run_seq', sa.Integer(), nullable=False),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('initiated_by', sa.String(length=128), nullable=False),
    sa.Column('started_at', UTC_TIMESTAMP, server_default=utc_now(), nullable=False),
    sa.Column('finished_at', UTC_TIMESTAMP, nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('source_count', sa.Integer(), nullable=False),
    sa.Column('source_bytes', sa.BigInteger(), nullable=False),
    sa.Column('target_count', sa.Integer(), nullable=True),
    sa.Column('target_bytes', sa.BigInteger(), nullable=True),
    sa.Column('completed_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('abandoned_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('rejected_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('skipped_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('outstanding_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('retry_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('exception_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('evidence_uri', sa.String(length=1024), nullable=True),
    sa.Column('evidence_sha256', sa.String(length=64), nullable=True),
    sa.Column('reconciled_at', UTC_TIMESTAMP, nullable=True),
    sa.Column('signed_off_by', sa.String(length=128), nullable=True),
    sa.Column('signed_off_at', UTC_TIMESTAMP, nullable=True),
    sa.Column('notes', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['batch_id'], ['batch.batch_id'], ),
    sa.PrimaryKeyConstraint('run_id'),
    sa.UniqueConstraint('batch_id', 'run_seq')
    )
    op.create_table('migration_file',
    sa.Column('file_id', AUTO_PK, autoincrement=True, nullable=False),
    sa.Column('batch_id', sa.String(length=64), nullable=False),
    sa.Column('source_database', sa.String(length=256), nullable=False),
    sa.Column('source_path', sa.String(length=1024), nullable=False),
    sa.Column('staged_path', sa.String(length=1024), nullable=True),
    sa.Column('target_exposure_name', sa.String(length=256), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('checksum', sa.String(length=64), nullable=True),
    sa.Column('source_last_write', UTC_TIMESTAMP, nullable=True),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('claimed_by', sa.String(length=128), nullable=True),
    sa.Column('claimed_at', UTC_TIMESTAMP, nullable=True),
    sa.Column('folder_id', sa.String(length=128), nullable=True),
    sa.Column('exposure_set_id', sa.String(length=128), nullable=True),
    sa.Column('exposure_set_uri', sa.String(length=512), nullable=True),
    sa.Column('job_id', sa.String(length=128), nullable=True),
    sa.Column('server_id', sa.Integer(), nullable=True),
    sa.Column('last_error', sa.String(), nullable=True),
    sa.Column('created_at', UTC_TIMESTAMP, server_default=utc_now(), nullable=False),
    sa.Column('updated_at', UTC_TIMESTAMP, server_default=utc_now(), nullable=False),
    sa.ForeignKeyConstraint(['batch_id'], ['batch.batch_id'], ),
    sa.PrimaryKeyConstraint('file_id'),
    sa.UniqueConstraint('source_path'),
    sa.UniqueConstraint('target_exposure_name')
    )
    op.create_index(
        'ix_migration_file_state_batch_id',
        'migration_file',
        ['state', 'batch_id'],
        unique=False,
    )
    op.create_table('api_transaction',
    sa.Column('transaction_id', AUTO_PK, autoincrement=True, nullable=False),
    sa.Column('file_id', sa.BigInteger(), nullable=True),
    sa.Column('occurred_at', UTC_TIMESTAMP, server_default=utc_now(), nullable=False),
    sa.Column('method', sa.String(length=16), nullable=False),
    sa.Column('url', sa.String(length=2048), nullable=False),
    sa.Column('status_code', sa.Integer(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('request_body', sa.String(), nullable=True),
    sa.Column('response_body', sa.String(), nullable=True),
    sa.Column('correlation_id', sa.String(length=128), nullable=True),
    sa.ForeignKeyConstraint(['file_id'], ['migration_file.file_id'], ),
    sa.PrimaryKeyConstraint('transaction_id')
    )
    op.create_index(
        op.f('ix_api_transaction_correlation_id'),
        'api_transaction',
        ['correlation_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_api_transaction_file_id'),
        'api_transaction',
        ['file_id'],
        unique=False,
    )
    op.create_table('migration_event',
    sa.Column('event_id', AUTO_PK, autoincrement=True, nullable=False),
    sa.Column('file_id', sa.BigInteger(), nullable=False),
    sa.Column('occurred_at', UTC_TIMESTAMP, server_default=utc_now(), nullable=False),
    sa.Column('from_state', sa.String(length=32), nullable=True),
    sa.Column('to_state', sa.String(length=32), nullable=False),
    sa.Column('actor', sa.String(length=128), nullable=False),
    sa.Column('detail', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['file_id'], ['migration_file.file_id'], ),
    sa.PrimaryKeyConstraint('event_id')
    )
    op.create_index(
        op.f('ix_migration_event_file_id'),
        'migration_event',
        ['file_id'],
        unique=False,
    )

def downgrade() -> None:
    op.drop_index(op.f('ix_migration_event_file_id'), table_name='migration_event')
    op.drop_table('migration_event')
    op.drop_index(op.f('ix_api_transaction_file_id'), table_name='api_transaction')
    op.drop_index(op.f('ix_api_transaction_correlation_id'), table_name='api_transaction')
    op.drop_table('api_transaction')
    op.drop_index('ix_migration_file_state_batch_id', table_name='migration_file')
    op.drop_table('migration_file')
    op.drop_table('batch_run')
    op.drop_table('batch')
