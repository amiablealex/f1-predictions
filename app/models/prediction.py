"""Prediction models.

Predictions are stored as one row per (user, round, prediction-type) — and
for the position-based predictions, one row per position. This is more
verbose than a single JSON-blob table but lets the scoring engine and the
"friends' predictions" view query them with ordinary SQL.

The `PredictionScore` table holds the *calculated* points for each
prediction, written by the scoring engine after a session's reveal phase
completes. Leaderboards read from this table for fast aggregation.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PredictionType(str, enum.Enum):
    """Discrete prediction kinds, used by PredictionScore.kind."""
    RACE_TOP10 = "race_top10"
    QUALI_TOP3 = "quali_top3"
    SPRINT_TOP3 = "sprint_top3"
    POLE_TIME = "pole_time"
    FASTEST_LAP = "fastest_lap"
    DNF_COUNT = "dnf_count"
    PLACES_GAINED = "places_gained"
    QUALI_RANDOM_DRIVER = "quali_random_driver"
    QUALI_HEAD_TO_HEAD = "quali_head_to_head"
    QUALI_NTH = "quali_nth"
    SPECIAL = "special"


# -----------------------------------------------------------------------------
# Position-based predictions (top 10 race, top 3 quali, top 3 sprint)
# -----------------------------------------------------------------------------


class Top10Prediction(db.Model):
    __tablename__ = "predictions_race_top10"
    __table_args__ = (
        UniqueConstraint("user_id", "round_id", "position", name="uq_top10_user_round_pos"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..10
    predicted_driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user = relationship("User")
    round = relationship("Round")
    predicted_driver = relationship("Driver")


class Top3QualiPrediction(db.Model):
    __tablename__ = "predictions_quali_top3"
    __table_args__ = (
        UniqueConstraint("user_id", "round_id", "position", name="uq_qtop3_user_round_pos"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..3
    predicted_driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user = relationship("User")
    round = relationship("Round")
    predicted_driver = relationship("Driver")


class Top3SprintPrediction(db.Model):
    __tablename__ = "predictions_sprint_top3"
    __table_args__ = (
        UniqueConstraint("user_id", "round_id", "position", name="uq_stop3_user_round_pos"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # 1..3
    predicted_driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user = relationship("User")
    round = relationship("Round")
    predicted_driver = relationship("Driver")


# -----------------------------------------------------------------------------
# Single-value predictions (one row per user per round)
# -----------------------------------------------------------------------------


class PoleTimePrediction(db.Model):
    """User's predicted pole time, stored as a millisecond integer.

    Stored as ms (int) rather than seconds (float) to avoid floating-point
    drift — pole times are inherently three-decimal-place values.
    """

    __tablename__ = "predictions_pole_time"
    __table_args__ = (
        UniqueConstraint("user_id", "round_id", name="uq_poletime_user_round"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    predicted_time_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user = relationship("User")
    round = relationship("Round")


class FastestLapPrediction(db.Model):
    __tablename__ = "predictions_fastest_lap"
    __table_args__ = (
        UniqueConstraint("user_id", "round_id", name="uq_flap_user_round"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    predicted_driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user = relationship("User")
    round = relationship("Round")
    predicted_driver = relationship("Driver")


class DnfCountPrediction(db.Model):
    __tablename__ = "predictions_dnf_count"
    __table_args__ = (
        UniqueConstraint("user_id", "round_id", name="uq_dnf_user_round"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    predicted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user = relationship("User")
    round = relationship("Round")


class PlacesGainedPrediction(db.Model):
    """User's pick for the 'places gained' wager.

    Score = grid_position - finish_position. No cap, no floor. DNF treated
    as last_classified+1; DNS scores 0; pit lane (grid 0) treated as the
    last grid slot for scoring purposes.
    """

    __tablename__ = "predictions_places_gained"
    __table_args__ = (
        UniqueConstraint("user_id", "round_id", name="uq_places_gained_user_round"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    predicted_driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user = relationship("User")
    round = relationship("Round")
    predicted_driver = relationship("Driver")


class QualiRandomDriverPrediction(db.Model):
    """User's predicted qualifying position for the round's randomly-
    selected driver (Round.random_quali_driver). Same bucket scoring as
    the quali top 3 slots."""

    __tablename__ = "predictions_quali_random_driver"
    __table_args__ = (
        UniqueConstraint("user_id", "round_id", name="uq_quali_random_user_round"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    predicted_position: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user = relationship("User")
    round = relationship("Round")


class QualiHeadToHeadPrediction(db.Model):
    """User's pick for the round's head-to-head: which of the two
    Round.qh2h_driver_{a,b} they think qualifies higher.

    `predicted_driver_id` is a master Driver — same substitution-by-seat
    semantics as every other driver pick.
    """

    __tablename__ = "predictions_quali_head_to_head"
    __table_args__ = (
        UniqueConstraint("user_id", "round_id", name="uq_qh2h_user_round"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    predicted_driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user = relationship("User")
    round = relationship("Round")
    predicted_driver = relationship("Driver")


class QualiNthPrediction(db.Model):
    """User's pick for who will qualify in position Round.quali_nth_position.

    Scoring uses the standard quali bucket scheme on |actual - N|.
    """

    __tablename__ = "predictions_quali_nth"
    __table_args__ = (
        UniqueConstraint("user_id", "round_id", name="uq_qnth_user_round"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    predicted_driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user = relationship("User")
    round = relationship("Round")
    predicted_driver = relationship("Driver")


class SpecialPrediction(db.Model):
    """One row per (user, round, special_key).

    The special bank has heterogeneous payloads — some specials need a
    driver pick, some an integer, some a yes/no, some a team name. Rather
    than maintaining eight bespoke tables, this single table carries
    nullable columns for each shape; the scoring engine reads the column
    matching the special's type.
    """

    __tablename__ = "predictions_special"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "round_id", "special_key",
            name="uq_special_user_round_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    special_key: Mapped[str] = mapped_column(String(40), nullable=False)

    # Heterogeneous payload — populated columns depend on special_key.
    predicted_driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("drivers.id", ondelete="RESTRICT"),
    )
    predicted_int: Mapped[int | None] = mapped_column(Integer)
    predicted_bool: Mapped[bool | None] = mapped_column(Boolean)
    predicted_team_name: Mapped[str | None] = mapped_column(String(80))

    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user = relationship("User")
    round = relationship("Round")
    predicted_driver = relationship("Driver")


# -----------------------------------------------------------------------------
# Calculated scores (written by the scoring engine; read by leaderboards)
# -----------------------------------------------------------------------------


class PredictionScore(db.Model):
    """One row per (user, round, prediction-type, position-or-null).

    For `RACE_TOP10`, `QUALI_TOP3`, `SPRINT_TOP3` the `position` column is
    populated (the slot the points were awarded for). For all others
    `position` is NULL and there's a single row per user per round.
    """

    __tablename__ = "prediction_scores"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "round_id", "kind", "position", "special_key",
            name="uq_score_user_round_kind_position_special",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[PredictionType] = mapped_column(
        Enum(PredictionType, name="prediction_type"),
        nullable=False,
    )

    # NULL for non-position-based predictions.
    position: Mapped[int | None] = mapped_column(Integer)
    # For SPECIAL kind: which special this score row is for (matches
    # Round.special_a_key / special_b_key). NULL for all other kinds.
    special_key: Mapped[str | None] = mapped_column(String(40))
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    user = relationship("User")
    round = relationship("Round")
