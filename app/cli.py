"""CLI commands for one-off maintenance tasks."""
from __future__ import annotations

import logging

import click
from flask import current_app
from flask.cli import with_appcontext
from sqlalchemy import func

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


# =============================================================================
# repair-seats — one-off fix for lineups rewritten from results
# =============================================================================


@click.command("repair-seats")
@click.option("--season", type=int, default=None, help="Defaults to F1_SEASON.")
@click.option("--reference-round", type=int, default=None,
              help="Round whose lineup is the true one (default: first round with no results).")
@click.option("--refetch/--no-refetch", default=True,
              help="Re-fetch results from Jolpica (fills team names and missing debut rows).")
@click.option("--apply", "apply_", is_flag=True,
              help="Commit. Without it everything runs in a transaction that is rolled back.")
@with_appcontext
def repair_seats(season, reference_round, refetch, apply_):
    """Restore each played round's lineup to what the form showed, re-fetch
    results, recompute specials and re-score, then report every user's
    point change per round.

    Before seat scoring, the worker rewrote a round's lineup from its first
    results: stand-ins were added as extra seats and promoted drivers had
    their seat's team overwritten. The lineup of a round that has no
    results yet was never touched, so it is the reference: seats whose
    driver isn't in it are removed, and teams are restored from it.

    Run this before using the lineup editor on any round, since an edited
    round can't serve as the reference.
    """
    from app.models.prediction import PredictionScore
    from app.models.round import Session as RoundSession
    from app.models.user import User
    from app.seats import substitutions
    from worker.ingest import session_triggers_phase
    from worker.jobs import _fetch_and_ingest_session, _phase_scoring_inner

    season = season or current_app.config["F1_SEASON"]
    rounds = (
        db.session.query(Round).filter(Round.season == season)
        .order_by(Round.round_number).all()
    )
    played_types = (SessionType.QUALIFYING, SessionType.SPRINT_RACE, SessionType.RACE)

    def played(rd):
        return [s for s in rd.sessions if s.session_type in played_types and s.results]

    if reference_round is None:
        ref = next((r for r in rounds if r.round_drivers and not played(r)), None)
    else:
        ref = next((r for r in rounds if r.round_number == reference_round), None)
    if ref is None or not ref.round_drivers:
        click.echo("No reference round with a lineup found.", err=True)
        return

    def code(driver):
        return (driver.code or driver.driver_ref[:3].upper()) if driver else "?"

    ref_team = {s.expected_driver_id: s.constructor_name for s in ref.round_drivers}
    click.echo(f"Season {season}. Reference lineup: R{ref.round_number}")
    for s in sorted(ref.round_drivers, key=lambda s: (s.constructor_name or "", s.car_number)):
        click.echo(f"  {s.constructor_name}: {code(s.expected_driver)}")
    click.echo("")

    def totals():
        rows = (
            db.session.query(Round.round_number, PredictionScore.user_id,
                             func.coalesce(func.sum(PredictionScore.points), 0))
            .join(Round, Round.id == PredictionScore.round_id)
            .filter(Round.season == season)
            .group_by(Round.round_number, PredictionScore.user_id)
            .all()
        )
        return {(n, uid): int(p) for n, uid, p in rows}

    before = totals()
    client = build_default_client(current_app.config) if refetch else None
    round_ids = [r.id for r in rounds if played(r)]

    for rid in round_ids:
        rd = db.session.get(Round, rid)
        lines = []

        # 1. Lineup back to what the form showed.
        protected = {rd.random_quali_driver_id, rd.qh2h_driver_a_id, rd.qh2h_driver_b_id}
        for seat in list(rd.round_drivers):
            label = f"{code(seat.expected_driver)} ({seat.constructor_name})"
            if seat.expected_driver_id not in ref_team:
                if seat.id in protected:
                    lines.append(f"KEEP {label}: used by a round selection, check by hand")
                    continue
                if _round_has_picks_of(rd.id, seat.expected_driver_id):
                    lines.append(f"KEEP {label}: has picks, check by hand")
                    continue
                lines.append(f"remove seat {label}")
                db.session.delete(seat)
            elif seat.constructor_name != ref_team[seat.expected_driver_id]:
                lines.append(f"team {label} -> {ref_team[seat.expected_driver_id]}")
                seat.constructor_name = ref_team[seat.expected_driver_id]
        db.session.flush()
        db.session.expire(rd, ["round_drivers"])

        # 2. Re-fetch results (team names, debut rows), keeping statuses.
        if client is not None:
            for s in played(rd):
                status, fetched = s.status, s.results_fetched_at
                try:
                    _fetch_and_ingest_session(client, rd, s, gate=False)
                except JolpicaError as exc:
                    lines.append(f"{s.session_type.value}: re-fetch failed ({exc}); kept old rows")
                s.status, s.results_fetched_at = status, fetched
            db.session.flush()

        # 3. Specials, then every scored phase, from fresh state.
        db.session.expire_all()
        rd = db.session.get(Round, rid)
        race = next((s for s in rd.sessions if s.session_type == SessionType.RACE), None)
        if race is not None and race.results:
            from app.models.pitstop import PitStop
            from app.models.result import SessionResult
            upsert_special_outcomes(
                db.session, rd,
                race_results=db.session.query(SessionResult).filter_by(session_id=race.id)
                .order_by(SessionResult.position).all(),
                pit_stops=db.session.query(PitStop).filter_by(round_id=rd.id).all(),
            )
            db.session.flush()
            db.session.expire_all()
        session_types = [
            s.session_type for s in
            db.session.query(RoundSession).filter_by(round_id=rid).all()
            if s.status in (SessionStatus.COMPLETED, SessionStatus.PROVISIONAL)
        ]
        for st in session_types:
            phase = session_triggers_phase(st)
            if phase is not None:
                _phase_scoring_inner(rid, phase, commit=False)
        db.session.expire_all()

        rd = db.session.get(Round, rid)
        for s in played(rd):
            for seat, r in substitutions(rd.round_drivers, s.results):
                lines.append(
                    f"{s.session_type.value}: {code(r.actual_driver)} drove "
                    f"{code(seat.expected_driver)}'s seat ({seat.constructor_name})"
                )
        if lines:
            click.echo(f"R{rd.round_number}")
            for line in lines:
                click.echo(f"  {line}")

    after = totals()
    names = {u.id: u.username for u in db.session.query(User).all()}
    changes = sorted(
        (n, uid, before.get((n, uid), 0), after.get((n, uid), 0))
        for (n, uid) in set(before) | set(after)
        if before.get((n, uid), 0) != after.get((n, uid), 0)
    )
    click.echo("\nPoint changes")
    if not changes:
        click.echo("  none")
    season_delta: dict[int, int] = {}
    for n, uid, b, a in changes:
        click.echo(f"  R{n:<3} {names.get(uid, uid):<20} {b:>4} -> {a:<4} ({a - b:+d})")
        season_delta[uid] = season_delta.get(uid, 0) + a - b
    if season_delta:
        click.echo("\nSeason totals")
        for uid, delta in sorted(season_delta.items(), key=lambda kv: -kv[1]):
            click.echo(f"  {names.get(uid, uid):<20} {delta:+d}")

    if apply_:
        db.session.commit()
        click.echo("\nApplied.")
    else:
        db.session.rollback()
        click.echo("\nDry run: rolled back. Re-run with --apply to commit.")


def _round_has_picks_of(round_id: int, driver_id: int) -> bool:
    from app.lineups import PICK_MODELS

    return any(
        db.session.query(m.id).filter(m.round_id == round_id, m.predicted_driver_id == driver_id).first()
        for m in PICK_MODELS
    )
