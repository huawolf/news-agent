"""add aliases to watchlist"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_watchlist_aliases"
down_revision: Union[str, None] = "0005_signal_translation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # Aliases (e.g. 英伟达 / Nvidia) sit next to the canonical asset symbol so the
    # console can show how a pasted phrase was recognised. Non-null with a
    # server default so existing rows gain an empty list.
    op.add_column(
        "watchlist",
        sa.Column("aliases", JSONType, nullable=False, server_default="[]"),
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("watchlist") as batch_op:
            batch_op.alter_column("aliases", server_default=None)
    else:
        op.alter_column("watchlist", "aliases", server_default=None)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("watchlist") as batch_op:
            batch_op.drop_column("aliases")
    else:
        op.drop_column("watchlist", "aliases")
