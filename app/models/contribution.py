"""Contribution (wildcard) models.

A *contribution* is a user-authored prediction — a "wildcard" — set by a
trusted contributor for a single round. Unlike the API-driven predictions,
its outcome is entered manually by the contributor after the session.

  - ContributionDefinition: one per (contributor, round). The question, the
    input type, the allowed-option set (for restricted picks), the scoring
    configuration, and the manually-entered actual outcome.
  - ContributionPrediction: one per (user, contribution). Heterogeneous
    payload mirroring SpecialPrediction.
  - Scoring is written into PredictionScore (kind=CONTRIBUTION,
    contribution_id set) so leaderboards aggregate it with everything else.

Input types (catalogue + rules live in app/scoring/contributions.py):
  driver_pick · team_pick · integer · decimal · lap_time · bool · custom_choice
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, JSON, Numeric, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ContributionDefinition(db.Model):
    __tablename__ = "contribution_definitions"
    __table_args__ = (
        UniqueConstraint(
            "contributor_id", "round_id",
            name="uq_contribution_contributor_round",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contributor_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    round_id: Mapped[int] = mapped_column(
        ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True,
    )

    question_text: Mapped[str] = mapped_column(String(200), nullable=False)
    scoring_blurb: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    input_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # Restricted option sets — only the column matching input_type is used.
    # Null / empty list = unrestricted (all drivers / all teams).
    allowed_driver_ids: Mapped[list | None] = mapped_column(JSON)
    allowed_team_names: Mapped[list | None] = mapped_column(JSON)
    custom_options: Mapped[list | None] = mapped_column(JSON)

    # Scoring config.
    primary_points: Mapped[int] = mapped_column(Integer, nullable=False)
    primary_mode: Mapped[str] = mapped_column(String(10), nullable=False)  # exact | range
    primary_range: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    secondary_points: Mapped[int | None] = mapped_column(Integer)
    secondary_range: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))

    # Manually-entered actual outcome. The populated column matches
    # input_type; all null until the contributor submits the actual.
    actual_driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("drivers.id", ondelete="RESTRICT"),
    )
    actual_team_name: Mapped[str | None] = mapped_column(String(80))
    actual_int: Mapped[int | None] = mapped_column(Integer)
    actual_decimal: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    actual_lap_time_ms: Mapped[int | None] = mapped_column(Integer)
    actual_bool: Mapped[bool | None] = mapped_column(Boolean)
    actual_choice: Mapped[str | None] = mapped_column(String(80))
    actual_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_set_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    contributor = relationship("User", foreign_keys=[contributor_id])
    actual_set_by = relationship("User", foreign_keys=[actual_set_by_id])
    round = relationship("Round")
    actual_driver = relationship("Driver", foreign_keys=[actual_driver_id])
    predictions = relationship(
        "ContributionPrediction",
        back_populates="definition",
        cascade="all, delete-orphan",
    )

    @property
    def has_actual(self) -> bool:
        return self.actual_set_at is not None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ContributionDefinition r{self.round_id} by u{self.contributor_id} {self.input_type}>"


class ContributionPrediction(db.Model):
    __tablename__ = "contribution_predictions"
    __table_args__ = (
        UniqueConstraint(
            "contribution_id", "user_id",
            name="uq_contribution_prediction_user",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contribution_id: Mapped[int] = mapped_column(
        ForeignKey("contribution_definitions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )

    predicted_driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("drivers.id", ondelete="RESTRICT"),
    )
    predicted_team_name: Mapped[str | None] = mapped_column(String(80))
    predicted_int: Mapped[int | None] = mapped_column(Integer)
    predicted_decimal: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    predicted_lap_time_ms: Mapped[int | None] = mapped_column(Integer)
    predicted_bool: Mapped[bool | None] = mapped_column(Boolean)
    predicted_choice: Mapped[str | None] = mapped_column(String(80))

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )

    definition = relationship("ContributionDefinition", back_populates="predictions")
    user = relationship("User")
    predicted_driver = relationship("Driver")
