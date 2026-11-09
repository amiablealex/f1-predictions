"""Driver and per-round seat models.

The two tables work together to support the "score by seat" rule for driver
substitutions. The model is:

  * `drivers` is the master list (synced from Jolpica). Each row represents
    a real human driver with a permanent identifier.
  * `round_drivers` records, for a given round, who is the *expected*
    occupant of each car number. This is what populates the user's
    prediction dropdown.
  * When scoring, each seat is matched to the result of whoever drove it,
    by team (app/seats.py). Car numbers belong to drivers in F1 — a
    reserve races under his own number — so they can't identify a seat.

A user picks "Hamilton (Ferrari)" but is really betting on "whoever drives
Hamilton's Ferrari seat this round".

`round_drivers` is the lineup as offered on the form. The worker never
rewrites it from results; the admin lineup editor (app/lineups.py) is the
only thing that changes a seat's driver once the round exists.
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
    """One seat in a round's lineup: team + the driver expected in it.

    Predictions reference `expected_driver_id`. Scoring resolves the seat
    to whoever actually drove it (app/seats.py). `car_number` is the
    listed driver's number, kept as a fallback match key for result rows
    that predate `SessionResult.constructor_name`.
    """

    __tablename__ = "round_drivers"
    __table_args__ = (
        UniqueConstraint("round_id", "car_number", name="uq_round_car"),
        # Deferred so the lineup editor can move drivers between seats
        # (A→B, B→C) in one transaction.
        UniqueConstraint(
            "round_id", "expected_driver_id", name="uq_round_driver",
            deferrable=True, initially="DEFERRED",
        ),
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
