"""drop note from watchlist"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_drop_watchlist_note"
down_revision: Union[str, None] = "0006_watchlist_aliases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The free-text note was never read by matching, ranking or push, so it is
    # dropped along with the console input that filled it.
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("watchlist") as batch_op:
            batch_op.drop_column("note")
    else:
        op.drop_column("watchlist", "note")


def downgrade() -> None:
    op.add_column(
        "watchlist",
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("watchlist") as batch_op:
            batch_op.alter_column("note", server_default=None)
    else:
        op.alter_column("watchlist", "note", server_default=None)
