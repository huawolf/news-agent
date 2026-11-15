"""mirror portal signal settings on user preferences

Revision ID: 0015_user_signal_settings
Revises: 0014_signal_regions
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_user_signal_settings"
down_revision: Union[str, None] = "0014_signal_regions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("user_preference"):
        return
    columns = {column["name"] for column in inspector.get_columns("user_preference")}
    if "signal_settings" not in columns:
        op.add_column(
            "user_preference",
            sa.Column("signal_settings", JSONType, nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("user_preference"):
        return
    columns = {column["name"] for column in inspector.get_columns("user_preference")}
    if "signal_settings" in columns:
        with op.batch_alter_table("user_preference") as batch_op:
            batch_op.drop_column("signal_settings")
