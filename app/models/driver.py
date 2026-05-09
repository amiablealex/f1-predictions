"""Driver and per-round seat models.

The two tables work together to support the "score by seat" rule for driver
substitutions. The model is:

  * `drivers` is the master list (synced from Jolpica). Each row represents
    a real human driver with a permanent identifier.
  * `round_drivers` records, for a given round, who is the *expected*
    occupant of each car number. This is what populates the user's
    prediction dropdown.
  * When scoring, we read the actual session result (which has a
    `car_number`), look up which user-predicted driver was tied to that
    car number for that round, and award points based on the actual
    finishing position of that car number.

This means a user picks "Hamilton (Ferrari, #44)" but is really betting on
"whoever drives car 44 in this round" — exactly the rule we agreed.
"""
from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db


class Driver(db.Model):
    __tablename__ = "drivers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Jolpica's stable identifier, e.g. "hamilton", "max_verstappen"
    driver_ref: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    given_name: Mapped[str] = mapped_column(String(80), nullable=False)
    family_name: Mapped[str] = mapped_column(String(80), nullable=False)
    code: Mapped[str | None] = mapped_column(String(5))  # e.g. "HAM", "VER"
    permanent_number: Mapped[int | None] = mapped_column(Integer)
    nationality: Mapped[str | None] = mapped_column(String(50))

    @property
    def full_name(self) -> str:
        return f"{self.given_name} {self.family_name}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Driver {self.code or self.driver_ref}>"


class RoundDriver(db.Model):
    """Maps car numbers to expected drivers for a specific round.

    Predictions reference `expected_driver_id` (the regular driver). Scoring
    matches by `car_number` to whatever the API reports as the actual
    occupant of that car in the session.
    """

    __tablename__ = "round_drivers"
    __table_args__ = (
        UniqueConstraint("round_id", "car_number", name="uq_round_car"),
        UniqueConstraint("round_id", "expected_driver_id", name="uq_round_driver"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    car_number: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=False)
    constructor_name: Mapped[str | None] = mapped_column(String(80))

    round = relationship(
        "Round",
        back_populates="round_drivers",
        foreign_keys=[round_id],
    )
    expected_driver = relationship("Driver")
