"""github translation queue

Revision ID: 0011_github_translation
Revises: 0010_github_push
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_github_translation"
down_revision: Union[str, None] = "0010_github_push"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("github_translation"):
        return
    op.create_table(
        "github_translation",
        sa.Column("url", sa.String(512), primary_key=True),
        sa.Column("locale", sa.String(8), primary_key=True),
        sa.Column("full_name", sa.String(256), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_github_translation_status", "github_translation", ["status"])
    op.create_index(
        "ix_github_translation_updated_at", "github_translation", ["updated_at"]
    )


def downgrade() -> None:
    op.drop_table("github_translation")