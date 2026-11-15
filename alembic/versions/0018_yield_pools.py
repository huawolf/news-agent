"""yield pool observations uploaded by crawlers

Revision ID: 0018_yield_pools
Revises: 0017_reporter_profiles
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_yield_pools"
down_revision: Union[str, None] = "0017_reporter_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("yield_pool"):
        return

    op.create_table(
        "yield_pool",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("pool_name", sa.String(length=256), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("reference", sa.String(length=128), nullable=False),
        sa.Column("protocol", sa.String(length=128), nullable=False),
        sa.Column("chain", sa.String(length=64), nullable=False),
        sa.Column("tvl_usd", sa.Float(), nullable=False),
        sa.Column("apy_30d", sa.Float(), nullable=False),
        sa.Column("apy_today", sa.Float(), nullable=False),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("reporter", sa.String(length=128), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "pool_name", "chain", "protocol", name="uq_yield_pool_identity"
        ),
    )
    op.create_index("ix_yield_pool_recorded_at", "yield_pool", ["recorded_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("yield_pool"):
        return
    indexes = {index["name"] for index in inspector.get_indexes("yield_pool")}
    if "ix_yield_pool_recorded_at" in indexes:
        op.drop_index("ix_yield_pool_recorded_at", table_name="yield_pool")
    op.drop_table("yield_pool")
