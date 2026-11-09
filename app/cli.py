"""CLI commands for one-off maintenance tasks."""
from __future__ import annotations

import logging

import click
from flask import current_app
from flask.cli import with_appcontext

from app.api.exceptions import JolpicaError, JolpicaNotFoundError
from app.api.jolpica import build_default_client
from app.extensions import db
from app.models.driver import Driver
from app.models.round import Round, SessionStatus, SessionType
from worker.ingest import (
    _assign_round_selections,
    ingest_pit_stops,
    ingest_race_results,
    upsert_special_outcomes,
)

log = logging.getLogger(__name__)


def _drivers_by_ref_for_round(round_obj) -> dict[str, Driver]:
    """Fetch master Driver rows for everyone in the round's lineup + results."""
    refs: set[str] = set()
    for rd in round_obj.round_drivers:
        if rd.expected_driver is not None:
            refs.add(rd.expected_driver.driver_ref)
    for s in round_obj.sessions:
        for r in s.results:
            if r.actual_driver is not None:
                refs.add(r.actual_driver.driver_ref)
    if not refs:
        return {}
    rows = db.session.query(Driver).filter(Driver.driver_ref.in_(refs)).all()
    return {d.driver_ref: d for d in rows}


@click.command("backfill-phase4")
@click.option("--season", type=int, default=None,
              help="Season to backfill (defaults to F1_SEASON).")
@with_appcontext
def backfill_phase4(season):
    """Backfill Phase 4 selections and outcomes.

    Two operations, both idempotent:

      1. For every round with a populated lineup but missing round-level
         selections (qh2h / quali_nth / specials), populate the missing
         ones. Existing selections are preserved.

      2. For every round whose race session is COMPLETED, fetch pit-stops
         from Jolpica and (re)compute the SpecialOutcome rows.
    """
    season = season or current_app.config["F1_SEASON"]
    client = build_default_client(current_app.config)

    rounds = (
        db.session.query(Round)
        .filter(Round.season == season)
        .order_by(Round.round_number.asc())
        .all()
    )
    click.echo(f"Backfilling {len(rounds)} round(s) in season {season}.\n")

    selections_set = 0
    outcomes_done = 0

    for rd in rounds:
        # 1. Selections
        before = (rd.qh2h_driver_a_id, rd.quali_nth_position, rd.special_a_key)
        _assign_round_selections(db.session, rd)
        db.session.commit()
        after = (rd.qh2h_driver_a_id, rd.quali_nth_position, rd.special_a_key)
        if before != after:
            selections_set += 1
            click.echo(
                f"  round {rd.round_number}: selections set "
                f"(h2h={rd.qh2h_driver_a_id}/{rd.qh2h_driver_b_id} "
                f"qnth={rd.quali_nth_position} "
                f"specials={rd.special_a_key}, {rd.special_b_key})"
            )

        # 2. Outcomes — only for rounds whose race is complete.
        race_session = next(
            (s for s in rd.sessions if s.session_type == SessionType.RACE),
            None,
        )
        if race_session is None or race_session.status != SessionStatus.COMPLETED:
            continue

        drivers_by_ref = _drivers_by_ref_for_round(rd)

        # Re-fetch race results so laps_completed + race_time_ms (added
        # in later schema versions) get populated on past rounds.
        try:
            api_results = client.get_race_results(rd.season, rd.round_number)
            refs = {e.driver_ref for e in api_results}
            results_drivers = {
                d.driver_ref: d for d in
                db.session.query(Driver).filter(Driver.driver_ref.in_(refs)).all()
            }
            ingest_race_results(
                db.session, race_session, api_results, results_drivers,
            )
            db.session.commit()
            click.echo(f"  round {rd.round_number}: race results refreshed")
            # Refresh local handle to include the new driver refs.
            drivers_by_ref = _drivers_by_ref_for_round(rd)
        except JolpicaError as exc:
            click.echo(
                f"  round {rd.round_number}: race results re-fetch failed: {exc}",
                err=True,
            )

        try:
            api_stops = client.get_pit_stops(rd.season, rd.round_number)
            ingest_pit_stops(db.session, rd, api_stops, drivers_by_ref)
        except JolpicaNotFoundError:
            click.echo(f"  round {rd.round_number}: pit-stops not available")
        except JolpicaError as exc:
            click.echo(
                f"  round {rd.round_number}: pit-stops fetch failed: {exc}",
                err=True,
            )

        upsert_special_outcomes(
            db.session, rd,
            race_results=list(race_session.results),
            pit_stops=list(rd.pit_stops),
        )
        db.session.commit()
        outcomes_done += 1
        click.echo(
            f"  round {rd.round_number}: outcomes computed "
            f"({rd.special_a_key}, {rd.special_b_key})"
        )

    click.echo(
        f"\nDone. Selections set on {selections_set} round(s). "
        f"Outcomes computed for {outcomes_done} round(s)."
    )

@click.command("seed-test-predictions")
@click.option("--user-id", type=int, required=True)
@click.option("--season", type=int, default=None)
@click.option("--round", "round_number", type=int, required=True)
@with_appcontext
def seed_test_predictions(user_id, season, round_number):
    """Insert plausible test predictions for a user and round, then score.

    DEV USE ONLY. Wipes any existing predictions for this (user, round)
    pair before inserting. Re-runs the relevant scoring phases.
    """
    import random
    from app.models.driver import RoundDriver
    from app.models.prediction import (
        DnfCountPrediction, FastestLapPrediction, PlacesGainedPrediction,
        PoleTimePrediction, QualiHeadToHeadPrediction, QualiNthPrediction,
        QualiRandomDriverPrediction, SpecialPrediction,
        Top3QualiPrediction, Top3SprintPrediction, Top10Prediction,
    )
    from app.models.round import Round, ScoringPhase, SessionStatus, SessionType
    from app.specials import SPECIALS_BY_KEY
    from worker.jobs import phase_scoring_job

    if current_app.config.get("FLASK_ENV", "").lower() == "production":
        click.echo("This command is dev-only. Aborted.", err=True)
        return

    season = season or current_app.config["F1_SEASON"]
    rd = db.session.query(Round).filter_by(
        season=season, round_number=round_number,
    ).one_or_none()
    if rd is None:
        click.echo(f"Round {season}/{round_number} not found.", err=True)
        return

    round_drivers = list(rd.round_drivers)
    if not round_drivers:
        click.echo("Round has no lineup yet — can't seed.", err=True)
        return

    # Wipe any existing predictions for this user × round.
    for model in (
        Top10Prediction, Top3QualiPrediction, Top3SprintPrediction,
        PoleTimePrediction, FastestLapPrediction, DnfCountPrediction,
        PlacesGainedPrediction, QualiRandomDriverPrediction,
        QualiHeadToHeadPrediction, QualiNthPrediction, SpecialPrediction,
    ):
        db.session.query(model).filter_by(
            user_id=user_id, round_id=rd.id,
        ).delete()
    db.session.flush()

    # Pick a stable seed off (user_id, round_id) so re-runs produce the
    # same predictions — easier to reason about scoring drift.
    rng = random.Random(f"{user_id}-{rd.id}")
    shuffled = list(round_drivers)
    rng.shuffle(shuffled)
    pick = lambda i: shuffled[i % len(shuffled)].expected_driver_id

    # Race top 10 — shuffled lineup, first 10 picks
    for pos in range(1, 11):
        db.session.add(Top10Prediction(
            user_id=user_id, round_id=rd.id, position=pos,
            predicted_driver_id=pick(pos - 1),
        ))

    # Quali top 3 — different shuffle
    rng2 = random.Random(f"{user_id}-{rd.id}-quali")
    qshuffled = list(round_drivers)
    rng2.shuffle(qshuffled)
    for pos in range(1, 4):
        db.session.add(Top3QualiPrediction(
            user_id=user_id, round_id=rd.id, position=pos,
            predicted_driver_id=qshuffled[pos - 1].expected_driver_id,
        ))

    # Sprint top 3 — only if sprint weekend
    if rd.weekend_type.value == "sprint":
        for pos in range(1, 4):
            db.session.add(Top3SprintPrediction(
                user_id=user_id, round_id=rd.id, position=pos,
                predicted_driver_id=pick(10 + pos),
            ))

    # Pole time — random in 1:20.000 to 1:35.000 range
    db.session.add(PoleTimePrediction(
        user_id=user_id, round_id=rd.id,
        predicted_time_ms=rng.randint(80_000, 95_000),
    ))

    # Fastest lap
    db.session.add(FastestLapPrediction(
        user_id=user_id, round_id=rd.id,
        predicted_driver_id=pick(0),
    ))

    # DNF count
    db.session.add(DnfCountPrediction(
        user_id=user_id, round_id=rd.id,
        predicted_count=rng.randint(0, 6),
    ))

    # Places gained
    db.session.add(PlacesGainedPrediction(
        user_id=user_id, round_id=rd.id,
        predicted_driver_id=pick(5),
    ))

    # Random-driver quali
    if rd.random_quali_driver_id is not None:
        db.session.add(QualiRandomDriverPrediction(
            user_id=user_id, round_id=rd.id,
            predicted_position=rng.randint(1, 20),
        ))

    # H2H
    if rd.qh2h_driver_a and rd.qh2h_driver_b:
        choice = rng.choice([rd.qh2h_driver_a, rd.qh2h_driver_b])
        db.session.add(QualiHeadToHeadPrediction(
            user_id=user_id, round_id=rd.id,
            predicted_driver_id=choice.expected_driver_id,
        ))

    # Quali Nth
    if rd.quali_nth_position is not None:
        db.session.add(QualiNthPrediction(
            user_id=user_id, round_id=rd.id,
            predicted_driver_id=pick(7),
        ))

    # Specials
    teams = sorted({rd_.constructor_name for rd_ in round_drivers if rd_.constructor_name})
    for key in (rd.special_a_key, rd.special_b_key):
        if not key:
            continue
        sp = SPECIALS_BY_KEY[key]
        pred = SpecialPrediction(
            user_id=user_id, round_id=rd.id, special_key=key,
        )
        if sp.input_type == "driver_pick":
            pred.predicted_driver_id = pick(3)
        elif sp.input_type == "int":
            pred.predicted_int = rng.randint(1, 50)
        elif sp.input_type == "bool":
            pred.predicted_bool = rng.choice([True, False])
        elif sp.input_type == "team_pick":
            pred.predicted_team_name = rng.choice(teams) if teams else None
        db.session.add(pred)

    db.session.commit()
    click.echo(f"Seeded predictions for user {user_id}, round {round_number}.")

    # Trigger scoring for whichever phases have completed sessions.
    sessions_by_type = {s.session_type: s for s in rd.sessions}
    for session_type, phase in (
        (SessionType.SPRINT_RACE, ScoringPhase.SPRINT),
        (SessionType.QUALIFYING, ScoringPhase.QUALI),
        (SessionType.RACE, ScoringPhase.RACE),
    ):
        s = sessions_by_type.get(session_type)
        if s and s.status == SessionStatus.COMPLETED:
            phase_scoring_job(current_app._get_current_object(), rd.id, phase)
            click.echo(f"  re-scored phase {phase.value}")
