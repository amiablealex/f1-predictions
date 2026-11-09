"""Provisional results: what's missing from a session's data, and which
picks to hold back while it is.

Jolpica publishes results in stages — finishing order first, grid,
fastest lap, pit stops and quali times later (sometimes much later).
A session whose data is incomplete sits in `SessionStatus.PROVISIONAL`:
it is scored with what exists, and picks that depend on a missing input
are withheld (no score row) rather than scored as 0.

Everything here is derived from what is already in the database, so the
worker, the round page and the admin page all agree without storing
anything extra.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.extensions import db
from app.models.pitstop import PitStop
from app.models.prediction import PredictionScore, PredictionType
from app.models.result import SessionResult
from app.models.round import Session, SessionStatus, SessionType

GRID = "grid"
FASTEST_LAP = "fastest_lap"
PIT_STOPS = "pit_stops"
POLE_TIME = "pole_time"

LABELS = {
    GRID: "starting grid",
    FASTEST_LAP: "fastest lap",
    PIT_STOPS: "pit stops",
    POLE_TIME: "pole time",
}

# Missing input → (prediction kinds, special keys) that can't be scored.
_GAPS: dict[str, tuple[frozenset, frozenset]] = {
    GRID: (frozenset({PredictionType.PLACES_GAINED}), frozenset({"pole_sitter_wins"})),
    FASTEST_LAP: (frozenset({PredictionType.FASTEST_LAP}), frozenset()),
    PIT_STOPS: (
        frozenset(),
        frozenset({"most_pitstops", "lap_of_first_pitstop", "longest_stint"}),
    ),
    POLE_TIME: (frozenset({PredictionType.POLE_TIME}), frozenset()),
}


@dataclass(frozen=True)
class Withheld:
    kinds: frozenset = field(default_factory=frozenset)
    special_keys: frozenset = field(default_factory=frozenset)

    def __bool__(self) -> bool:
        return bool(self.kinds or self.special_keys)

    def covers(self, kind: PredictionType, special_key: str | None = None) -> bool:
        if kind in self.kinds:
            return True
        return kind == PredictionType.SPECIAL and special_key in self.special_keys

    def covers_score(self, row: PredictionScore) -> bool:
        return self.covers(row.kind, row.special_key)


NOTHING_WITHHELD = Withheld()


def missing_inputs(session: Session) -> set[str]:
    """Inputs Jolpica hasn't supplied yet for a session that has results.

    Queries rather than relationship collections so it's correct straight
    after an ingest flush.
    """
    if session.session_type == SessionType.QUALIFYING:
        return set() if session.pole_time_ms is not None else {POLE_TIME}

    if session.session_type not in (SessionType.RACE, SessionType.SPRINT_RACE):
        return set()

    rows = db.session.query(SessionResult).filter_by(session_id=session.id).all()
    if not rows:
        return set()
    missing: set[str] = set()
    with_grid = sum(1 for r in rows if r.grid_position is not None)
    if with_grid < len(rows) * 0.8:
        missing.add(GRID)
    if not any(r.is_fastest_lap for r in rows):
        missing.add(FASTEST_LAP)
    if session.session_type == SessionType.RACE:
        stops = db.session.query(PitStop.id).filter_by(round_id=session.round_id).first()
        if stops is None:
            missing.add(PIT_STOPS)
    return missing


def withheld_for(missing: set[str]) -> Withheld:
    kinds: set = set()
    keys: set = set()
    for item in missing:
        k, s = _GAPS.get(item, (frozenset(), frozenset()))
        kinds |= k
        keys |= s
    return Withheld(frozenset(kinds), frozenset(keys))


def withheld_for_session(session: Session | None) -> Withheld:
    """Picks to hold back for a session. Only provisional sessions withhold."""
    if session is None or session.status != SessionStatus.PROVISIONAL:
        return NOTHING_WITHHELD
    return withheld_for(missing_inputs(session))


def describe(missing: set[str]) -> str:
    """Admin-facing list, e.g. 'starting grid, fastest lap'."""
    return ", ".join(LABELS.get(m, m) for m in sorted(missing))
