from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a8d6c2b9e1"
down_revision: str | None = "05aeecdfb4f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "migration_file"
_COLUMNS = ("source_path", "target_exposure_name")
_NOT_REMOVED = "state <> 'REMOVED'"

def _index_name(column: str) -> str:
    return f"uq_{_TABLE}_{column}"

def _drop_unique_constraints() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            _TABLE,
            recreate="always",
            naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
        ) as batch_op:
            for column in _COLUMNS:
                batch_op.drop_constraint(_index_name(column), type_="unique")
        return
    for constraint in sa.inspect(bind).get_unique_constraints(_TABLE):
        if tuple(constraint["column_names"]) in {(c,) for c in _COLUMNS}:
            op.drop_constraint(constraint["name"], _TABLE, type_="unique")

def upgrade() -> None:
    _drop_unique_constraints()
    for column in _COLUMNS:
        op.create_index(
            _index_name(column),
            _TABLE,
            [column],
            unique=True,
            sqlite_where=sa.text(_NOT_REMOVED),
            mssql_where=sa.text(_NOT_REMOVED),
        )

def downgrade() -> None:
    bind = op.get_bind()
    for column in _COLUMNS:
        clash = bind.execute(
            sa.text(
                f"SELECT {column} FROM {_TABLE} GROUP BY {column} HAVING COUNT(*) > 1"
            )
        ).first()
        if clash is not None:
            raise RuntimeError(
                f"cannot downgrade: {column} {clash[0]!r} is held by more than one row "
                "(a REMOVED file registered again)"
            )
    for column in _COLUMNS:
        op.drop_index(_index_name(column), table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch_op:
        for column in _COLUMNS:
            batch_op.create_unique_constraint(_index_name(column), [column])
