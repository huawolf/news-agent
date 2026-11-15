"""github translation display name

Revision ID: 0012_github_translation_name
Revises: 0011_github_translation
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_github_translation_name"
down_revision: Union[str, None] = "0011_github_translation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("github_translation"):
        return
    columns = {col["name"] for col in inspector.get_columns("github_translation")}
    if "name" in columns:
        return
    op.add_column(
        "github_translation",
        sa.Column("name", sa.String(256), nullable=False, server_default=""),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("github_translation"):
        return
    columns = {col["name"] for col in inspector.get_columns("github_translation")}
    if "name" not in columns:
        return
    op.drop_column("github_translation", "name")