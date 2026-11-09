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


def _parse_rounds(raw: str | None) -> set[int] | None:
    if not raw:
        return None
    try:
        return {int(x) for x in raw.split(",") if x.strip()}
    except ValueError:
        raise click.BadParameter("comma-separated round numbers, e.g. 12,13,14")


@click.command("repair-seats")
@click.option("--season", type=int, default=None, help="Defaults to F1_SEASON.")
@click.option("--rounds", "rounds_raw", default=None,
              help="Only these round numbers, e.g. 12,13,14,19,20. Default: every played round.")
@click.option("--reference-round", type=int, default=None,
              help="Round whose lineup is the true one (default: first round with no results).")
@click.option("--refetch/--no-refetch", default=True,
              help="Re-fetch results from Jolpica (fills team names and missing debut rows).")
@click.option("--apply", "apply_", is_flag=True,
              help="Commit. Without it everything runs in a transaction that is rolled back.")
@with_appcontext
def repair_seats(season, rounds_raw, reference_round, refetch, apply_):
    """Restore each played round's lineup to what the form showed, re-fetch
    results, recompute specials and re-score, then report what changed:
    result rows, pit stops, special outcomes, and points per user and per
    prediction type.

    Before seat scoring, the worker rewrote a round's lineup from its first
    results: stand-ins were added as extra seats and promoted drivers had
    their seat's team overwritten. The lineup of a round that has no
    results yet was never touched, so it is the reference: seats whose
    driver isn't in it are removed, and teams are restored from it.

    Run this before using the lineup editor on any round, since an edited
    round can't serve as the reference.
    """
    from app.models.pitstop import PitStop
    from app.models.prediction import PredictionScore
    from app.models.result import SessionResult
    from app.models.round import Session as RoundSession
    from app.models.special import SpecialOutcome
    from app.models.user import User
    from app.seats import substitutions
    from worker.ingest import session_triggers_phase
    from worker.jobs import _fetch_and_ingest_session, _phase_scoring_inner

    season = season or current_app.config["F1_SEASON"]
    only = _parse_rounds(rounds_raw)
    rounds = (
        db.session.query(Round).filter(Round.season == season)
        .order_by(Round.round_number).all()
    )
    played_types = (SessionType.QUALIFYING, SessionType.SPRINT_RACE, SessionType.RACE)
    drivers = {d.id: d for d in db.session.query(Driver).all()}

    def code(driver_id):
        d = drivers.get(driver_id) or (db.session.get(Driver, driver_id) if driver_id else None)
        return (d.code or d.driver_ref[:3].upper()) if d else "?"

    def played_sessions(round_id):
        # Queried, not read from relationship collections, which can still
        # hold rows a re-fetch has just deleted.
        out = []
        for s in (
            db.session.query(RoundSession).filter_by(round_id=round_id).all()
        ):
            if s.session_type not in played_types:
                continue
            rows = db.session.query(SessionResult).filter_by(session_id=s.id).all()
            if rows:
                out.append((s, rows))
        order = {st: i for i, st in enumerate(played_types)}
        return sorted(out, key=lambda sr: order[sr[0].session_type])

    def has_results(rd):
        return any(
            s.session_type in played_types and s.results for s in rd.sessions
        )

    if reference_round is None:
        ref = next((r for r in rounds if r.round_drivers and not has_results(r)), None)
    else:
        ref = next((r for r in rounds if r.round_number == reference_round), None)
    if ref is None or not ref.round_drivers:
        click.echo("No reference round with a lineup found.", err=True)
        return

    ref_team = {s.expected_driver_id: s.constructor_name for s in ref.round_drivers}
    click.echo(f"Season {season}. Reference lineup: R{ref.round_number}")
    for s in sorted(ref.round_drivers, key=lambda s: (s.constructor_name or "", s.car_number)):
        click.echo(f"  {s.constructor_name}: {code(s.expected_driver_id)}")
    click.echo("")

    def totals(by):
        rows = (
            db.session.query(Round.round_number, by,
                             func.coalesce(func.sum(PredictionScore.points), 0))
            .join(Round, Round.id == PredictionScore.round_id)
            .filter(Round.season == season)
            .group_by(Round.round_number, by)
            .all()
        )
        return {(n, k): int(p) for n, k, p in rows}

    def snap_results(round_id):
        out = {}
        for s, rows in played_sessions(round_id):
            out[s.session_type] = (
                {r.position: (r.actual_driver_id, r.status, r.grid_position, r.is_fastest_lap,
                              r.laps_completed, r.race_time_ms, r.best_qualifying_time_ms)
                 for r in rows},
                s.pole_time_ms, s.dnf_count,
            )
        return out

    def snap_specials(round_id):
        return {
            o.special_key: (o.no_result, o.actual_driver_id, o.actual_int,
                            o.actual_bool, o.actual_team_name)
            for o in db.session.query(SpecialOutcome).filter_by(round_id=round_id).all()
        }

    fields = ("driver", "status", "grid", "fastest lap", "laps", "race time", "quali time")

    def diff_results(before, after):
        lines = []
        for st in played_types:
            if st not in before and st not in after:
                continue
            b_rows, b_pole, b_dnf = before.get(st, ({}, None, None))
            a_rows, a_pole, a_dnf = after.get(st, ({}, None, None))
            notes = []
            for pos in sorted(set(b_rows) | set(a_rows)):
                b, a = b_rows.get(pos), a_rows.get(pos)
                if b == a:
                    continue
                if b is None:
                    notes.append(f"P{pos} new {code(a[0])}")
                elif a is None:
                    notes.append(f"P{pos} gone {code(b[0])}")
                elif b[0] != a[0]:
                    notes.append(f"P{pos} {code(b[0])}->{code(a[0])}")
                else:
                    changed = [f for f, x, y in zip(fields, b, a) if x != y]
                    notes.append(f"P{pos} {code(a[0])} {'/'.join(changed)}")
            if b_pole != a_pole:
                notes.append(f"pole time {b_pole}->{a_pole}")
            if b_dnf != a_dnf:
                notes.append(f"dnf count {b_dnf}->{a_dnf}")
            if notes:
                shown = ", ".join(notes[:10]) + (f", +{len(notes) - 10} more" if len(notes) > 10 else "")
                lines.append(f"{st.value} data changed: {shown}")
        return lines

    def special_value(v):
        no_result, driver_id, as_int, as_bool, team = v
        if no_result:
            return "no result"
        if team and all(p.isdigit() for p in team.split(",")):
            return "/".join(code(int(p)) for p in team.split(","))
        parts = [x for x in (
            code(driver_id) if driver_id else None, team,
            str(as_int) if as_int is not None else None,
            str(as_bool) if as_bool is not None else None,
        ) if x]
        return " ".join(parts) or "-"

    before_user = totals(PredictionScore.user_id)
    before_kind = totals(PredictionScore.kind)
    client = build_default_client(current_app.config) if refetch else None
    round_ids = [
        r.id for r in rounds
        if has_results(r) and (only is None or r.round_number in only)
    ]

    for rid in round_ids:
        rd = db.session.get(Round, rid)
        lines = []
        results_before = snap_results(rid)
        specials_before = snap_specials(rid)
        stops_before = db.session.query(PitStop).filter_by(round_id=rid).count()

        # 1. Lineup back to what the form showed.
        protected = {rd.random_quali_driver_id, rd.qh2h_driver_a_id, rd.qh2h_driver_b_id}
        for seat in list(rd.round_drivers):
            label = f"{code(seat.expected_driver_id)} ({seat.constructor_name})"
            if seat.expected_driver_id not in ref_team:
                if seat.id in protected:
                    lines.append(f"KEEP seat {label}: used by a round selection, check by hand")
                    continue
                if _round_has_picks_of(rid, seat.expected_driver_id):
                    lines.append(f"KEEP seat {label}: has picks, check by hand")
                    continue
                lines.append(f"remove seat {label}")
                db.session.delete(seat)
            elif seat.constructor_name != ref_team[seat.expected_driver_id]:
                lines.append(f"seat team {label} -> {ref_team[seat.expected_driver_id]}")
                seat.constructor_name = ref_team[seat.expected_driver_id]
        db.session.flush()
        db.session.expire_all()

        # 2. Re-fetch results (team names, debut rows), keeping statuses.
        if client is not None:
            rd = db.session.get(Round, rid)
            for s, _rows in played_sessions(rid):
                status, fetched = s.status, s.results_fetched_at
                try:
                    _fetch_and_ingest_session(client, rd, s, gate=False)
                except JolpicaError as exc:
                    lines.append(f"{s.session_type.value}: re-fetch refused ({exc}); kept stored rows")
                s.status, s.results_fetched_at = status, fetched
            db.session.flush()
            db.session.expire_all()

        # Stored rows a re-fetch couldn't replace still lack a team: take it
        # from the round's other sessions, same car and same driver.
        sessions = played_sessions(rid)
        known = {
            (r.car_number, r.actual_driver_id): r.constructor_name
            for _s, rows in sessions for r in rows if r.constructor_name
        }
        for s, rows in sessions:
            filled = 0
            for r in rows:
                if not r.constructor_name and (r.car_number, r.actual_driver_id) in known:
                    r.constructor_name = known[(r.car_number, r.actual_driver_id)]
                    filled += 1
            if filled:
                lines.append(f"{s.session_type.value}: team filled on {filled} stored row(s) from other sessions")
        db.session.flush()
        db.session.expire_all()

        # 3. Specials, then every scored phase, from fresh state.
        rd = db.session.get(Round, rid)
        race = next((s for s, _ in played_sessions(rid) if s.session_type == SessionType.RACE), None)
        if race is not None:
            upsert_special_outcomes(
                db.session, rd,
                race_results=db.session.query(SessionResult).filter_by(session_id=race.id)
                .order_by(SessionResult.position).all(),
                pit_stops=db.session.query(PitStop).filter_by(round_id=rid).all(),
            )
            db.session.flush()
            db.session.expire_all()
        for s in db.session.query(RoundSession).filter_by(round_id=rid).all():
            if s.status in (SessionStatus.COMPLETED, SessionStatus.PROVISIONAL):
                phase = session_triggers_phase(s.session_type)
                if phase is not None:
                    _phase_scoring_inner(rid, phase, commit=False)
        db.session.expire_all()

        # 4. Report.
        lines += diff_results(results_before, snap_results(rid))
        stops_after = db.session.query(PitStop).filter_by(round_id=rid).count()
        if stops_after != stops_before:
            lines.append(f"pit stops {stops_before} -> {stops_after}")
        specials_after = snap_specials(rid)
        for key in sorted(set(specials_before) | set(specials_after)):
            b, a = specials_before.get(key), specials_after.get(key)
            if b != a:
                lines.append(
                    f"special {key}: {special_value(b) if b else 'none'} -> "
                    f"{special_value(a) if a else 'none'}"
                )
        rd = db.session.get(Round, rid)
        for s, rows in played_sessions(rid):
            for seat, r in substitutions(rd.round_drivers, rows):
                lines.append(
                    f"{s.session_type.value}: {code(r.actual_driver_id)} drove "
                    f"{code(seat.expected_driver_id)}'s seat ({seat.constructor_name})"
                )
        if lines:
            click.echo(f"R{rd.round_number}")
            for line in lines:
                click.echo(f"  {line}")

    after_user = totals(PredictionScore.user_id)
    after_kind = totals(PredictionScore.kind)
    names = {u.id: u.username for u in db.session.query(User).all()}

    def changes(before, after):
        return sorted(
            (n, k, before.get((n, k), 0), after.get((n, k), 0))
            for (n, k) in set(before) | set(after)
            if before.get((n, k), 0) != after.get((n, k), 0)
        )

    click.echo("\nPoint changes by prediction type (all users)")
    kind_changes = changes(before_kind, after_kind)
    if not kind_changes:
        click.echo("  none")
    for n, kind, b, a in kind_changes:
        label = kind.value if hasattr(kind, "value") else kind
        click.echo(f"  R{n:<3} {label:<20} {b:>5} -> {a:<5} ({a - b:+d})")

    click.echo("\nPoint changes by user")
    user_changes = changes(before_user, after_user)
    if not user_changes:
        click.echo("  none")
    season_delta: dict[int, int] = {}
    for n, uid, b, a in user_changes:
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
