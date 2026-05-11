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
