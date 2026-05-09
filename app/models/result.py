"""Per-session per-car results, populated by the worker after a session ends."""
from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db


class SessionResult(db.Model):
    """One row per finishing position in a session.

    For a race, position 1..N covers all classified entries plus DNFs (the
    API returns DNFs ordered after classified finishers). For qualifying,
    position 1..20 reflects the Q1/Q2/Q3 outcome.

    `actual_driver_id` records who actually drove the car (which may differ
    from the round's expected driver in the case of substitutions). The
    scoring engine uses `car_number` to bridge predictions → actual results.
    """

    __tablename__ = "session_results"
    __table_args__ = (
        UniqueConstraint("session_id", "position", name="uq_session_result_position"),
        UniqueConstraint("session_id", "car_number", name="uq_session_result_car"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    car_number: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id", ondelete="RESTRICT"), nullable=False)

    # Status text from the API ("Finished", "+1 Lap", "Engine", "Accident",
    # "Did not start", etc.). The scoring engine treats anything not in
    # {Finished, +N Lap(s)} as a DNF for DNF-count purposes.
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    # Pre-computed flag, set when results are ingested.
    is_classified: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # For races: True for the one row representing the fastest-lap setter.
    is_fastest_lap: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # For qualifying: the best of Q1/Q2/Q3 in milliseconds. Null otherwise.
    best_qualifying_time_ms: Mapped[int | None] = mapped_column(Integer)

    # For races (and sprint races): the starting grid position. Pit lane
    # starts come through as 0; nullable for old rows ingested before this
    # column existed (and for qualifying-only sessions).
    grid_position: Mapped[int | None] = mapped_column(Integer)

    session = relationship("Session", back_populates="results")
    actual_driver = relationship("Driver")
