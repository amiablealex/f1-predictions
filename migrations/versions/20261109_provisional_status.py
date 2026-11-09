"""Add PROVISIONAL to the session_status enum.

Uppercase to match what SQLAlchemy sends at runtime (enum .name), per
the 20260517_enumfix lesson.

Revision ID: 20261109_provisional
Revises: 19a8e1056539
Create Date: 2026-11-09
"""
from __future__ import annotations

from alembic import op


revision = "20261109_provisional"
down_revision = "19a8e1056539"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE session_status ADD VALUE IF NOT EXISTS 'PROVISIONAL'")


def downgrade() -> None:
    # Postgres can't remove enum values without recreating the type.
    pass
