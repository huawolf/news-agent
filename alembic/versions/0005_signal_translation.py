"""add per-locale signal translations and locale-specific search columns"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_signal_translation"
down_revision: Union[str, None] = "0004_signal_batch_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # Pre-0005 rows are single-language, and their summary is exactly the
    # display layer now searchable in that locale, so backfill from it.
    op.add_column(
        "signal",
        sa.Column("summary_search", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "signal",
        sa.Column("keywords_search", sa.Text(), nullable=False, server_default=""),
    )
    op.execute("UPDATE signal SET summary_search = summary WHERE summary_search = ''")
    # `keywords_search` needs the tokeniser, which only runs in the app layer;
    # leaving it empty keeps recall working through `content_search` and the
    # next scoring pass fills it in.

    op.create_table(
        "signal_translation",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("signal_id", sa.String(64), nullable=False),
        sa.Column("locale", sa.String(8), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("keywords", JSONType, nullable=False),
        sa.Column("title_search", sa.Text(), nullable=False),
        sa.Column("summary_search", sa.Text(), nullable=False),
        sa.Column("keywords_search", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["signal_id"], ["signal.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("signal_id", "locale", name="uq_signal_translation_locale"),
    )
    op.create_index(
        "ix_signal_translation_status", "signal_translation", ["status"]
    )
    op.create_index(
        "ix_signal_translation_updated_at", "signal_translation", ["updated_at"]
    )

    # Late translations for already-pulled signals travel on their own cursor:
    # the signal cursor only advances on first_seen_at, so a translation that
    # completed later would never be delivered.
    op.add_column(
        "sync_state",
        sa.Column(
            "last_translation_pulled_at", sa.DateTime(), nullable=True
        ),
    )

    if op.get_bind().dialect.name == "sqlite":
        # SQLite keeps the column-level default in the CREATE TABLE statement and
        # has no cheap way to drop it, so the server default is dropped by
        # rewriting the table.
        with op.batch_alter_table("signal") as batch_op:
            batch_op.alter_column("summary_search", server_default=None)
            batch_op.alter_column("keywords_search", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_signal_translation_updated_at", table_name="signal_translation")
    op.drop_index("ix_signal_translation_status", table_name="signal_translation")
    op.drop_table("signal_translation")
    op.drop_column("sync_state", "last_translation_pulled_at")
    if op.get_bind().dialect.name == "sqlite":
        # SQLite cannot drop a column in place; the table is rebuilt instead.
        with op.batch_alter_table("signal") as batch_op:
            batch_op.drop_column("keywords_search")
            batch_op.drop_column("summary_search")
    else:
        op.drop_column("signal", "keywords_search")
        op.drop_column("signal", "summary_search")
