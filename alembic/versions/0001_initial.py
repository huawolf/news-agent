"""initial signal schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "signal",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(64), nullable=False),
        sa.Column("source_urls", JSONType, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("title_search", sa.Text(), nullable=False),
        sa.Column("content_search", sa.Text(), nullable=False),
        sa.Column("keywords", JSONType, nullable=False),
        sa.Column("source_name", sa.String(256), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("sectors", JSONType, nullable=False),
        sa.Column("assets", JSONType, nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(128), nullable=False),
        sa.Column("reported_by", sa.String(128), nullable=True),
        sa.Column("is_github", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_signal_url_hash", "signal", ["url_hash"])
    op.create_index("ix_signal_published_at", "signal", ["published_at"])
    op.create_index("ix_signal_first_seen_at", "signal", ["first_seen_at"])
    op.create_index("ix_signal_score", "signal", ["score"])
    op.create_index("ix_signal_origin_first_seen", "signal", ["origin", "first_seen_at"])

    op.create_table(
        "watchlist",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("asset", sa.String(64), nullable=False),
        sa.Column("asset_class", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("owner", "asset", name="uq_watchlist_owner_asset"),
    )

    op.create_table(
        "sector",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("keywords", JSONType, nullable=False),
        sa.Column("sort_weight", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.UniqueConstraint("name", name="uq_sector_name"),
    )

    op.create_table(
        "fetch_source",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("xml_url", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("params", JSONType, nullable=False),
        sa.Column("etag", sa.Text(), nullable=False),
        sa.Column("last_modified", sa.Text(), nullable=False),
        sa.Column("last_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("owner", "kind", "key", name="uq_fetch_source_owner_kind_key"),
    )

    op.create_table(
        "sync_state",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("last_pulled_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "app_state",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("seed_version", sa.Integer(), nullable=False),
    )

    op.create_table(
        "dedup_duplicate",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("candidate_url", sa.Text(), nullable=False),
        sa.Column("candidate_title", sa.Text(), nullable=False),
        sa.Column("matched_signal_id", sa.String(64), nullable=False),
        sa.Column("match_level", sa.String(16), nullable=False),
        sa.Column("decided_by", sa.String(16), nullable=False),
        sa.Column("bm25_score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_dedup_matched_signal_id", "dedup_duplicate", ["matched_signal_id"])
    op.create_index("ix_dedup_match_level", "dedup_duplicate", ["match_level"])
    op.create_index("ix_dedup_created_at", "dedup_duplicate", ["created_at"])

    op.create_table(
        "energy_ledger",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("signal_id", sa.String(64), nullable=True),
        sa.Column("delta", sa.Float(), nullable=False),
        sa.Column("balance_after", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_energy_ledger_user_id", "energy_ledger", ["user_id"])
    op.create_index("ix_energy_user_created", "energy_ledger", ["user_id", "created_at"])

    op.create_table(
        "user_profile",
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("username", sa.String(128), nullable=False),
        sa.Column("membership", sa.String(64), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "github_cache",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "local_setting",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "ix_signal_assets_gin", "signal", ["assets"], postgresql_using="gin"
        )


def downgrade() -> None:
    for table in (
        "local_setting",
        "github_cache",
        "user_profile",
        "energy_ledger",
        "dedup_duplicate",
        "app_state",
        "sync_state",
        "fetch_source",
        "sector",
        "watchlist",
        "signal",
    ):
        op.drop_table(table)