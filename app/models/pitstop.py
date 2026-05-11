"""Pit-stop records, ingested from Jolpica's /pitstops endpoint.

One row per (round, driver, stop_number). Note that Jolpica's `duration`
includes pit lane transit, not the stationary stop — useful for
"most pit stops" and "lap of first pit stop" specials, but not for
fastest-stop analysis.
"""
from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db


class PitStop(db.Model):
    __tablename__ = "pit_stops"
    __table_args__ = (
        UniqueConstraint(
            "round_id", "driver_id", "stop_number",
            name="uq_pitstop_round_driver_stop",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    driver_id: Mapped[int] = mapped_column(
        ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=False,
    )
    lap: Mapped[int] = mapped_column(Integer, nullable=False)
    stop_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Optional — Jolpica sometimes omits duration. In milliseconds.
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    round = relationship("Round", back_populates="pit_stops")
    driver = relationship("Driver")
