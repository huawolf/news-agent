"""github push history

Revision ID: 0010_github_push
Revises: 0009_nullable_signal_url
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_github_push"
down_revision: Union[str, None] = "0009_nullable_signal_url"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("github_push"):
        return
    op.create_table(
        "github_push",
        sa.Column("url", sa.String(512), primary_key=True),
        sa.Column("full_name", sa.String(256), nullable=False, server_default=""),
        sa.Column("pushed_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("github_push")
