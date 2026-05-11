"""Database ingestion helpers.

Every function here is idempotent — safe to call multiple times. The
worker leans on this for crash-resilience: a job can re-run after a
restart and reach the same end state.

Convention: ingest functions take an active SQLAlchemy session and the
relevant API dataclasses. They never commit — the caller controls
transaction boundaries.
"""
from __future__ import annotations

import logging
import random as random_mod
from datetime import datetime, timezone

from sqlalchemy.orm import Session as DbSession

from app.api.jolpica import (
    APIDriver,
    APIPitStop,
    APIQualifyingEntry,
    APIRaceEntry,
    APIRoundEntry,
    APIRound,
    country_to_iso2,
)
from app.config import Config
from app.models.driver import Driver, RoundDriver
from app.models.pitstop import PitStop
from app.models.prediction import PredictionScore, PredictionType
from app.models.result import SessionResult
from app.specials import SPECIAL_KEYS
from app.models.round import (
    Round,
    RoundScoringConfig,
    RoundState,
    ScoringPhase,
    Session,
    SessionStatus,
    SessionType,
    WeekendType,
)

log = logging.getLogger(__name__)


# =============================================================================
# Phase ↔ kind mapping
# =============================================================================

PHASE_KINDS: dict[ScoringPhase, tuple[PredictionType, ...]] = {
    ScoringPhase.SPRINT: (PredictionType.SPRINT_TOP3,),
    ScoringPhase.QUALI:  (
        PredictionType.QUALI_TOP3,
        PredictionType.POLE_TIME,
        PredictionType.QUALI_RANDOM_DRIVER,
        PredictionType.QUALI_HEAD_TO_HEAD,
        PredictionType.QUALI_NTH,
    ),
    ScoringPhase.RACE:   (
        PredictionType.RACE_TOP10,
        PredictionType.FASTEST_LAP,
        PredictionType.DNF_COUNT,
        PredictionType.PLACES_GAINED,
        PredictionType.SPECIAL,
    ),
}


def session_triggers_phase(session_type: SessionType) -> ScoringPhase | None:
    """Return the reveal phase whose scoring is triggered by completion of
    this session. Sprint qualifying does NOT trigger on its own — its
    predictions are revealed when the sprint race completes."""
    if session_type == SessionType.SPRINT_RACE:
        return ScoringPhase.SPRINT
    if session_type == SessionType.QUALIFYING:
        return ScoringPhase.QUALI
    if session_type == SessionType.RACE:
        return ScoringPhase.RACE
    return None


# =============================================================================
# Driver master list
# =============================================================================


def upsert_driver(db: DbSession, api_driver: APIDriver) -> Driver:
    """Find-or-create a Driver row, refreshing display fields each time."""
    driver = db.query(Driver).filter_by(driver_ref=api_driver.driver_ref).one_or_none()
    if driver is None:
        driver = Driver(driver_ref=api_driver.driver_ref)
        db.add(driver)
    driver.given_name = api_driver.given_name
    driver.family_name = api_driver.family_name
    driver.code = api_driver.code
    driver.permanent_number = api_driver.permanent_number
    driver.nationality = api_driver.nationality
    return driver


def upsert_drivers(db: DbSession, api_drivers: list[APIDriver]) -> dict[str, Driver]:
    """Upsert a batch of drivers. Returns {driver_ref: Driver}."""
    out: dict[str, Driver] = {}
    for ad in api_drivers:
        out[ad.driver_ref] = upsert_driver(db, ad)
    db.flush()
    return out


# =============================================================================
# Random quali driver pick (one-time, per round)
# =============================================================================


def _assign_random_quali_driver(db: DbSession, target_round: Round) -> bool:
    """Pick a random driver from the round's lineup for the per-round
    quali wager. Idempotent — does nothing if already set or if the
    lineup is empty.

    Deliberately non-deterministic (`random.choice`). Once the column is
    populated it is never overwritten, so the pick is stable for the rest
    of the round's lifetime even across worker restarts and re-syncs.
    """
    if target_round.random_quali_driver_id is not None:
        return False
    if not target_round.round_drivers:
        return False
    pick = random_mod.choice(list(target_round.round_drivers))
    target_round.random_quali_driver_id = pick.id
    db.flush()
    log.info(
        "random_quali_driver: round %d/%d → driver_id=%d (car #%d)",
        target_round.season, target_round.round_number,
        pick.expected_driver_id, pick.car_number,
    )
    return True

def _assign_qh2h_drivers(db: DbSession, target_round: Round) -> bool:
    """Pick two RoundDriver rows from the same team for the quali
    head-to-head wager. Only considers teams with exactly two drivers
    (so substitution rounds with a 3rd driver entered skip that team).

    Idempotent — does nothing if both columns are already set.
    """
    if (
        target_round.qh2h_driver_a_id is not None
        and target_round.qh2h_driver_b_id is not None
    ):
        return False
    teams: dict[str | None, list[RoundDriver]] = {}
    for rd in target_round.round_drivers:
        teams.setdefault(rd.constructor_name, []).append(rd)
    eligible = [
        roster for team_name, roster in teams.items()
        if team_name is not None and len(roster) == 2
    ]
    if not eligible:
        return False
    chosen = random_mod.choice(eligible)
    # Order is arbitrary but stable once set.
    target_round.qh2h_driver_a_id = chosen[0].id
    target_round.qh2h_driver_b_id = chosen[1].id
    db.flush()
    log.info(
        "qh2h: round %d/%d → team=%s cars #%d vs #%d",
        target_round.season, target_round.round_number,
        chosen[0].constructor_name, chosen[0].car_number, chosen[1].car_number,
    )
    return True


def _assign_quali_nth_position(db: DbSession, target_round: Round) -> bool:
    """Pick N in [11, field_size] for the 'who will qualify Nth' wager.

    Idempotent. Skipped if the lineup is too small for the range to be
    valid (shouldn't happen in F1, defensive only).
    """
    if target_round.quali_nth_position is not None:
        return False
    field_size = len(target_round.round_drivers)
    if field_size < 11:
        return False
    target_round.quali_nth_position = random_mod.randint(11, field_size)
    db.flush()
    log.info(
        "quali_nth: round %d/%d → N=%d",
        target_round.season, target_round.round_number,
        target_round.quali_nth_position,
    )
    return True


def _assign_specials(db: DbSession, target_round: Round) -> bool:
    """Draw two distinct specials from the bank of 8 for this round.

    Idempotent — does nothing if both columns are already set. Specials
    are lineup-independent, but for simplicity we draw them at the same
    time as the lineup-dependent selections.
    """
    if (
        target_round.special_a_key is not None
        and target_round.special_b_key is not None
    ):
        return False
    a, b = random_mod.sample(SPECIAL_KEYS, 2)
    target_round.special_a_key = a
    target_round.special_b_key = b
    db.flush()
    log.info(
        "specials: round %d/%d → %s, %s",
        target_round.season, target_round.round_number, a, b,
    )
    return True


def _assign_round_selections(db: DbSession, target_round: Round) -> None:
    """Run all four one-time round-level random selections.

    Each is independently idempotent — safe to call after every lineup
    change. Called from the two places that populate RoundDriver.
    """
    _assign_random_quali_driver(db, target_round)
    _assign_qh2h_drivers(db, target_round)
    _assign_quali_nth_position(db, target_round)
    _assign_specials(db, target_round)


# =============================================================================
# Schedule (Round + Session)
# =============================================================================


_SESSION_TYPE_FROM_API = {
    "qualifying": SessionType.QUALIFYING,
    "race": SessionType.RACE,
    "sprint_quali": SessionType.SPRINT_QUALI,
    "sprint_race": SessionType.SPRINT_RACE,
}

_SCORING_SESSION_TYPES = {
    SessionType.QUALIFYING,
    SessionType.RACE,
    SessionType.SPRINT_QUALI,
    SessionType.SPRINT_RACE,
}


def upsert_round_with_sessions(db: DbSession, api_round: APIRound) -> Round:
    """Find-or-create a Round and its Sessions; refresh display fields and
    snapshot RoundScoringConfig if not already present."""
    rd = (
        db.query(Round)
        .filter_by(season=api_round.season, round_number=api_round.round_number)
        .one_or_none()
    )
    if rd is None:
        rd = Round(
            season=api_round.season,
            round_number=api_round.round_number,
            gp_name=api_round.gp_name,
        )
        db.add(rd)
        db.flush()

    rd.gp_name = api_round.gp_name
    rd.country = api_round.country
    rd.country_code = api_round.country_code or country_to_iso2(api_round.country)
    rd.circuit_name = api_round.circuit_name
    rd.circuit_ref = api_round.circuit_ref
    rd.weekend_type = WeekendType.SPRINT if api_round.has_sprint else WeekendType.STANDARD

    # Snapshot scoring config the first time we see this round.
    if rd.scoring_config is None:
        rd.scoring_config = RoundScoringConfig.from_defaults(
            round_id=rd.id, defaults=Config.SCORING_DEFAULTS,
        )

    # Upsert sessions
    existing_by_type = {s.session_type: s for s in rd.sessions}
    seen_types: set[SessionType] = set()
    for api_sess in api_round.sessions:
        st = _SESSION_TYPE_FROM_API.get(api_sess.session_type)
        if st is None:
            continue
        seen_types.add(st)
        sess = existing_by_type.get(st)
        if sess is None:
            sess = Session(round=rd, session_type=st, scheduled_start=api_sess.scheduled_start)
            db.add(sess)
        else:
            # Don't move the start time backwards once a session has begun
            # progressing through states — admin can override manually.
            if sess.status == SessionStatus.UPCOMING:
                sess.scheduled_start = api_sess.scheduled_start

    # Recompute predictions deadline from the earliest scoring session.
    scoring_sessions = [s for s in rd.sessions if s.session_type in _SCORING_SESSION_TYPES]
    if scoring_sessions:
        first = min(scoring_sessions, key=lambda s: s.scheduled_start)
        offset_min = Config.DEADLINE_OFFSET_MINUTES
        from datetime import timedelta
        rd.predictions_deadline = first.scheduled_start - timedelta(minutes=offset_min)

    db.flush()
    return rd


def copy_round_drivers_from_previous(db: DbSession, target_round: Round) -> int:
    """Seed RoundDriver for an upcoming round by copying from the most
    recent round (in the same season) that already has a lineup. Used
    when a new round appears on the schedule before its first session
    has produced an entry list. Returns rows copied."""
    if target_round.round_drivers:
        return 0  # already populated; don't clobber

    previous = (
        db.query(Round)
        .filter(
            Round.season == target_round.season,
            Round.round_number < target_round.round_number,
        )
        .order_by(Round.round_number.desc())
        .all()
    )
    for prev in previous:
        if prev.round_drivers:
            for rd in prev.round_drivers:
                db.add(RoundDriver(
                    round=target_round,
                    car_number=rd.car_number,
                    expected_driver_id=rd.expected_driver_id,
                    constructor_name=rd.constructor_name,
                ))
            db.flush()
            # Pick all round-level random selections now that the lineup exists.
            _assign_round_selections(db, target_round)
            return len(prev.round_drivers)
    return 0


def upsert_round_drivers_from_entries(
    db: DbSession, target_round: Round, entries: list[APIRoundEntry]
) -> int:
    """Refresh RoundDriver from a session's actual entry list.

    For each (car_number, driver) in the entries:
      - upsert the master Driver row
      - upsert the RoundDriver mapping for this round + car number

    Existing RoundDriver rows for cars that aren't in `entries` are kept
    (they may correspond to drivers who DNS'd this session but are still
    the regular for that seat).
    """
    api_drivers = [e.driver for e in entries]
    drivers_by_ref = upsert_drivers(db, api_drivers)

    existing_by_car = {rd.car_number: rd for rd in target_round.round_drivers}
    written = 0
    for entry in entries:
        driver = drivers_by_ref[entry.driver.driver_ref]
        rd = existing_by_car.get(entry.car_number)
        if rd is None:
            rd = RoundDriver(
                round=target_round,
                car_number=entry.car_number,
                expected_driver_id=driver.id,
                constructor_name=entry.constructor_name,
            )
            db.add(rd)
            written += 1
        else:
            rd.expected_driver_id = driver.id
            rd.constructor_name = entry.constructor_name
    db.flush()
    # Pick all round-level random selections if not already chosen.
    _assign_round_selections(db, target_round)
    return written


# =============================================================================
# Session results
# =============================================================================


def _replace_session_results(db: DbSession, session: Session) -> None:
    """Wipe existing SessionResult rows for this session — we replace
    wholesale on each ingest pass (the API gives us a complete result set)."""
    for r in list(session.results):
        db.delete(r)
    db.flush()


def ingest_qualifying_results(
    db: DbSession,
    session: Session,
    api_entries: list[APIQualifyingEntry],
    drivers_by_ref: dict[str, Driver],
) -> None:
    """Write qualifying results and compute the session's pole time."""
    _replace_session_results(db, session)

    pole_time_ms: int | None = None
    for entry in api_entries:
        driver = drivers_by_ref.get(entry.driver_ref)
        if driver is None:
            log.warning(
                "Qualifying entry references unknown driver %s — skipping",
                entry.driver_ref,
            )
            continue
        sr = SessionResult(
            session=session,
            position=entry.position,
            car_number=entry.car_number,
            actual_driver_id=driver.id,
            status="Finished",  # quali doesn't use race-style status; record as Finished
            is_classified=True,
            is_fastest_lap=False,
            best_qualifying_time_ms=entry.best_time_ms,
        )
        db.add(sr)
        if entry.position == 1 and entry.best_time_ms is not None:
            pole_time_ms = entry.best_time_ms

    session.pole_time_ms = pole_time_ms
    session.results_fetched_at = datetime.now(timezone.utc)
    session.status = SessionStatus.COMPLETED
    db.flush()


def ingest_race_results(
    db: DbSession,
    session: Session,
    api_entries: list[APIRaceEntry],
    drivers_by_ref: dict[str, Driver],
) -> None:
    """Write race / sprint race results and compute fastest-lap + DNF count."""
    _replace_session_results(db, session)

    fastest_lap_driver_id: int | None = None
    dnf_count = 0
    for entry in api_entries:
        driver = drivers_by_ref.get(entry.driver_ref)
        if driver is None:
            log.warning("Race entry references unknown driver %s — skipping", entry.driver_ref)
            continue
        sr = SessionResult(
            session=session,
            position=entry.position,
            car_number=entry.car_number,
            actual_driver_id=driver.id,
            status=entry.status,
            is_classified=entry.is_classified,
            is_fastest_lap=entry.is_fastest_lap,
            grid_position=entry.grid,
            laps_completed=entry.laps_completed,
            race_time_ms=entry.race_time_ms,
        )
        db.add(sr)
        if entry.is_fastest_lap:
            fastest_lap_driver_id = driver.id
        if not entry.is_classified:
            dnf_count += 1

    session.fastest_lap_driver_id = fastest_lap_driver_id
    session.dnf_count = dnf_count
    session.results_fetched_at = datetime.now(timezone.utc)
    session.status = SessionStatus.COMPLETED
    db.flush()

# =============================================================================
# Pit stops
# =============================================================================


def ingest_pit_stops(
    db: DbSession,
    target_round: Round,
    api_stops: list[APIPitStop],
    drivers_by_ref: dict[str, Driver],
) -> int:
    """Replace pit-stop records for a round.

    Wholesale-replace: Jolpica returns the complete set on every request,
    so we wipe and reinsert. Idempotent — safe to re-run.

    Unknown drivers (i.e. those not in the master Driver table at the
    time of ingest) are skipped with a warning, on the assumption that
    a driver appearing in pit-stop data without a matching master row
    is a data oddity worth flagging rather than silently inserting.
    Returns the number of rows written.
    """
    db.query(PitStop).filter_by(round_id=target_round.id).delete()
    db.flush()

    written = 0
    for stop in api_stops:
        driver = drivers_by_ref.get(stop.driver_ref)
        if driver is None:
            log.warning(
                "Pit stop references unknown driver %s — skipping", stop.driver_ref,
            )
            continue
        db.add(PitStop(
            round_id=target_round.id,
            driver_id=driver.id,
            lap=stop.lap,
            stop_number=stop.stop_number,
            duration_ms=stop.duration_ms,
        ))
        written += 1
    db.flush()
    return written


# =============================================================================
# Special outcomes — compute + persist for the round's two active specials
# =============================================================================


def upsert_special_outcomes(
    db: DbSession,
    target_round: Round,
    race_results: list,
    pit_stops: list,
) -> int:
    """Compute outcomes for the round's two active specials and upsert.

    Idempotent — wipes and rewrites both outcome rows on every call so
    re-running after corrected race data produces correct outcomes.
    Returns the number of outcomes written (0, 1, or 2).
    """
    from app.scoring.specials import compute_special_outcome
    from app.models.special import SpecialOutcome

    active = [k for k in (target_round.special_a_key, target_round.special_b_key) if k]
    if not active:
        return 0

    # Wipe existing outcomes for this round's active specials.
    db.query(SpecialOutcome).filter(
        SpecialOutcome.round_id == target_round.id,
        SpecialOutcome.special_key.in_(active),
    ).delete(synchronize_session=False)
    db.flush()

    written = 0
    for key in active:
        outcome = compute_special_outcome(
            round_id=target_round.id,
            special_key=key,
            race_results=race_results,
            pit_stops=pit_stops,
            round_drivers=list(target_round.round_drivers),
        )
        db.add(outcome)
        written += 1
    db.flush()
    log.info(
        "special_outcomes: round %d/%d → wrote %d outcomes (%s)",
        target_round.season, target_round.round_number, written, ", ".join(active),
    )
    return written


# =============================================================================
# Replace prediction scores for a phase (idempotent re-scoring)
# =============================================================================


def replace_phase_scores(
    db: DbSession, round_id: int, phase: ScoringPhase, score_rows: list[PredictionScore]
) -> None:
    """Delete existing PredictionScore rows for (round, phase) then insert.

    Lets us re-run scoring (e.g. after admin fixes results) without
    creating duplicates.
    """
    kinds = PHASE_KINDS[phase]
    db.query(PredictionScore).filter(
        PredictionScore.round_id == round_id,
        PredictionScore.kind.in_(kinds),
    ).delete(synchronize_session=False)
    db.flush()
    for row in score_rows:
        db.add(row)
    db.flush()


# =============================================================================
# Round-level state
# =============================================================================


def update_round_state(db: DbSession, round_obj: Round) -> None:
    """Roll up Round.state from its sessions' statuses."""
    statuses = {s.status for s in round_obj.sessions}
    scoring_sessions = [s for s in round_obj.sessions if s.session_type in _SCORING_SESSION_TYPES]
    if not scoring_sessions:
        return
    if all(s.status == SessionStatus.COMPLETED for s in scoring_sessions):
        round_obj.state = RoundState.COMPLETED
    elif any(s.status in {SessionStatus.IN_PROGRESS, SessionStatus.PENDING_RESULTS, SessionStatus.COMPLETED}
             for s in scoring_sessions):
        round_obj.state = RoundState.IN_PROGRESS
    # else: leave UPCOMING
    db.flush()
