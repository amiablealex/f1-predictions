"""contributions wildcards

Revision ID: 19a8e1056539
Revises: 20260517_enumfix
Create Date: 2026-06-08 12:54:19.977785

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '19a8e1056539'
down_revision = '20260517_enumfix'
branch_labels = None
depends_on = None


def upgrade():
    # 1. New user flag.
    op.add_column(
        "users",
        sa.Column("is_contributor", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("users", "is_contributor", server_default=None)

    # 2. contribution_definitions
    op.create_table(
        "contribution_definitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contributor_id", sa.Integer(), nullable=False),
        sa.Column("round_id", sa.Integer(), nullable=False),
        sa.Column("question_text", sa.String(length=200), nullable=False),
        sa.Column("scoring_blurb", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("input_type", sa.String(length=20), nullable=False),
        sa.Column("allowed_driver_ids", sa.JSON(), nullable=True),
        sa.Column("allowed_team_names", sa.JSON(), nullable=True),
        sa.Column("custom_options", sa.JSON(), nullable=True),
        sa.Column("primary_points", sa.Integer(), nullable=False),
        sa.Column("primary_mode", sa.String(length=10), nullable=False),
        sa.Column("primary_range", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column("secondary_points", sa.Integer(), nullable=True),
        sa.Column("secondary_range", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column("actual_driver_id", sa.Integer(), nullable=True),
        sa.Column("actual_team_name", sa.String(length=80), nullable=True),
        sa.Column("actual_int", sa.Integer(), nullable=True),
        sa.Column("actual_decimal", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column("actual_lap_time_ms", sa.Integer(), nullable=True),
        sa.Column("actual_bool", sa.Boolean(), nullable=True),
        sa.Column("actual_choice", sa.String(length=80), nullable=True),
        sa.Column("actual_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_set_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contributor_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["round_id"], ["rounds.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actual_driver_id"], ["drivers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actual_set_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("contributor_id", "round_id", name="uq_contribution_contributor_round"),
    )
    op.create_index("ix_contribution_definitions_contributor_id", "contribution_definitions", ["contributor_id"])
    op.create_index("ix_contribution_definitions_round_id", "contribution_definitions", ["round_id"])

    # 3. contribution_predictions
    op.create_table(
        "contribution_predictions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("contribution_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("predicted_driver_id", sa.Integer(), nullable=True),
        sa.Column("predicted_team_name", sa.String(length=80), nullable=True),
        sa.Column("predicted_int", sa.Integer(), nullable=True),
        sa.Column("predicted_decimal", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column("predicted_lap_time_ms", sa.Integer(), nullable=True),
        sa.Column("predicted_bool", sa.Boolean(), nullable=True),
        sa.Column("predicted_choice", sa.String(length=80), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contribution_id"], ["contribution_definitions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["predicted_driver_id"], ["drivers.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("contribution_id", "user_id", name="uq_contribution_prediction_user"),
    )
    op.create_index("ix_contribution_predictions_contribution_id", "contribution_predictions", ["contribution_id"])
    op.create_index("ix_contribution_predictions_user_id", "contribution_predictions", ["user_id"])

    # 4. prediction_scores: new column, FK, constraint swap, enum value.
    op.add_column("prediction_scores", sa.Column("contribution_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_prediction_scores_contribution_id",
        "prediction_scores", "contribution_definitions",
        ["contribution_id"], ["id"], ondelete="CASCADE",
    )
    op.drop_constraint("uq_score_user_round_kind_position_special", "prediction_scores", type_="unique")
    op.create_unique_constraint(
        "uq_score_user_round_kind_position_special_contribution",
        "prediction_scores",
        ["user_id", "round_id", "kind", "position", "special_key", "contribution_id"],
    )
    # New enum label — the NAME (uppercase), matching SQLAlchemy's storage of
    # Enum members by .name (RACE_TOP10, SPECIAL, ...). autocommit_block
    # escapes Alembic's transaction, which ADD VALUE requires.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE prediction_type ADD VALUE IF NOT EXISTS 'CONTRIBUTION'")


def downgrade():
    op.drop_constraint(
        "uq_score_user_round_kind_position_special_contribution",
        "prediction_scores", type_="unique",
    )
    op.create_unique_constraint(
        "uq_score_user_round_kind_position_special",
        "prediction_scores",
        ["user_id", "round_id", "kind", "position", "special_key"],
    )
    op.drop_constraint("fk_prediction_scores_contribution_id", "prediction_scores", type_="foreignkey")
    op.drop_column("prediction_scores", "contribution_id")
    op.drop_index("ix_contribution_predictions_user_id", table_name="contribution_predictions")
    op.drop_index("ix_contribution_predictions_contribution_id", table_name="contribution_predictions")
    op.drop_table("contribution_predictions")
    op.drop_index("ix_contribution_definitions_round_id", table_name="contribution_definitions")
    op.drop_index("ix_contribution_definitions_contributor_id", table_name="contribution_definitions")
    op.drop_table("contribution_definitions")
    op.drop_column("users", "is_contributor")
    # Postgres can't drop a single enum label; leaving 'CONTRIBUTION' on the
    # type is harmless and won't block a re-upgrade (ADD VALUE IF NOT EXISTS).
