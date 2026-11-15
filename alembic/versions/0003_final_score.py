"""add final signal ranking score"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_final_score"
down_revision: Union[str, None] = "0002_peer_sync_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("signal", sa.Column("final_score", sa.Float(), nullable=False, server_default="0"))
    op.execute("UPDATE signal SET final_score = score")
    op.create_index("ix_signal_final_score", "signal", ["final_score"])


def downgrade() -> None:
    op.drop_index("ix_signal_final_score", table_name="signal")
    op.drop_column("signal", "final_score")
