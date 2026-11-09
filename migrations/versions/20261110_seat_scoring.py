"""Seat scoring: team on session results, deferrable lineup uniqueness.

- session_results.constructor_name: seat resolution matches stand-ins to
  seats by team, since car numbers follow drivers.
- uq_round_driver becomes DEFERRABLE INITIALLY DEFERRED so the lineup
  editor can move drivers between seats in one transaction.

Revision ID: 20261110_seats
Revises: 20261109_provisional
Create Date: 2026-11-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20261110_seats"
down_revision = "20261109_provisional"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_results",
        sa.Column("constructor_name", sa.String(length=80), nullable=True),
    )
    op.drop_constraint("uq_round_driver", "round_drivers", type_="unique")
    op.create_unique_constraint(
        "uq_round_driver", "round_drivers", ["round_id", "expected_driver_id"],
        deferrable=True, initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint("uq_round_driver", "round_drivers", type_="unique")
    op.create_unique_constraint(
        "uq_round_driver", "round_drivers", ["round_id", "expected_driver_id"],
    )
    op.drop_column("session_results", "constructor_name")
