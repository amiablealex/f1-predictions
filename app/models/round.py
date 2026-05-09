"""Round, Session, and per-round scoring config models.

A `Round` corresponds to a Grand Prix weekend. A `Session` is one of the
scoring events within that weekend (sprint quali, sprint race, qualifying,
race). `RoundScoringConfig` is a per-round snapshot of the points values
that were active when the round was created — this is what makes past
leaderboards immutable when config changes mid-season.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WeekendType(str, enum.Enum):
    STANDARD = "standard"
    SPRINT = "sprint"


class RoundState(str, enum.Enum):
    UPCOMING = "upcoming"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class SessionType(str, enum.Enum):
    SPRINT_QUALI = "sprint_quali"
    SPRINT_RACE = "sprint_race"
    QUALIFYING = "qualifying"
    RACE = "race"


class SessionStatus(str, enum.Enum):
    UPCOMING = "upcoming"
    IN_PROGRESS = "in_progress"
    PENDING_RESULTS = "pending_results"
    COMPLETED = "completed"


class ScoringPhase(str, enum.Enum):
    """Marks which reveal-phase a prediction belongs to.

    Used by the scoring engine and the worker so leaderboards update in the
    correct waves agreed in scoping:
      - SPRINT  → after sprint race  (reveals SQ pole + SR top 3)
      - QUALI   → after main qualifying (reveals quali top 3 + pole time
                  + random driver pick)
      - RACE    → after main race (reveals race top 10 + fastest lap +
                  DNF count + places gained)
    """
    SPRINT = "sprint"
    QUALI = "quali"
    RACE = "race"


class Round(db.Model):
    __tablename__ = "rounds"
    __table_args__ = (
        UniqueConstraint("season", "round_number", name="uq_season_round"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Display fields, populated from Jolpica
    gp_name: Mapped[str] = mapped_column(String(120), nullable=False)
    country: Mapped[str | None] = mapped_column(String(80))
    country_code: Mapped[str | None] = mapped_column(String(2))   # ISO-2 for flag emoji
    circuit_name: Mapped[str | None] = mapped_column(String(120))
    circuit_ref: Mapped[str | None] = mapped_column(String(50))   # Jolpica's circuitId

    weekend_type: Mapped[WeekendType] = mapped_column(
        Enum(WeekendType, name="weekend_type"),
        default=WeekendType.STANDARD,
        nullable=False,
    )
    state: Mapped[RoundState] = mapped_column(
        Enum(RoundState, name="round_state"),
        default=RoundState.UPCOMING,
        nullable=False,
        index=True,
    )

    # Deadline for predictions = (first scoring session start) - DEADLINE_OFFSET_MINUTES.
    # Cached on the round for fast queries.
    predictions_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    predictions_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # The "where will <driver> qualify?" pick. Set once by the worker when
    # the round's lineup is first populated; never overwritten. Same driver
    # is used for every user in the league.
    random_quali_driver_id: Mapped[int | None] = mapped_column(
        ForeignKey("round_drivers.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    # Relationships
    sessions = relationship(
        "Session",
        back_populates="round",
        cascade="all, delete-orphan",
        order_by="Session.scheduled_start",
    )
    round_drivers = relationship(
        "RoundDriver",
        back_populates="round",
        cascade="all, delete-orphan",
        foreign_keys="RoundDriver.round_id",
    )
    scoring_config = relationship(
        "RoundScoringConfig",
        back_populates="round",
        uselist=False,
        cascade="all, delete-orphan",
    )
    # The randomly-picked driver for the per-round quali wager. Disambiguated
    # via foreign_keys because there are two FKs between Round and RoundDriver
    # (the back-populated round_drivers via RoundDriver.round_id, plus this
    # new one via Round.random_quali_driver_id). post_update breaks the
    # circular dependency at write time.
    random_quali_driver = relationship(
        "RoundDriver",
        foreign_keys=[random_quali_driver_id],
        post_update=True,
    )

    @property
    def has_sprint(self) -> bool:
        return self.weekend_type == WeekendType.SPRINT

    @property
    def display_label(self) -> str:
        return f"Round {self.round_number} — {self.gp_name}"

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Round {self.season}/{self.round_number} {self.gp_name}>"


class Session(db.Model):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("round_id", "session_type", name="uq_round_session_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), nullable=False, index=True)
    session_type: Mapped[SessionType] = mapped_column(
        Enum(SessionType, name="session_type"),
        nullable=False,
    )
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    scheduled_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status"),
        default=SessionStatus.UPCOMING,
        nullable=False,
        index=True,
    )

    # When we successfully fetched final results from the API.
    results_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the scoring engine ran for this session's phase.
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # For qualifying sessions: pole time stored in milliseconds, computed
    # from the fastest of the actual driver's Q1/Q2/Q3 laps.
    pole_time_ms: Mapped[int | None] = mapped_column(Integer)
    # For the main race: the driver_id (master Driver.id) who set the fastest lap.
    fastest_lap_driver_id: Mapped[int | None] = mapped_column(ForeignKey("drivers.id"))
    # For the main race: count of cars that did not finish.
    dnf_count: Mapped[int | None] = mapped_column(Integer)

    round = relationship("Round", back_populates="sessions")
    results = relationship(
        "SessionResult",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="SessionResult.position",
    )
    fastest_lap_driver = relationship("Driver")

    @property
    def scoring_phase(self) -> ScoringPhase:
        """Map a session to the reveal phase it triggers."""
        if self.session_type == SessionType.SPRINT_RACE:
            return ScoringPhase.SPRINT
        if self.session_type == SessionType.QUALIFYING:
            return ScoringPhase.QUALI
        if self.session_type == SessionType.RACE:
            return ScoringPhase.RACE
        # SPRINT_QUALI does not trigger a reveal on its own; its predictions
        # are revealed together with SPRINT_RACE.
        return ScoringPhase.SPRINT


class RoundScoringConfig(db.Model):
    """Per-round snapshot of the points configuration.

    Created when the round is first synced. The scoring engine reads from
    here, never from `current_app.config`. This guarantees historical
    leaderboards stay frozen if defaults change later.
    """

    __tablename__ = "round_scoring_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(ForeignKey("rounds.id", ondelete="CASCADE"), unique=True, nullable=False)

    # Race top 10 — per slot
    race_top10_correct: Mapped[int] = mapped_column(Integer, nullable=False)
    race_top10_one_off: Mapped[int] = mapped_column(Integer, nullable=False)
    race_top10_two_off: Mapped[int] = mapped_column(Integer, nullable=False)

    # Quali — bucketed scoring shared by quali top 3 slots and the
    # random-driver wager. JSON list of {max_delta, points}, ordered by
    # max_delta ascending; the first matching bucket wins.
    quali_position_buckets: Mapped[list] = mapped_column(JSON, nullable=False)

    # Pole time — buckets stored as JSON list of {within_seconds, points}
    pole_time_buckets: Mapped[list] = mapped_column(JSON, nullable=False)

    # Sprint
    sprint_top3_correct: Mapped[int] = mapped_column(Integer, nullable=False)
    sprint_top3_one_off: Mapped[int] = mapped_column(Integer, nullable=False)

    # Fastest lap + DNF count
    fastest_lap_correct: Mapped[int] = mapped_column(Integer, nullable=False)
    dnf_count_correct: Mapped[int] = mapped_column(Integer, nullable=False)
    dnf_count_one_off: Mapped[int] = mapped_column(Integer, nullable=False)

    # Note: places_gained has no per-config knobs — it's a simple
    # (grid - finish) calculation defined entirely in the engine.

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    round = relationship("Round", back_populates="scoring_config")

    @classmethod
    def from_defaults(cls, round_id: int, defaults: dict) -> "RoundScoringConfig":
        """Build a snapshot from `Config.SCORING_DEFAULTS`."""
        return cls(
            round_id=round_id,
            race_top10_correct=defaults["race_top10_correct"],
            race_top10_one_off=defaults["race_top10_one_off"],
            race_top10_two_off=defaults["race_top10_two_off"],
            quali_position_buckets=list(defaults["quali_position_buckets"]),
            pole_time_buckets=list(defaults["pole_time_buckets"]),
            sprint_top3_correct=defaults["sprint_top3_correct"],
            sprint_top3_one_off=defaults["sprint_top3_one_off"],
            fastest_lap_correct=defaults["fastest_lap_correct"],
            dnf_count_correct=defaults["dnf_count_correct"],
            dnf_count_one_off=defaults["dnf_count_one_off"],
        )
