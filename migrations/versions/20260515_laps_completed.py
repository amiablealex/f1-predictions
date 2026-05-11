"""Add session_results.laps_completed.

Needed for "first retirement" and "longest stint" specials — captures
the number of laps each driver completed in a race or sprint race.

Revision ID: 20260515_laps
Revises: 20260514_phase4
Create Date: 2026-05-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260515_laps"
down_revision = "20260514_phase4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_results",
        sa.Column("laps_completed", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session_results", "laps_completed")
