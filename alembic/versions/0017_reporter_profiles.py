"""public reporter profiles, scoring quota and visibility indexes

Revision ID: 0017_reporter_profiles
Revises: 0016_signal_indexes
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_reporter_profiles"
down_revision: Union[str, None] = "0016_signal_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _index_names(inspector, table: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table)}


def _create_visible_indexes() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("signal"):
        return
    indexes = _index_names(inspector, "signal")
    for name, columns in (
        ("ix_signal_first_seen_at", ["first_seen_at"]),
        ("ix_signal_region_first_seen", ["region", "first_seen_at"]),
        ("ix_signal_feed_order", ["batch_id", "final_score", "first_seen_at", "id"]),
    ):
        if name in indexes:
            op.drop_index(name, table_name="signal")
        op.create_index(
            name,
            "signal",
            columns,
            sqlite_where=sa.text("score >= 60"),
            postgresql_where=sa.text("score >= 60"),
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    is_postgres = bind.dialect.name == "postgresql"

    if inspector.has_table("user_profile"):
        columns = {column["name"] for column in inspector.get_columns("user_profile")}
        if "display_name" not in columns:
            with op.batch_alter_table("user_profile") as batch_op:
                batch_op.add_column(
                    sa.Column(
                        "display_name",
                        sa.String(length=64),
                        nullable=False,
                        server_default="",
                    )
                )

    if not inspector.has_table("reporter_scoring_usage"):
        op.create_table(
            "reporter_scoring_usage",
            sa.Column("user_id", sa.String(length=128), primary_key=True),
            sa.Column("usage_date", sa.String(length=10), primary_key=True),
            sa.Column(
                "scored_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    if inspector.has_table("signal"):
        indexes = _index_names(inspector, "signal")
        if "ix_signal_reporter_first_seen" not in indexes:
            op.create_index(
                "ix_signal_reporter_first_seen",
                "signal",
                ["reported_by", "first_seen_at"],
            )
        _create_visible_indexes()
        # pg_trgm indexes are created by ``create_backend_indexes`` after
        # migrations.  Drop the old full-table copies so bootstrap recreates
        # them with the visibility predicate instead of leaving the stale ones.
        if is_postgres:
            indexes = _index_names(inspector, "signal")
            for name in (
                "ix_signal_title_search_trgm",
                "ix_signal_content_search_trgm",
            ):
                if name in indexes:
                    op.drop_index(name, table_name="signal")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    is_postgres = bind.dialect.name == "postgresql"

    if inspector.has_table("reporter_scoring_usage"):
        op.drop_table("reporter_scoring_usage")

    if inspector.has_table("signal"):
        indexes = _index_names(inspector, "signal")
        if "ix_signal_reporter_first_seen" in indexes:
            op.drop_index("ix_signal_reporter_first_seen", table_name="signal")
        for name, columns in (
            ("ix_signal_first_seen_at", ["first_seen_at"]),
            ("ix_signal_region_first_seen", ["region", "first_seen_at"]),
            ("ix_signal_feed_order", ["batch_id", "final_score", "first_seen_at", "id"]),
        ):
            if name in indexes:
                op.drop_index(name, table_name="signal")
            op.create_index(name, "signal", columns)
        if is_postgres:
            indexes = _index_names(inspector, "signal")
            for name, column in (
                ("ix_signal_title_search_trgm", "title_search"),
                ("ix_signal_content_search_trgm", "content_search"),
            ):
                if name not in indexes:
                    op.create_index(
                        name,
                        "signal",
                        [column],
                        postgresql_using="gin",
                        postgresql_ops={column: "gin_trgm_ops"},
                    )

    if inspector.has_table("user_profile"):
        columns = {column["name"] for column in inspector.get_columns("user_profile")}
        if "display_name" in columns:
            with op.batch_alter_table("user_profile") as batch_op:
                batch_op.drop_column("display_name")

