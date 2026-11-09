"""Admin lineup edits: change which driver a seat is listed under.

Only for rounds still open for predictions. A seat keeps its identity and
team; only the listed driver changes. Picks follow the seat: every pick in
the round naming the old driver moves to the new one, so a user who picked
"OCO (Haas)" now sees "<replacement> (Haas)" on their form.

Locked rounds are never edited. Stand-ins there need no admin action:
scoring resolves seats to whoever drove them (app/seats.py).

Scope "season" applies the same driver swap to every later open round of
the season, which is how a permanent replacement is entered once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.extensions import db
from app.models.contribution import ContributionDefinition, ContributionPrediction
from app.models.driver import Driver, RoundDriver
from app.models.prediction import (
    FastestLapPrediction,
    PlacesGainedPrediction,
    QualiHeadToHeadPrediction,
    QualiNthPrediction,
    SpecialPrediction,
    Top3QualiPrediction,
    Top3SprintPrediction,
    Top10Prediction,
)
from app.models.result import SessionResult
from app.models.round import Round, Session

# Every prediction table with a driver pick (all keyed by round_id).
PICK_MODELS = (
    Top10Prediction, Top3QualiPrediction, Top3SprintPrediction,
    FastestLapPrediction, PlacesGainedPrediction,
    QualiHeadToHeadPrediction, QualiNthPrediction, SpecialPrediction,
)


class LineupError(ValueError):
    pass


@dataclass
class LineupChange:
    rounds: list[int] = field(default_factory=list)   # round numbers touched
    seats: int = 0
    picks: int = 0


def is_open(round_obj: Round, now: datetime | None = None) -> bool:
    """Predictions still editable: not locked and deadline not passed."""
    now = now or datetime.now(timezone.utc)
    if round_obj.predictions_locked:
        return False
    return round_obj.predictions_deadline is None or round_obj.predictions_deadline > now


def season_drivers(season: int) -> list[Driver]:
    """Drivers worth offering in the lineup editor: anyone listed in a
    seat or appearing in a result this season, plus admin placeholders."""
    from worker.ingest import PLACEHOLDER_PREFIX

    seat_ids = (
        db.session.query(RoundDriver.expected_driver_id)
        .join(Round, Round.id == RoundDriver.round_id)
        .filter(Round.season == season)
    )
    result_ids = (
        db.session.query(SessionResult.actual_driver_id)
        .join(Session, Session.id == SessionResult.session_id)
        .join(Round, Round.id == Session.round_id)
        .filter(Round.season == season)
    )
    return (
        db.session.query(Driver)
        .filter(
            Driver.id.in_(seat_ids)
            | Driver.id.in_(result_ids)
            | Driver.driver_ref.startswith(PLACEHOLDER_PREFIX)
        )
        .order_by(Driver.family_name, Driver.given_name)
        .all()
    )


def update_lineup(round_obj: Round, seat_changes: dict[int, int], scope: str) -> LineupChange:
    """Apply {seat_id: new_driver_id} to `round_obj`, and with
    scope == "season" to every later open round of the season.

    Validates every target round before changing anything; raises
    LineupError without side effects if any round would list a driver
    twice. Flushes; the caller commits.
    """
    if not is_open(round_obj):
        raise LineupError("Predictions for this round are closed; its lineup can't change.")
    seats_by_id = {s.id: s for s in round_obj.round_drivers}
    driver_map: dict[int, int] = {}
    for seat_id, new_driver_id in seat_changes.items():
        seat = seats_by_id.get(seat_id)
        if seat is None:
            raise LineupError(f"Seat {seat_id} isn't in this round.")
        if seat.expected_driver_id != new_driver_id:
            driver_map[seat.expected_driver_id] = new_driver_id
    if not driver_map:
        return LineupChange()
    if db.session.query(Driver.id).filter(Driver.id.in_(set(driver_map.values()))).count() \
            != len(set(driver_map.values())):
        raise LineupError("Unknown driver.")

    targets = [round_obj]
    if scope == "season":
        targets += [
            r for r in (
                db.session.query(Round)
                .filter(Round.season == round_obj.season,
                        Round.round_number > round_obj.round_number)
                .order_by(Round.round_number)
                .all()
            )
            if is_open(r)
        ]

    plans: list[tuple[Round, dict[int, int]]] = []
    labels = _labels(set(driver_map) | set(driver_map.values()))
    for target in targets:
        listed = [s.expected_driver_id for s in target.round_drivers]
        local = {old: new for old, new in driver_map.items() if old in listed}
        if not local:
            continue
        after = [local.get(d, d) for d in listed]
        dupes = {d for d in after if after.count(d) > 1}
        if dupes:
            names = ", ".join(sorted(labels.get(d, str(d)) for d in dupes))
            raise LineupError(
                f"R{target.round_number}: {names} would be listed in two seats. "
                "Move them out of their current seat in the same save."
            )
        plans.append((target, local))

    change = LineupChange()
    for target, local in plans:
        seats, picks = _apply(target, local)
        change.rounds.append(target.round_number)
        change.seats += seats
        change.picks += picks
    db.session.flush()
    return change


def _apply(round_obj: Round, driver_map: dict[int, int]) -> tuple[int, int]:
    """Relist seats and move the round's picks. Each row is read once and
    mapped once, so chains (A→B, B→C) can't double-move."""
    seats = 0
    for seat in round_obj.round_drivers:
        if seat.expected_driver_id in driver_map:
            seat.expected_driver_id = driver_map[seat.expected_driver_id]
            seats += 1

    picks = 0
    for model in PICK_MODELS:
        rows = (
            db.session.query(model)
            .filter(model.round_id == round_obj.id,
                    model.predicted_driver_id.in_(list(driver_map)))
            .all()
        )
        for row in rows:
            row.predicted_driver_id = driver_map[row.predicted_driver_id]
            picks += 1

    defs = db.session.query(ContributionDefinition).filter_by(round_id=round_obj.id).all()
    for d in defs:
        if d.allowed_driver_ids:
            d.allowed_driver_ids = [driver_map.get(i, i) for i in d.allowed_driver_ids]
        if d.actual_driver_id in driver_map:
            d.actual_driver_id = driver_map[d.actual_driver_id]
    if defs:
        rows = (
            db.session.query(ContributionPrediction)
            .filter(ContributionPrediction.contribution_id.in_([d.id for d in defs]),
                    ContributionPrediction.predicted_driver_id.in_(list(driver_map)))
            .all()
        )
        for row in rows:
            row.predicted_driver_id = driver_map[row.predicted_driver_id]
            picks += 1
    return seats, picks


def _labels(ids: set[int]) -> dict[int, str]:
    rows = db.session.query(Driver).filter(Driver.id.in_(ids)).all()
    return {d.id: d.code or d.family_name for d in rows}


_CODE_RE = re.compile(r"^[A-Z]{3}$")


def add_placeholder_driver(given_name: str, family_name: str, code: str) -> Driver:
    """Create a driver Jolpica doesn't know yet (announced debutant).

    driver_ref is a placeholder; the first Jolpica record with the same
    family name claims this row (worker.ingest._claim_placeholder), so
    seats and picks pointing at it carry over.
    """
    from worker.ingest import PLACEHOLDER_PREFIX

    given_name, family_name = given_name.strip(), family_name.strip()
    code = code.strip().upper()
    if not given_name or not family_name:
        raise LineupError("Given and family name are both required.")
    if not _CODE_RE.match(code):
        raise LineupError("Code must be three letters, e.g. TSU.")
    slug = re.sub(r"[^a-z0-9]+", "_", family_name.lower()).strip("_")
    ref = f"{PLACEHOLDER_PREFIX}{slug}"
    if db.session.query(Driver.id).filter_by(driver_ref=ref).first():
        raise LineupError(f"A placeholder for {family_name} already exists.")
    driver = Driver(driver_ref=ref, given_name=given_name, family_name=family_name, code=code)
    db.session.add(driver)
    db.session.flush()
    return driver
