"""Add session_results.race_time_ms.

Needed for the margin-of-victory special — stores each classified
finisher's raw race time in milliseconds, parsed from Jolpica's
Time.millis field. Computing margin between P1 and P2 just requires
subtraction.

Revision ID: 20260516_racetime
Revises: 20260515_laps
Create Date: 2026-05-16
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260516_racetime"
down_revision = "20260515_laps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_results",
        sa.Column("race_time_ms", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session_results", "race_time_ms")
