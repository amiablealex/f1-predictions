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

from flask import Flask
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
    QualiRandomDriverPrediction,
    Top3QualiPrediction,
    Top3SprintPrediction,
    Top10Prediction,
)
from app.models.round import (
    Round,
    RoundState,
    ScoringPhase,
    Session,
    SessionStatus,
    SessionType,
)
from app.scoring.engine import UserPredictions, build_phase_scores
from worker.ingest import (
    PHASE_KINDS,
    copy_round_drivers_from_previous,
    ingest_qualifying_results,
    ingest_race_results,
    replace_phase_scores,
    session_triggers_phase,
    update_round_state,
    upsert_drivers,
    upsert_round_drivers_from_entries,
    upsert_round_with_sessions,
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

        if changed:
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


def results_poll_job(app: Flask, client: JolpicaClient) -> None:
    """For every session in `pending_results`, attempt to fetch and ingest.

    On success: triggers phase scoring for the session's reveal phase
    (if applicable). On 404: leaves the session pending for the next poll.
    """
    with app.app_context():
        pending = (
            db.session.query(Session)
            .options(joinedload(Session.round))
            .filter(Session.status == SessionStatus.PENDING_RESULTS)
            .all()
        )

        for s in pending:
            round_obj = s.round
            log.info(
                "results_poll: trying %s for %d/%d",
                s.session_type.value, round_obj.season, round_obj.round_number,
            )
            try:
                _fetch_and_ingest_session(client, round_obj, s)
            except JolpicaNotFoundError:
                log.info(
                    "results_poll: results not yet available for %s round %d",
                    s.session_type.value, round_obj.round_number,
                )
                continue
            except JolpicaTransientError as exc:
                log.warning("results_poll: transient error, will retry: %s", exc)
                continue
            except JolpicaError as exc:
                log.exception("results_poll: hard error on %s round %d: %s",
                              s.session_type.value, round_obj.round_number, exc)
                continue

            # Refresh the round-driver mapping from this session's entries
            # the first time results land for the round.
            try:
                _refresh_round_drivers_if_first_session(client, round_obj, s)
            except JolpicaError as exc:
                log.warning("results_poll: round-driver refresh failed: %s", exc)

            db.session.commit()
            update_round_state(db.session, round_obj)
            db.session.commit()

            # Trigger phase scoring if this session's completion completes a phase
            phase = session_triggers_phase(s.session_type)
            if phase is not None:
                phase_scoring_job(app, round_obj.id, phase)


def _fetch_and_ingest_session(client: JolpicaClient, round_obj: Round, session_obj: Session) -> None:
    """Dispatch to the right Jolpica endpoint, then write results to the DB."""
    season, round_no = round_obj.season, round_obj.round_number
    if session_obj.session_type == SessionType.QUALIFYING:
        api_results = client.get_qualifying_results(season, round_no)
        refs = {e.driver_ref for e in api_results}
        drivers = _drivers_by_ref(refs)
        ingest_qualifying_results(db.session, session_obj, api_results, drivers)
    elif session_obj.session_type == SessionType.RACE:
        api_results = client.get_race_results(season, round_no)
        refs = {e.driver_ref for e in api_results}
        drivers = _drivers_by_ref(refs)
        ingest_race_results(db.session, session_obj, api_results, drivers)
    elif session_obj.session_type == SessionType.SPRINT_RACE:
        api_results = client.get_sprint_race_results(season, round_no)
        refs = {e.driver_ref for e in api_results}
        drivers = _drivers_by_ref(refs)
        ingest_race_results(db.session, session_obj, api_results, drivers)
    else:
        # SPRINT_QUALI is deadline-only and never enters PENDING_RESULTS,
        # so this branch shouldn't be reached. Log if it ever is.
        log.warning("Unexpected session type for results poll: %s", session_obj.session_type)


def _refresh_round_drivers_if_first_session(
    client: JolpicaClient, round_obj: Round, just_completed: Session,
) -> None:
    """When the first results of a round land, refresh RoundDriver from
    the actual entry list. This keeps the lineup up-to-date when the
    seeded copy from the previous round was stale."""
    completed_count = sum(
        1 for s in round_obj.sessions if s.status == SessionStatus.COMPLETED
    )
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

    # If SPRINT phase, both sprint sessions need to be completed for the
    # output to be meaningful.
    sessions_by_type = {s.session_type: s for s in round_obj.sessions}
    if phase == ScoringPhase.SPRINT:
        sq = sessions_by_type.get(SessionType.SPRINT_QUALI)
        sr = sessions_by_type.get(SessionType.SPRINT_RACE)
        if sq is None or sr is None:
            log.warning("phase_scoring: SPRINT phase on a non-sprint round; skipping")
            return
        if sq.status != SessionStatus.COMPLETED or sr.status != SessionStatus.COMPLETED:
            log.info("phase_scoring: SPRINT phase waiting for both sprint sessions to complete")
            return

    # Find every user who has any prediction for this round.
    user_ids = _user_ids_with_predictions(round_id)
    if not user_ids:
        log.info("phase_scoring: round %d has no predictions to score", round_id)
        return

    log.info("phase_scoring: round %d phase %s for %d users",
             round_id, phase.value, len(user_ids))

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
    ):
        rows = db.session.query(model.user_id).filter(model.round_id == round_id).distinct().all()
        user_id_sets.append({r[0] for r in rows})
    if not user_id_sets:
        return []
    return sorted(set().union(*user_id_sets))


def _load_user_predictions(user_id: int, round_id: int) -> UserPredictions:
    """Materialise all of a user's predictions for a round into a UserPredictions."""
    return UserPredictions(
        top10=db.session.query(Top10Prediction).filter_by(user_id=user_id, round_id=round_id).all(),
        quali_top3=db.session.query(Top3QualiPrediction).filter_by(user_id=user_id, round_id=round_id).all(),
        sprint_top3=db.session.query(Top3SprintPrediction).filter_by(user_id=user_id, round_id=round_id).all(),
        pole_time=db.session.query(PoleTimePrediction).filter_by(user_id=user_id, round_id=round_id).one_or_none(),
        fastest_lap=db.session.query(FastestLapPrediction).filter_by(user_id=user_id, round_id=round_id).one_or_none(),
        dnf_count=db.session.query(DnfCountPrediction).filter_by(user_id=user_id, round_id=round_id).one_or_none(),
        places_gained=db.session.query(PlacesGainedPrediction).filter_by(user_id=user_id, round_id=round_id).one_or_none(),
        quali_random_driver=db.session.query(QualiRandomDriverPrediction).filter_by(user_id=user_id, round_id=round_id).one_or_none(),
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
