"""Global leaderboard opt-in flag on users.

Existing users default to shown; the global page tells them how to opt out.

Revision ID: 20261111_global
Revises: 20261110_seats
Create Date: 2026-11-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20261111_global"
down_revision = "20261110_seats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("show_on_global", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("users", "show_on_global")
