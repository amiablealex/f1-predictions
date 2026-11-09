"""Scheduled job functions.

Each job is a plain callable taking (app, client) — APScheduler in the
worker process schedules them at fixed intervals; admin routes in the web
service can call them on demand. Jobs run inside an app context so
SQLAlchemy works.

All jobs are idempotent. Crash mid-job, restart, re-run — same end state.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from flask import Flask, current_app
from sqlalchemy.orm import joinedload

from app.api.exceptions import (
    JolpicaError,
    JolpicaNotFoundError,
    JolpicaTransientError,
)
from app.api.jolpica import JolpicaClient
from app.extensions import db
from app.models.driver import Driver, RoundDriver
from app.models.prediction import (
    DnfCountPrediction,
    FastestLapPrediction,
    PlacesGainedPrediction,
    PoleTimePrediction,
    QualiHeadToHeadPrediction,
    QualiNthPrediction,
    QualiRandomDriverPrediction,
    SpecialPrediction,
    Top3QualiPrediction,
    Top3SprintPrediction,
    Top10Prediction,
)
from app.models.pitstop import PitStop
from app.models.result import SessionResult
from app.models.special import SpecialOutcome
from app.models.round import (
    Round,
    RoundState,
    ScoringPhase,
    Session,
    SessionStatus,
    SessionType,
)
from app.scoring.engine import UserPredictions, build_phase_scores
from app.scoring.provisional import describe as describe_missing
from app.scoring.provisional import missing_inputs, withheld_for_session
from worker.ingest import (
    PHASE_KINDS,
    copy_round_drivers_from_previous,
    ingest_pit_stops,
    ingest_qualifying_results,
    ingest_race_results,
    replace_phase_scores,
    session_triggers_phase,
    update_round_state,
    upsert_drivers,
    upsert_round_drivers_from_entries,
    upsert_round_with_sessions,
    upsert_special_outcomes,
)

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# =============================================================================
# Job 1: schedule sync
# =============================================================================


def schedule_sync_job(app: Flask, client: JolpicaClient) -> None:
    """Pull season schedule from Jolpica → upsert Round + Session rows.

    Also seeds RoundDriver for newly-created upcoming rounds by copying
    from the most recent round in the same season.
    """
    season = app.config["F1_SEASON"]
    log.info("schedule_sync: fetching season %d", season)
    try:
        api_rounds = client.get_season_schedule(season)
    except JolpicaError as exc:
        log.error("schedule_sync: API error: %s", exc)
        return

    with app.app_context():
        for api_round in api_rounds:
            rd = upsert_round_with_sessions(db.session, api_round)
            # Seed RoundDriver if empty (will be refreshed from results
            # once the first session of the round completes).
            if not rd.round_drivers:
                copied = copy_round_drivers_from_previous(db.session, rd)
                if copied:
                    log.info(
                        "schedule_sync: seeded %d RoundDriver rows for %s/round %d",
                        copied, rd.season, rd.round_number,
                    )
        db.session.commit()
    log.info("schedule_sync: done (%d rounds)", len(api_rounds))


# =============================================================================
# Job 2: master driver list sync
# =============================================================================


def driver_master_sync_job(app: Flask, client: JolpicaClient) -> None:
    """Refresh the master Driver list for the season."""
    season = app.config["F1_SEASON"]
    log.info("driver_master_sync: season %d", season)
    try:
        api_drivers = client.get_season_drivers(season)
    except JolpicaError as exc:
        log.error("driver_master_sync: API error: %s", exc)
        return
    with app.app_context():
        upsert_drivers(db.session, api_drivers)
        db.session.commit()
    log.info("driver_master_sync: %d drivers", len(api_drivers))


# =============================================================================
# Job 3: session state transitions
# =============================================================================


def session_state_transitions_job(app: Flask, client: JolpicaClient) -> None:
    """Walk session statuses forward based on the wall clock.

      upcoming      → in_progress       at scheduled_start
      in_progress   → pending_results   at scheduled_start + duration
    Sessions in `pending_results` are picked up by results_poll_job.
    """
    durations = app.config["SESSION_DURATION_MINUTES"]
    timeout_hours = app.config["RESULTS_PENDING_TIMEOUT_HOURS"]
    now = _utcnow()

    with app.app_context():
        # Only consider sessions of rounds that aren't fully done.
        sessions = (
            db.session.query(Session)
            .join(Round)
            .filter(Round.state != RoundState.COMPLETED)
            .all()
        )
        changed = 0
        for s in sessions:
            sched_start = s.scheduled_start
            if sched_start is None:
                continue
            if s.status == SessionStatus.UPCOMING and now >= sched_start:
                s.status = SessionStatus.IN_PROGRESS
                changed += 1
                log.info("session_state: %s round %d → in_progress",
                         s.session_type.value, s.round_id)
            if s.status == SessionStatus.IN_PROGRESS:
                duration_min = durations.get(s.session_type.value, 120)
                if now >= sched_start + timedelta(minutes=duration_min):
                    if s.session_type == SessionType.SPRINT_QUALI:
                        # Sprint qualifying is deadline-only — Jolpica has
                        # no SQ endpoint, so we don't fetch anything. Just
                        # mark it completed once the session window passes.
                        s.status = SessionStatus.COMPLETED
                        s.results_fetched_at = now
                    else:
                        s.status = SessionStatus.PENDING_RESULTS
                    changed += 1
                    log.info("session_state: %s round %d → %s",
                             s.session_type.value, s.round_id, s.status.value)
            if s.status == SessionStatus.PENDING_RESULTS and s.results_fetched_at is None:
                # If pending too long, log a warning so admin can investigate.
                if now >= sched_start + timedelta(hours=timeout_hours):
                    log.warning(
                        "session_state: %s round %d has been pending >%dh — admin investigate",
                        s.session_type.value, s.round_id, timeout_hours,
                    )
        # Roll up round states
        for round_obj in {s.round for s in sessions}:
            update_round_state(db.session, round_obj)

        # Commit unconditionally: a round roll-up can change even when no
        # session did this tick (e.g. after an admin data fix).
        db.session.commit()


# =============================================================================
# Job 4: results polling
# =============================================================================


def _drivers_by_ref(season_driver_refs: set[str]) -> dict[str, Driver]:
    """Fetch master Driver rows for a set of driver refs."""
    if not season_driver_refs:
        return {}
    rows = db.session.query(Driver).filter(Driver.driver_ref.in_(season_driver_refs)).all()
    return {d.driver_ref: d for d in rows}

def _race_results_complete(
    api_results: list,
    session_obj: Session,
) -> tuple[bool, str]:
    """Validate completeness of fetched race-style results before ingest.

    Returns (is_complete, reason). If the session has been pending past
    the configured grace period, accepts whatever's available so we
    don't retry indefinitely when upstream data is permanently
    incomplete.

    Completeness criteria for race / sprint race:
      - At least one row in results
      - grid populated on >=80% of rows (allows a couple of DNS/DNQ
        oddities while still catching the case where the entire grid
        field is missing)
      - At least one row flagged as the fastest-lap setter

    Qualifying is not gated here — its data shape is simpler and the
    late-classification failure mode that motivated this gate doesn't
    apply to quali results.
    """
    grace_hours = current_app.config.get("RESULTS_PENDING_TIMEOUT_HOURS", 6)
    if session_obj.scheduled_start is not None:
        elapsed = _utcnow() - session_obj.scheduled_start
        if elapsed > timedelta(hours=grace_hours):
            return (True, f"past {grace_hours}h grace period; accepting partial data")

    total = len(api_results)
    if total == 0:
        return (False, "no result rows")

    grid_count = sum(1 for r in api_results if r.grid is not None)
    if grid_count < total * 0.8:
        return (False, f"grid populated on only {grid_count}/{total} rows")

    fl_count = sum(1 for r in api_results if r.is_fastest_lap)
    if fl_count == 0:
        return (False, "no row flagged as fastest-lap setter")

    return (True, "ok")

_POLLED_STATUSES = (SessionStatus.PENDING_RESULTS, SessionStatus.PROVISIONAL)


def results_poll_job(app: Flask, client: JolpicaClient) -> None:
    """For every session awaiting results or still provisional, fetch and ingest.

    With PROVISIONAL_RESULTS_ENABLED (default), any non-empty result set is
    ingested and scored straight away. If inputs are still missing (grid,
    fastest lap, pit stops, pole time) the session goes PROVISIONAL and is
    re-polled; once the data is complete, or RESULTS_FINALISE_AFTER_HOURS
    passes, it becomes COMPLETED. A provisional session whose data hasn't
    changed since the last poll is left untouched (rolled back).

    With the flag off, the old gate applies: race-style results wait until
    complete, and partial data is accepted after RESULTS_PENDING_TIMEOUT_HOURS.
    """
    with app.app_context():
        provisional_on = current_app.config.get("PROVISIONAL_RESULTS_ENABLED", True)
        session_ids = [
            sid for (sid,) in db.session.query(Session.id)
            .filter(Session.status.in_(_POLLED_STATUSES))
            .order_by(Session.scheduled_start)
            .all()
        ]
        for sid in session_ids:
            # Re-fetch each time: an unchanged poll rolls back, expiring objects.
            s = db.session.get(Session, sid)
            if s is None or s.status not in _POLLED_STATUSES:
                continue
            _poll_session(app, client, s, provisional_on)


def _poll_session(app: Flask, client: JolpicaClient, s: Session, provisional_on: bool) -> None:
    round_obj = s.round
    prev_status = s.status
    before = _results_signature(s)
    log.info(
        "results_poll: trying %s for %d/%d",
        s.session_type.value, round_obj.season, round_obj.round_number,
    )
    try:
        _fetch_and_ingest_session(client, round_obj, s, gate=not provisional_on)
    except JolpicaNotFoundError:
        log.info(
            "results_poll: results not yet available for %s round %d",
            s.session_type.value, round_obj.round_number,
        )
        _finalise_if_overdue(app, s)
        return
    except JolpicaTransientError as exc:
        log.warning("results_poll: transient error, will retry: %s", exc)
        _finalise_if_overdue(app, s)
        return
    except JolpicaError as exc:
        db.session.rollback()
        log.exception("results_poll: hard error on %s round %d: %s",
                      s.session_type.value, round_obj.round_number, exc)
        return

    missing: set[str] = set()
    if provisional_on:
        missing = _apply_completeness(s)

    if (
        prev_status == SessionStatus.PROVISIONAL
        and s.status == SessionStatus.PROVISIONAL
        and _results_signature(s) == before
    ):
        db.session.rollback()
        log.info(
            "results_poll: %s round %d unchanged, still provisional (missing: %s)",
            s.session_type.value, round_obj.round_number, describe_missing(missing),
        )
        return

    # Refresh the round-driver mapping from this session's entries
    # the first time results land for the round.
    try:
        _refresh_round_drivers_if_first_session(client, round_obj, s)
    except JolpicaError as exc:
        log.warning("results_poll: round-driver refresh failed: %s", exc)

    if s.status == SessionStatus.PROVISIONAL:
        log.info(
            "results_poll: %s round %d provisional (missing: %s)",
            s.session_type.value, round_obj.round_number, describe_missing(missing),
        )
    _commit_and_score(app, s)


def _results_signature(s: Session) -> tuple:
    """Everything an ingest can change for a session, read from the DB.

    Compared before and after a re-ingest of a provisional session to skip
    re-scoring when Jolpica hasn't published anything new.
    """
    rows = (
        db.session.query(
            SessionResult.position, SessionResult.car_number, SessionResult.status,
            SessionResult.grid_position, SessionResult.laps_completed,
            SessionResult.race_time_ms, SessionResult.is_fastest_lap,
            SessionResult.best_qualifying_time_ms,
        )
        .filter(SessionResult.session_id == s.id)
        .order_by(SessionResult.position)
        .all()
    )
    stops: list = []
    if s.session_type == SessionType.RACE:
        stops = (
            db.session.query(PitStop.driver_id, PitStop.stop_number, PitStop.lap)
            .filter(PitStop.round_id == s.round_id)
            .order_by(PitStop.driver_id, PitStop.stop_number)
            .all()
        )
    return (
        tuple(tuple(r) for r in rows),
        tuple(tuple(r) for r in stops),
        s.pole_time_ms,
    )


def _is_overdue(s: Session) -> bool:
    hours = current_app.config.get("RESULTS_FINALISE_AFTER_HOURS", 48)
    if s.scheduled_start is None:
        return False
    return _utcnow() >= s.scheduled_start + timedelta(hours=hours)


def _apply_completeness(s: Session) -> set[str]:
    """Set PROVISIONAL or COMPLETED on a freshly-ingested session."""
    missing = missing_inputs(s)
    if not missing:
        s.status = SessionStatus.COMPLETED
    elif _is_overdue(s):
        s.status = SessionStatus.COMPLETED
        log.warning(
            "results_poll: %s round %d finalised with data still missing (%s)",
            s.session_type.value, s.round_id, describe_missing(missing),
        )
    else:
        s.status = SessionStatus.PROVISIONAL
    return missing


def _finalise_if_overdue(app: Flask, s: Session) -> None:
    """After a failed poll: a provisional session past the cut-off is
    finalised with the data it already has."""
    db.session.rollback()
    if s.status != SessionStatus.PROVISIONAL or not _is_overdue(s):
        return
    log.warning(
        "results_poll: %s round %d finalised at cut-off (missing: %s)",
        s.session_type.value, s.round_id, describe_missing(missing_inputs(s)),
    )
    s.status = SessionStatus.COMPLETED
    _commit_and_score(app, s)


def _commit_and_score(app: Flask, s: Session) -> None:
    """Commit, roll up the round state, and re-score the session's phase."""
    db.session.commit()
    update_round_state(db.session, s.round)
    db.session.commit()
    phase = session_triggers_phase(s.session_type)
    if phase is not None:
        phase_scoring_job(app, s.round_id, phase)


def finalise_session(app: Flask, s: Session) -> None:
    """Admin: accept a provisional session's data as final."""
    s.status = SessionStatus.COMPLETED
    _commit_and_score(app, s)


def reassess_session(app: Flask, s: Session) -> None:
    """Admin: after a manual data fix, re-check completeness and re-score."""
    if s.status == SessionStatus.PROVISIONAL:
        _apply_completeness(s)
    if s.status in (SessionStatus.PROVISIONAL, SessionStatus.COMPLETED):
        _commit_and_score(app, s)
    else:
        db.session.commit()


def _check_race_results(api_results: list, session_obj: Session, gate: bool) -> None:
    if gate:
        complete, reason = _race_results_complete(api_results, session_obj)
        if not complete:
            raise JolpicaTransientError(
                f"{session_obj.session_type.value} results for round "
                f"{session_obj.round_id} incomplete: {reason}"
            )
    elif not api_results:
        raise JolpicaTransientError("no result rows")


def _fetch_and_ingest_session(
    client: JolpicaClient, round_obj: Round, session_obj: Session, gate: bool = True,
) -> None:
    """Dispatch to the right Jolpica endpoint, then write results to the DB.

    `gate=True` holds race-style results back until they look complete
    (the pre-provisional behaviour); `gate=False` ingests any non-empty set.
    """
    season, round_no = round_obj.season, round_obj.round_number
    if session_obj.session_type == SessionType.QUALIFYING:
        api_results = client.get_qualifying_results(season, round_no)
        refs = {e.driver_ref for e in api_results}
        drivers = _drivers_by_ref(refs)
        ingest_qualifying_results(db.session, session_obj, api_results, drivers)
    elif session_obj.session_type == SessionType.RACE:
        api_results = client.get_race_results(season, round_no)
        _check_race_results(api_results, session_obj, gate)
        refs = {e.driver_ref for e in api_results}
        drivers = _drivers_by_ref(refs)
        ingest_race_results(db.session, session_obj, api_results, drivers)
        # Fetch pit-stops in the same pass — needed for specials.
        try:
            api_stops = client.get_pit_stops(season, round_no)
            stop_refs = {s.driver_ref for s in api_stops}
            stop_drivers = _drivers_by_ref(stop_refs)
            ingest_pit_stops(db.session, round_obj, api_stops, stop_drivers)
        except JolpicaNotFoundError:
            log.info("pit-stops not yet available for round %d", round_no)
        # Compute special outcomes from the freshly-ingested race + stops.
        # Queried rather than read from relationship collections, which
        # can still hold rows deleted by the replace above.
        upsert_special_outcomes(
            db.session, round_obj,
            race_results=(
                db.session.query(SessionResult)
                .filter_by(session_id=session_obj.id)
                .order_by(SessionResult.position)
                .all()
            ),
            pit_stops=db.session.query(PitStop).filter_by(round_id=round_obj.id).all(),
        )
    elif session_obj.session_type == SessionType.SPRINT_RACE:
        api_results = client.get_sprint_race_results(season, round_no)
        _check_race_results(api_results, session_obj, gate)
        refs = {e.driver_ref for e in api_results}
        drivers = _drivers_by_ref(refs)
        ingest_race_results(db.session, session_obj, api_results, drivers)
    elif session_obj.session_type == SessionType.SPRINT_QUALI:
        # Deadline-only, nothing to fetch. Only lands here via the admin
        # refresh button; complete it again rather than leave it pending.
        session_obj.status = SessionStatus.COMPLETED
        session_obj.results_fetched_at = _utcnow()
    else:
        log.warning("Unexpected session type for results poll: %s", session_obj.session_type)


def _refresh_round_drivers_if_first_session(
    client: JolpicaClient, round_obj: Round, just_completed: Session,
) -> None:
    """When the first results of a round land, refresh RoundDriver from
    the actual entry list. This keeps the lineup up-to-date when the
    seeded copy from the previous round was stale."""
    landed = (SessionStatus.COMPLETED, SessionStatus.PROVISIONAL)
    completed_count = sum(1 for s in round_obj.sessions if s.status in landed)
    if completed_count > 1:
        return  # not the first
    try:
        entries = client.get_round_entries(
            round_obj.season, round_obj.round_number, prefer_session="qualifying"
        )
    except JolpicaNotFoundError:
        return
    upsert_round_drivers_from_entries(db.session, round_obj, entries)


# =============================================================================
# Job 5: phase scoring
# =============================================================================


def phase_scoring_job(app: Flask, round_id: int, phase: ScoringPhase) -> None:
    """Run the scoring engine for a (round, phase) and write score rows.

    Called from within results_poll_job. Also exposed for admin re-runs.
    Caller is responsible for being in an app context — but if we're
    not, we set one up.
    """
    if not _has_app_context():
        with app.app_context():
            _phase_scoring_inner(round_id, phase)
    else:
        _phase_scoring_inner(round_id, phase)


_PHASE_SESSION_TYPE = {
    ScoringPhase.SPRINT: SessionType.SPRINT_RACE,
    ScoringPhase.QUALI: SessionType.QUALIFYING,
    ScoringPhase.RACE: SessionType.RACE,
}


def _has_app_context() -> bool:
    from flask import has_app_context
    return has_app_context()


def _phase_scoring_inner(round_id: int, phase: ScoringPhase) -> None:
    round_obj = (
        db.session.query(Round)
        .options(
            joinedload(Round.sessions).joinedload(Session.results),
            joinedload(Round.round_drivers),
            joinedload(Round.scoring_config),
            joinedload(Round.random_quali_driver),
            joinedload(Round.qh2h_driver_a),
            joinedload(Round.qh2h_driver_b),
            joinedload(Round.special_outcomes),
        )
        .filter(Round.id == round_id)
        .one_or_none()
    )
    if round_obj is None:
        log.error("phase_scoring: round %d not found", round_id)
        return
    config = round_obj.scoring_config
    if config is None:
        log.error("phase_scoring: round %d has no scoring config", round_id)
        return

    # SPRINT phase scores sprint race results only. Sprint quali is
    # deadline-only and never scored, so its status doesn't gate this.
    sessions_by_type = {s.session_type: s for s in round_obj.sessions}
    if phase == ScoringPhase.SPRINT:
        sr = sessions_by_type.get(SessionType.SPRINT_RACE)
        if sr is None:
            log.warning("phase_scoring: SPRINT phase on a non-sprint round; skipping")
            return
        if sr.status not in (SessionStatus.COMPLETED, SessionStatus.PROVISIONAL):
            log.info("phase_scoring: SPRINT phase waiting for sprint race to complete")
            return

    # Find every user who has any prediction for this round.
    user_ids = _user_ids_with_predictions(round_id)
    if not user_ids:
        log.info("phase_scoring: round %d has no predictions to score", round_id)
        return

    log.info("phase_scoring: round %d phase %s for %d users",
             round_id, phase.value, len(user_ids))

    special_outcomes_by_key = {o.special_key: o for o in round_obj.special_outcomes}

    # Picks whose inputs are still missing from a provisional session get
    # no score row (shown as unscored, not 0).
    phase_session = sessions_by_type.get(_PHASE_SESSION_TYPE[phase])
    withheld = withheld_for_session(phase_session)

    all_score_rows = []
    for user_id in user_ids:
        user_preds = _load_user_predictions(user_id, round_id)
        rows = build_phase_scores(
            user_id=user_id,
            round_id=round_id,
            phase=phase,
            user_preds=user_preds,
            sessions_by_type=sessions_by_type,
            round_drivers=list(round_obj.round_drivers),
            config=config,
            round_obj=round_obj,
            special_outcomes_by_key=special_outcomes_by_key,
            withheld=withheld,
        )
        all_score_rows.extend(rows)

    replace_phase_scores(db.session, round_id, phase, all_score_rows)

    # Mark the relevant session(s) as scored
    now = _utcnow()
    if phase == ScoringPhase.SPRINT:
        for st in (SessionType.SPRINT_QUALI, SessionType.SPRINT_RACE):
            s = sessions_by_type.get(st)
            if s is not None:
                s.scored_at = now
    elif phase == ScoringPhase.QUALI:
        s = sessions_by_type.get(SessionType.QUALIFYING)
        if s is not None:
            s.scored_at = now
    elif phase == ScoringPhase.RACE:
        s = sessions_by_type.get(SessionType.RACE)
        if s is not None:
            s.scored_at = now

    db.session.commit()


def _user_ids_with_predictions(round_id: int) -> list[int]:
    """Return all user IDs that have at least one prediction of any type
    for this round."""
    user_id_sets: list[set[int]] = []
    for model in (
        Top10Prediction, Top3QualiPrediction, Top3SprintPrediction,
        PoleTimePrediction, FastestLapPrediction,
        DnfCountPrediction,
        PlacesGainedPrediction, QualiRandomDriverPrediction,
        QualiHeadToHeadPrediction, QualiNthPrediction,
        SpecialPrediction,
    ):
        rows = db.session.query(model.user_id).filter(model.round_id == round_id).distinct().all()
        user_id_sets.append({r[0] for r in rows})
    if not user_id_sets:
        return []
    return sorted(set().union(*user_id_sets))


def _load_user_predictions(user_id: int, round_id: int) -> UserPredictions:
    """Materialise all of a user's predictions for a round into a UserPredictions."""
    specials_rows = db.session.query(SpecialPrediction).filter_by(
        user_id=user_id, round_id=round_id,
    ).all()
    return UserPredictions(
        top10=db.session.query(Top10Prediction).filter_by(user_id=user_id, round_id=round_id).all(),
        quali_top3=db.session.query(Top3QualiPrediction).filter_by(user_id=user_id, round_id=round_id).all(),
        sprint_top3=db.session.query(Top3SprintPrediction).filter_by(user_id=user_id, round_id=round_id).all(),
        pole_time=db.session.query(PoleTimePrediction).filter_by(user_id=user_id, round_id=round_id).one_or_none(),
        fastest_lap=db.session.query(FastestLapPrediction).filter_by(user_id=user_id, round_id=round_id).one_or_none(),
        dnf_count=db.session.query(DnfCountPrediction).filter_by(user_id=user_id, round_id=round_id).one_or_none(),
        places_gained=db.session.query(PlacesGainedPrediction).filter_by(user_id=user_id, round_id=round_id).one_or_none(),
        quali_random_driver=db.session.query(QualiRandomDriverPrediction).filter_by(user_id=user_id, round_id=round_id).one_or_none(),
        qh2h=db.session.query(QualiHeadToHeadPrediction).filter_by(user_id=user_id, round_id=round_id).one_or_none(),
        qnth=db.session.query(QualiNthPrediction).filter_by(user_id=user_id, round_id=round_id).one_or_none(),
        specials={p.special_key: p for p in specials_rows},
    )


# =============================================================================
# Job 6: deadline lock
# =============================================================================


def deadline_lock_job(app: Flask, client: JolpicaClient | None = None) -> None:
    """Set Round.predictions_locked = True for any round whose deadline
    has passed but isn't yet locked."""
    now = _utcnow()
    with app.app_context():
        rounds = (
            db.session.query(Round)
            .filter(
                Round.predictions_locked.is_(False),
                Round.predictions_deadline.is_not(None),
                Round.predictions_deadline <= now,
            )
            .all()
        )
        for r in rounds:
            r.predictions_locked = True
            log.info("deadline_lock: round %d/%d locked", r.season, r.round_number)
        if rounds:
            db.session.commit()


# =============================================================================
# Convenience: full pipeline (used by admin "refresh now" trigger)
# =============================================================================


def run_full_pipeline(app: Flask, client: JolpicaClient) -> None:
    """Run schedule sync → driver sync → state transitions → results poll → lock.

    Equivalent to admin pressing "refresh everything now". Useful for
    catching up after the worker was down.
    """
    schedule_sync_job(app, client)
    driver_master_sync_job(app, client)
    session_state_transitions_job(app, client)
    results_poll_job(app, client)
    deadline_lock_job(app, client)
