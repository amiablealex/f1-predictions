"""Phase 4: quali head-to-head, qualify-Nth, specials rotation, pit stops.

Schema changes:
  - rounds: add qh2h_driver_a_id, qh2h_driver_b_id (FK round_drivers, SET NULL),
    quali_nth_position (int), special_a_key, special_b_key (varchar 40).
  - round_scoring_configs: 9 new integer columns (h2h + 8 specials),
    backfilled to defaults then made NOT NULL.
  - prediction_scores: add special_key (varchar 40); replace unique
    constraint to include it.
  - prediction_type enum: add 'quali_head_to_head', 'quali_nth', 'special'.
  - new tables: pit_stops, special_outcomes,
    predictions_quali_head_to_head, predictions_quali_nth,
    predictions_special.

Revision ID: 20260514_phase4
Revises: 20260513_phase3
Create Date: 2026-05-14
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import text


revision = "20260514_phase4"
down_revision = "20260513_phase3"
branch_labels = None
depends_on = None


# Defaults for the 9 new scoring-config columns. Used to backfill existing
# round_scoring_configs rows so the NOT NULL alters stick.
SCORING_DEFAULTS_NEW = {
    "qh2h_correct": 5,
    "special_first_retirement": 10,
    "special_most_pitstops": 10,
    "special_last_classified": 10,
    "special_margin_of_victory": 10,
    "special_lap_of_first_pitstop": 10,
    "special_pole_sitter_wins": 10,
    "special_longest_stint": 10,
    "special_biggest_team_gap": 10,
}


def upgrade() -> None:
    # -------------------------------------------------------------------------
    # 1. rounds: 5 new columns + 2 FKs to round_drivers
    # -------------------------------------------------------------------------
    op.add_column("rounds", sa.Column("qh2h_driver_a_id", sa.Integer(), nullable=True))
    op.add_column("rounds", sa.Column("qh2h_driver_b_id", sa.Integer(), nullable=True))
    op.add_column("rounds", sa.Column("quali_nth_position", sa.Integer(), nullable=True))
    op.add_column("rounds", sa.Column("special_a_key", sa.String(length=40), nullable=True))
    op.add_column("rounds", sa.Column("special_b_key", sa.String(length=40), nullable=True))
    op.create_foreign_key(
        "fk_rounds_qh2h_driver_a", "rounds", "round_drivers",
        ["qh2h_driver_a_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_rounds_qh2h_driver_b", "rounds", "round_drivers",
        ["qh2h_driver_b_id"], ["id"], ondelete="SET NULL",
    )

    # -------------------------------------------------------------------------
    # 2. round_scoring_configs: 9 new columns, backfilled, then NOT NULL
    # -------------------------------------------------------------------------
    for col_name in SCORING_DEFAULTS_NEW:
        op.add_column(
            "round_scoring_configs",
            sa.Column(col_name, sa.Integer(), nullable=True),
        )
    for col_name, default_val in SCORING_DEFAULTS_NEW.items():
        op.execute(
            text(f"UPDATE round_scoring_configs SET {col_name} = :v")
            .bindparams(v=default_val)
        )
    for col_name in SCORING_DEFAULTS_NEW:
        op.alter_column("round_scoring_configs", col_name, nullable=False)

    # -------------------------------------------------------------------------
    # 3. prediction_type enum: add three new values
    # -------------------------------------------------------------------------
    op.execute("ALTER TYPE prediction_type ADD VALUE IF NOT EXISTS 'quali_head_to_head'")
    op.execute("ALTER TYPE prediction_type ADD VALUE IF NOT EXISTS 'quali_nth'")
    op.execute("ALTER TYPE prediction_type ADD VALUE IF NOT EXISTS 'special'")

    # -------------------------------------------------------------------------
    # 4. prediction_scores: add special_key; replace unique constraint
    # -------------------------------------------------------------------------
    op.add_column(
        "prediction_scores",
        sa.Column("special_key", sa.String(length=40), nullable=True),
    )
    op.drop_constraint(
        "uq_score_user_round_kind_position", "prediction_scores", type_="unique",
    )
    op.create_unique_constraint(
        "uq_score_user_round_kind_position_special",
        "prediction_scores",
        ["user_id", "round_id", "kind", "position", "special_key"],
    )

    # -------------------------------------------------------------------------
    # 5. pit_stops
    # -------------------------------------------------------------------------
    op.create_table(
        "pit_stops",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "round_id", sa.Integer(),
            sa.ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "driver_id", sa.Integer(),
            sa.ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("lap", sa.Integer(), nullable=False),
        sa.Column("stop_number", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.UniqueConstraint(
            "round_id", "driver_id", "stop_number",
            name="uq_pitstop_round_driver_stop",
        ),
    )
    op.create_index("ix_pit_stops_round_id", "pit_stops", ["round_id"])

    # -------------------------------------------------------------------------
    # 6. special_outcomes — cached actual results for the two round specials
    # -------------------------------------------------------------------------
    op.create_table(
        "special_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "round_id", sa.Integer(),
            sa.ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("special_key", sa.String(length=40), nullable=False),
        sa.Column(
            "actual_driver_id", sa.Integer(),
            sa.ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=True,
        ),
        sa.Column("actual_int", sa.Integer(), nullable=True),
        sa.Column("actual_bool", sa.Boolean(), nullable=True),
        sa.Column("actual_team_name", sa.String(length=80), nullable=True),
        sa.Column(
            "no_result", sa.Boolean(),
            nullable=False, server_default=sa.false(),
        ),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "round_id", "special_key", name="uq_special_outcome_round_key",
        ),
    )
    op.create_index(
        "ix_special_outcomes_round_id", "special_outcomes", ["round_id"],
    )

    # -------------------------------------------------------------------------
    # 7. predictions_quali_head_to_head
    # -------------------------------------------------------------------------
    op.create_table(
        "predictions_quali_head_to_head",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "round_id", sa.Integer(),
            sa.ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "predicted_driver_id", sa.Integer(),
            sa.ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "submitted_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "round_id", name="uq_qh2h_user_round"),
    )
    op.create_index(
        "ix_predictions_quali_h2h_user_id",
        "predictions_quali_head_to_head", ["user_id"],
    )
    op.create_index(
        "ix_predictions_quali_h2h_round_id",
        "predictions_quali_head_to_head", ["round_id"],
    )

    # -------------------------------------------------------------------------
    # 8. predictions_quali_nth
    # -------------------------------------------------------------------------
    op.create_table(
        "predictions_quali_nth",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "round_id", sa.Integer(),
            sa.ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "predicted_driver_id", sa.Integer(),
            sa.ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column(
            "submitted_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "round_id", name="uq_qnth_user_round"),
    )
    op.create_index(
        "ix_predictions_quali_nth_user_id",
        "predictions_quali_nth", ["user_id"],
    )
    op.create_index(
        "ix_predictions_quali_nth_round_id",
        "predictions_quali_nth", ["round_id"],
    )

    # -------------------------------------------------------------------------
    # 9. predictions_special — heterogeneous payload, special_key disambiguates
    # -------------------------------------------------------------------------
    op.create_table(
        "predictions_special",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "round_id", sa.Integer(),
            sa.ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("special_key", sa.String(length=40), nullable=False),
        sa.Column(
            "predicted_driver_id", sa.Integer(),
            sa.ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=True,
        ),
        sa.Column("predicted_int", sa.Integer(), nullable=True),
        sa.Column("predicted_bool", sa.Boolean(), nullable=True),
        sa.Column("predicted_team_name", sa.String(length=80), nullable=True),
        sa.Column(
            "submitted_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "user_id", "round_id", "special_key",
            name="uq_special_user_round_key",
        ),
    )
    op.create_index(
        "ix_predictions_special_user_id", "predictions_special", ["user_id"],
    )
    op.create_index(
        "ix_predictions_special_round_id", "predictions_special", ["round_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_predictions_special_round_id", table_name="predictions_special",
    )
    op.drop_index(
        "ix_predictions_special_user_id", table_name="predictions_special",
    )
    op.drop_table("predictions_special")

    op.drop_index(
        "ix_predictions_quali_nth_round_id", table_name="predictions_quali_nth",
    )
    op.drop_index(
        "ix_predictions_quali_nth_user_id", table_name="predictions_quali_nth",
    )
    op.drop_table("predictions_quali_nth")

    op.drop_index(
        "ix_predictions_quali_h2h_round_id",
        table_name="predictions_quali_head_to_head",
    )
    op.drop_index(
        "ix_predictions_quali_h2h_user_id",
        table_name="predictions_quali_head_to_head",
    )
    op.drop_table("predictions_quali_head_to_head")

    op.drop_index("ix_special_outcomes_round_id", table_name="special_outcomes")
    op.drop_table("special_outcomes")

    op.drop_index("ix_pit_stops_round_id", table_name="pit_stops")
    op.drop_table("pit_stops")

    op.drop_constraint(
        "uq_score_user_round_kind_position_special",
        "prediction_scores", type_="unique",
    )
    op.create_unique_constraint(
        "uq_score_user_round_kind_position",
        "prediction_scores",
        ["user_id", "round_id", "kind", "position"],
    )
    op.drop_column("prediction_scores", "special_key")

    # Enum values left in place — Postgres can't remove enum values cleanly.

    for col_name in reversed(list(SCORING_DEFAULTS_NEW)):
        op.drop_column("round_scoring_configs", col_name)

    op.drop_constraint("fk_rounds_qh2h_driver_b", "rounds", type_="foreignkey")
    op.drop_constraint("fk_rounds_qh2h_driver_a", "rounds", type_="foreignkey")
    op.drop_column("rounds", "special_b_key")
    op.drop_column("rounds", "special_a_key")
    op.drop_column("rounds", "quali_nth_position")
    op.drop_column("rounds", "qh2h_driver_b_id")
    op.drop_column("rounds", "qh2h_driver_a_id")
