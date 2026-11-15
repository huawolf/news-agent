"""allow URL-less signals"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_nullable_signal_url"
down_revision: Union[str, None] = "0008_account_profile_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("signal") as batch_op:
        batch_op.alter_column(
            "url",
            existing_type=sa.Text(),
            nullable=True,
        )
    op.create_index("uq_signal_url", "signal", ["url"], unique=True)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM signal WHERE url IS NULL LIMIT 1")).first():
        raise RuntimeError("cannot restore NOT NULL while URL-less signals exist")
    op.drop_index("uq_signal_url", table_name="signal")
    with op.batch_alter_table("signal") as batch_op:
        batch_op.alter_column(
            "url",
            existing_type=sa.Text(),
            nullable=False,
        )
