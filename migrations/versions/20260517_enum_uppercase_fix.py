"""Add uppercase enum variants for new prediction types.

The prediction_type enum was created by SQLAlchemy using .name
(uppercase: RACE_TOP10). Later ALTER TYPE ADD VALUE migrations used
lowercase strings (the .value: places_gained). The app sends .name at
runtime, so the lowercase values are unreachable. This migration adds
uppercase variants for the affected types.

Revision ID: 20260517_enumfix
Revises: 20260516_racetime
Create Date: 2026-05-17
"""
from __future__ import annotations

from alembic import op


revision = "20260517_enumfix"
down_revision = "20260516_racetime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in (
        "PLACES_GAINED",
        "QUALI_RANDOM_DRIVER",
        "QUALI_HEAD_TO_HEAD",
        "QUALI_NTH",
        "SPECIAL",
    ):
        op.execute(
            f"ALTER TYPE prediction_type ADD VALUE IF NOT EXISTS '{name}'"
        )


def downgrade() -> None:
    # Postgres can't remove enum values without recreating the type.
    pass
