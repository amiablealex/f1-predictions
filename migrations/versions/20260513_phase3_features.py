"""Phase 3 features: places gained, random quali driver, bucketed quali scoring.

Schema changes:
  - round_scoring_configs: add quali_position_buckets (JSON, NOT NULL),
    drop quali_top3_correct + quali_top3_one_off.
  - session_results: add grid_position (Integer, nullable).
  - rounds: add random_quali_driver_id (FK → round_drivers.id, SET NULL).
  - prediction_type enum: add 'places_gained' and 'quali_random_driver'.
  - new tables: predictions_places_gained, predictions_quali_random_driver.

Data backfill:
  - Existing round_scoring_configs rows get the new bucket scheme.
  - Each existing Round with a populated lineup gets a random quali driver
    assigned (so rounds synced before this deploy don't go without one).

Revision ID: 20260513_phase3
Revises: <PASTE_CURRENT_HEAD_HERE>
Create Date: 2026-05-13
"""
from __future__ import annotations

import json
import random as random_mod

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import text


# revision identifiers, used by Alembic.
revision = "20260513_phase3"
down_revision = "9fd2b05af6d7"  # TODO: see deployment guide
branch_labels = None
depends_on = None


# Bucket scheme used for quali_top3 and quali_random_driver.
# Order matters — first matching bucket wins. Last bucket is the catch-all.
NEW_QUALI_BUCKETS = [
    {"max_delta": 0, "points": 5},
    {"max_delta": 1, "points": 2},
    {"max_delta": 2, "points": 1},
    {"max_delta": 5, "points": 0},
    {"max_delta": 8, "points": -2},
    {"max_delta": 999, "points": -5},
]


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. round_scoring_configs: add quali_position_buckets, drop old columns
    # -------------------------------------------------------------------------
    op.add_column(
        "round_scoring_configs",
        sa.Column(
            "quali_position_buckets",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    # Backfill existing rows with the new bucket scheme.
    op.execute(
        text(
            "UPDATE round_scoring_configs "
            "SET quali_position_buckets = CAST(:buckets AS json)"
        ).bindparams(buckets=json.dumps(NEW_QUALI_BUCKETS))
    )
    op.alter_column(
        "round_scoring_configs", "quali_position_buckets", nullable=False
    )
    op.drop_column("round_scoring_configs", "quali_top3_correct")
    op.drop_column("round_scoring_configs", "quali_top3_one_off")

    # -------------------------------------------------------------------------
    # 2. session_results: add grid_position
    # -------------------------------------------------------------------------
    op.add_column(
        "session_results",
        sa.Column("grid_position", sa.Integer(), nullable=True),
    )

    # -------------------------------------------------------------------------
    # 3. rounds: add random_quali_driver_id FK
    # -------------------------------------------------------------------------
    op.add_column(
        "rounds",
        sa.Column("random_quali_driver_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_rounds_random_quali_driver",
        "rounds",
        "round_drivers",
        ["random_quali_driver_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # -------------------------------------------------------------------------
    # 4. prediction_type enum: add new values (Postgres 12+ supports
    #    ALTER TYPE ADD VALUE inside a transaction).
    # -------------------------------------------------------------------------
    op.execute("ALTER TYPE prediction_type ADD VALUE IF NOT EXISTS 'places_gained'")
    op.execute(
        "ALTER TYPE prediction_type ADD VALUE IF NOT EXISTS 'quali_random_driver'"
    )

    # -------------------------------------------------------------------------
    # 5. predictions_places_gained
    # -------------------------------------------------------------------------
    op.create_table(
        "predictions_places_gained",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "round_id",
            sa.Integer(),
            sa.ForeignKey("rounds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "predicted_driver_id",
            sa.Integer(),
            sa.ForeignKey("drivers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "user_id", "round_id", name="uq_places_gained_user_round"
        ),
    )
    op.create_index(
        "ix_predictions_places_gained_user_id",
        "predictions_places_gained",
        ["user_id"],
    )
    op.create_index(
        "ix_predictions_places_gained_round_id",
        "predictions_places_gained",
        ["round_id"],
    )

    # -------------------------------------------------------------------------
    # 6. predictions_quali_random_driver
    # -------------------------------------------------------------------------
    op.create_table(
        "predictions_quali_random_driver",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "round_id",
            sa.Integer(),
            sa.ForeignKey("rounds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("predicted_position", sa.Integer(), nullable=False),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "user_id", "round_id", name="uq_quali_random_user_round"
        ),
    )
    op.create_index(
        "ix_predictions_quali_random_driver_user_id",
        "predictions_quali_random_driver",
        ["user_id"],
    )
    op.create_index(
        "ix_predictions_quali_random_driver_round_id",
        "predictions_quali_random_driver",
        ["round_id"],
    )

    # -------------------------------------------------------------------------
    # 7. Backfill: assign a random quali driver to every existing round
    #    that has a populated lineup. Idempotent — only sets where NULL.
    # -------------------------------------------------------------------------
    bind = op.get_bind()
    rounds_with_drivers = bind.execute(
        text(
            "SELECT DISTINCT r.id "
            "FROM rounds r "
            "JOIN round_drivers rd ON rd.round_id = r.id "
            "WHERE r.random_quali_driver_id IS NULL"
        )
    ).fetchall()
    for (round_id,) in rounds_with_drivers:
        rd_rows = bind.execute(
            text("SELECT id FROM round_drivers WHERE round_id = :rid"),
            {"rid": round_id},
        ).fetchall()
        if not rd_rows:
            continue
        chosen = random_mod.choice([r[0] for r in rd_rows])
        bind.execute(
            text(
                "UPDATE rounds SET random_quali_driver_id = :rdid "
                "WHERE id = :rid"
            ),
            {"rdid": chosen, "rid": round_id},
        )


def downgrade() -> None:
    op.drop_index(
        "ix_predictions_quali_random_driver_round_id",
        table_name="predictions_quali_random_driver",
    )
    op.drop_index(
        "ix_predictions_quali_random_driver_user_id",
        table_name="predictions_quali_random_driver",
    )
    op.drop_table("predictions_quali_random_driver")

    op.drop_index(
        "ix_predictions_places_gained_round_id",
        table_name="predictions_places_gained",
    )
    op.drop_index(
        "ix_predictions_places_gained_user_id",
        table_name="predictions_places_gained",
    )
    op.drop_table("predictions_places_gained")

    op.drop_constraint(
        "fk_rounds_random_quali_driver", "rounds", type_="foreignkey"
    )
    op.drop_column("rounds", "random_quali_driver_id")

    op.drop_column("session_results", "grid_position")

    op.add_column(
        "round_scoring_configs",
        sa.Column(
            "quali_top3_one_off",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "round_scoring_configs",
        sa.Column(
            "quali_top3_correct",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.alter_column(
        "round_scoring_configs", "quali_top3_correct", server_default=None
    )
    op.alter_column(
        "round_scoring_configs", "quali_top3_one_off", server_default=None
    )
    op.drop_column("round_scoring_configs", "quali_position_buckets")

    # NOTE: Postgres does not support removing values from an enum without
    # dropping and recreating the type. Left in place — harmless.
