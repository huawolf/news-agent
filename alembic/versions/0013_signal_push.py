"""signal push history

Revision ID: 0013_signal_push
Revises: 0012_github_translation_name
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_signal_push"
down_revision: Union[str, None] = "0012_github_translation_name"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("signal_push"):
        return
    op.create_table(
        "signal_push",
        sa.Column("signal_id", sa.String(64), primary_key=True),
        sa.Column("url_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("title", sa.Text, nullable=False, server_default=""),
        sa.Column("section", sa.String(16), nullable=False, server_default=""),
        sa.Column("locale", sa.String(8), nullable=False, server_default=""),
        sa.Column("pushed_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_signal_push_pushed_at", "signal_push", ["pushed_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_signal_push_pushed_at", table_name="signal_push")
    op.drop_table("signal_push")
