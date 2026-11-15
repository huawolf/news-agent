"""cache verified account energy and invite code"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_account_profile_fields"
down_revision: Union[str, None] = "0007_drop_watchlist_note"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_profile", sa.Column("energy_cached", sa.Float(), nullable=True))
    op.add_column(
        "user_profile",
        sa.Column("self_code", sa.String(length=32), nullable=False, server_default=""),
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("user_profile") as batch_op:
            batch_op.alter_column("self_code", server_default=None)
    else:
        op.alter_column("user_profile", "self_code", server_default=None)


def downgrade() -> None:
    op.drop_column("user_profile", "self_code")
    op.drop_column("user_profile", "energy_cached")
