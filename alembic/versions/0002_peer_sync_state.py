"""per-slaver sync status

Revision ID: 0002_peer_sync_state
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_peer_sync_state"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("peer_sync_state"):
        return
    op.create_table(
        "peer_sync_state",
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("last_pulled_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("peer_sync_state")
