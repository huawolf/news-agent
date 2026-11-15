"""improve signal query indexes

Revision ID: 0016_signal_indexes
Revises: 0015_user_signal_settings
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_signal_indexes"
down_revision: Union[str, None] = "0015_user_signal_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _index_names(inspector, table: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    is_postgres = bind.dialect.name == "postgresql"

    if inspector.has_table("signal"):
        indexes = _index_names(inspector, "signal")
        unique_constraints = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("signal")
        }

        # 0001 created a unique constraint on ``url``; 0009 added a second
        # unique index with the same meaning.  Keep ``uq_signal_url`` and drop
        # the legacy copy so inserts update only one URL uniqueness structure.
        if "signal_url_key" in unique_constraints:
            if is_postgres:
                op.drop_constraint("signal_url_key", "signal", type_="unique")
            else:
                with op.batch_alter_table("signal") as batch_op:
                    batch_op.drop_constraint("signal_url_key", type_="unique")
        elif "signal_url_key" in indexes:
            op.drop_index("signal_url_key", table_name="signal")

        if "ix_signal_feed_order" not in indexes:
            op.create_index(
                "ix_signal_feed_order",
                "signal",
                ["batch_id", "final_score", "first_seen_at", "id"],
            )
        if is_postgres and "ix_signal_sectors_gin" not in indexes:
            op.create_index(
                "ix_signal_sectors_gin",
                "signal",
                ["sectors"],
                postgresql_using="gin",
            )

    if inspector.has_table("signal_translation"):
        indexes = _index_names(inspector, "signal_translation")
        if "ix_signal_translation_sync" not in indexes:
            op.create_index(
                "ix_signal_translation_sync",
                "signal_translation",
                ["locale", "status", "updated_at"],
            )

    if inspector.has_table("energy_ledger"):
        indexes = _index_names(inspector, "energy_ledger")
        # ``(user_id, created_at)`` already serves every lookup by user_id.  The
        # standalone index only adds write amplification.
        if "ix_energy_ledger_user_id" in indexes:
            op.drop_index("ix_energy_ledger_user_id", table_name="energy_ledger")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    is_postgres = bind.dialect.name == "postgresql"

    if inspector.has_table("energy_ledger"):
        indexes = _index_names(inspector, "energy_ledger")
        if "ix_energy_ledger_user_id" not in indexes:
            op.create_index("ix_energy_ledger_user_id", "energy_ledger", ["user_id"])

    if inspector.has_table("signal_translation"):
        indexes = _index_names(inspector, "signal_translation")
        if "ix_signal_translation_sync" in indexes:
            op.drop_index(
                "ix_signal_translation_sync", table_name="signal_translation"
            )

    if inspector.has_table("signal"):
        indexes = _index_names(inspector, "signal")
        if is_postgres and "ix_signal_sectors_gin" in indexes:
            op.drop_index("ix_signal_sectors_gin", table_name="signal")
        if "ix_signal_feed_order" in indexes:
            op.drop_index("ix_signal_feed_order", table_name="signal")
