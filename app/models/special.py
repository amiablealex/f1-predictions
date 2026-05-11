"""SpecialOutcome — cached actual outcomes for the round's two specials.

Computed once during race-phase ingest from race results + pit-stop data.
Lets the round-detail page render historical specials quickly without
recomputing margin-of-victory or longest-stint on every page view.

Like SpecialPrediction, the row is heterogeneous: which columns are
populated depends on `special_key`. If a special has no result (e.g. no
DNFs for "first retirement") the row exists with `no_result=True` so
the round view can show a tidy "no result this round" line.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SpecialOutcome(db.Model):
    __tablename__ = "special_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "round_id", "special_key", name="uq_special_outcome_round_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    special_key: Mapped[str] = mapped_column(String(40), nullable=False)

    # Heterogeneous payload mirroring SpecialPrediction's shape.
    actual_driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("drivers.id", ondelete="RESTRICT"),
    )
    actual_int: Mapped[int | None] = mapped_column(Integer)
    actual_bool: Mapped[bool | None] = mapped_column(Boolean)
    actual_team_name: Mapped[str | None] = mapped_column(String(80))

    # Some specials may legitimately have no result (e.g. no DNFs for
    # "first retirement"). When True, scoring is skipped for everyone.
    no_result: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )

    round = relationship("Round", back_populates="special_outcomes")
    actual_driver = relationship("Driver")
