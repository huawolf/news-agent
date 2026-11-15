"""add acquisition-cycle batch identity to signals"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_signal_batch_id"
down_revision: Union[str, None] = "0003_final_score"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("signal", sa.Column("batch_id", sa.String(length=64), nullable=True))
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        expression = "'0000-legacy-' || to_char(first_seen_at AT TIME ZONE 'UTC', 'YYYYMMDD')"
    else:
        expression = "'0000-legacy-' || strftime('%Y%m%d', first_seen_at)"
    op.execute(f"UPDATE signal SET batch_id = {expression} WHERE batch_id IS NULL")
    if dialect == "sqlite":
        with op.batch_alter_table("signal") as batch_op:
            batch_op.alter_column("batch_id", nullable=False)
    else:
        op.alter_column("signal", "batch_id", nullable=False)
    op.create_index("ix_signal_batch_id", "signal", ["batch_id"])


def downgrade() -> None:
    op.drop_index("ix_signal_batch_id", table_name="signal")
    op.drop_column("signal", "batch_id")
