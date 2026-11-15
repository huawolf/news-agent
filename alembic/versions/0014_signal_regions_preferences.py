"""signal regions and user preferences

Revision ID: 0014_signal_regions
Revises: 0013_signal_push
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_signal_regions"
down_revision: Union[str, None] = "0013_signal_push"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("signal"):
        columns = {column["name"] for column in inspector.get_columns("signal")}
        if "region" not in columns:
            op.add_column("signal", sa.Column("region", sa.String(16), nullable=True))
        indexes = {index["name"] for index in inspector.get_indexes("signal")}
        if "ix_signal_region_first_seen" not in indexes:
            op.create_index(
                "ix_signal_region_first_seen",
                "signal",
                ["region", "first_seen_at"],
            )

    if not inspector.has_table("user_preference"):
        op.create_table(
            "user_preference",
            sa.Column("user_id", sa.String(128), primary_key=True),
            sa.Column("regions", JSONType, nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    # The old default taxonomy split frontier technology and AI.  Existing
    # installs are mostly test data, but the sector rows themselves are live
    # configuration and must reflect the new five-sector contract.
    if inspector.has_table("sector"):
        sector = sa.table(
            "sector",
            sa.column("name", sa.String(64)),
            sa.column("keywords", JSONType),
            sa.column("enabled", sa.Boolean()),
        )
        frontier_keywords = [
            "quantum computing",
            "quantum technology",
            "ai drug discovery",
            "ai biology",
            "synthetic biology",
            "space technology",
            "commercial space",
            "embodied intelligence",
            "humanoid robot",
            "artificial intelligence",
            "foundation model",
            "ai agent",
            "nuclear fusion",
            "fusion energy",
        ]
        op.execute(
            sector.update()
            .where(sector.c.name == "前沿科技")
            .values(keywords=frontier_keywords)
        )
        op.execute(
            sector.update()
            .where(sector.c.name == "AI技术")
            .values(enabled=False)
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("user_preference"):
        op.drop_table("user_preference")
    if inspector.has_table("signal"):
        indexes = {index["name"] for index in inspector.get_indexes("signal")}
        if "ix_signal_region_first_seen" in indexes:
            op.drop_index("ix_signal_region_first_seen", table_name="signal")
        columns = {column["name"] for column in inspector.get_columns("signal")}
        if "region" in columns:
            with op.batch_alter_table("signal") as batch_op:
                batch_op.drop_column("region")
