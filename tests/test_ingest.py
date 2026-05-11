"""Tests for worker.ingest functions.

Uses the real Postgres test database via the `db` fixture in conftest.py.
"""
from __future__ import annotations

import pytest

from app.api.jolpica import APIPitStop
from app.models.driver import Driver, RoundDriver
from app.models.pitstop import PitStop
from app.models.round import Round, WeekendType
from app.specials import SPECIAL_KEYS
from worker.ingest import ingest_pit_stops, _assign_round_selections


# =============================================================================
# Helpers
# =============================================================================


def _make_round(db, season=2026, round_number=1, gp_name="Test GP"):
    rd = Round(
        season=season,
        round_number=round_number,
        gp_name=gp_name,
        weekend_type=WeekendType.STANDARD,
    )
    db.session.add(rd)
    db.session.commit()
    return rd


def _make_driver(db, ref, given="Test", family="Driver", code=None):
    d = Driver(
        driver_ref=ref,
        given_name=given,
        family_name=family,
        code=code or ref[:3].upper(),
    )
    db.session.add(d)
    db.session.commit()
    return d


# =============================================================================
# ingest_pit_stops
# =============================================================================


def test_ingest_pit_stops_writes_rows(db):
    rd = _make_round(db)
    ham = _make_driver(db, "hamilton", family="Hamilton")
    ver = _make_driver(db, "max_verstappen", family="Verstappen")
    drivers_by_ref = {"hamilton": ham, "max_verstappen": ver}

    api_stops = [
        APIPitStop(driver_ref="hamilton", lap=12, stop_number=1, duration_ms=23_456),
        APIPitStop(driver_ref="hamilton", lap=35, stop_number=2, duration_ms=22_111),
        APIPitStop(driver_ref="max_verstappen", lap=14, stop_number=1, duration_ms=24_000),
    ]

    written = ingest_pit_stops(db.session, rd, api_stops, drivers_by_ref)
    db.session.commit()

    assert written == 3
    rows = db.session.query(PitStop).filter_by(round_id=rd.id).order_by(
        PitStop.driver_id, PitStop.stop_number,
    ).all()
    assert len(rows) == 3
    assert rows[0].driver_id == ham.id
    assert rows[0].lap == 12
    assert rows[0].stop_number == 1
    assert rows[0].duration_ms == 23_456
    assert rows[2].driver_id == ver.id
    assert rows[2].duration_ms == 24_000


def test_ingest_pit_stops_is_idempotent(db):
    """Re-running with identical data yields the same end state."""
    rd = _make_round(db)
    ham = _make_driver(db, "hamilton", family="Hamilton")
    drivers_by_ref = {"hamilton": ham}

    api_stops = [
        APIPitStop(driver_ref="hamilton", lap=12, stop_number=1, duration_ms=23_456),
    ]

    ingest_pit_stops(db.session, rd, api_stops, drivers_by_ref)
    db.session.commit()
    ingest_pit_stops(db.session, rd, api_stops, drivers_by_ref)
    db.session.commit()

    rows = db.session.query(PitStop).filter_by(round_id=rd.id).all()
    assert len(rows) == 1


def test_ingest_pit_stops_replaces_existing(db):
    """Wholesale replace: a second call with different data wipes the first."""
    rd = _make_round(db)
    ham = _make_driver(db, "hamilton", family="Hamilton")
    drivers_by_ref = {"hamilton": ham}

    first_pass = [
        APIPitStop(driver_ref="hamilton", lap=12, stop_number=1, duration_ms=23_000),
        APIPitStop(driver_ref="hamilton", lap=35, stop_number=2, duration_ms=22_000),
    ]
    ingest_pit_stops(db.session, rd, first_pass, drivers_by_ref)
    db.session.commit()
    assert db.session.query(PitStop).filter_by(round_id=rd.id).count() == 2

    # Corrected data from a re-fetch — only one stop now.
    second_pass = [
        APIPitStop(driver_ref="hamilton", lap=14, stop_number=1, duration_ms=24_500),
    ]
    ingest_pit_stops(db.session, rd, second_pass, drivers_by_ref)
    db.session.commit()

    rows = db.session.query(PitStop).filter_by(round_id=rd.id).all()
    assert len(rows) == 1
    assert rows[0].lap == 14
    assert rows[0].duration_ms == 24_500


def test_ingest_pit_stops_skips_unknown_driver(db, caplog):
    """A pit-stop referencing a driver not in the master table is skipped."""
    rd = _make_round(db)
    ham = _make_driver(db, "hamilton", family="Hamilton")
    drivers_by_ref = {"hamilton": ham}   # no entry for 'mystery_driver'

    api_stops = [
        APIPitStop(driver_ref="hamilton", lap=12, stop_number=1, duration_ms=23_000),
        APIPitStop(driver_ref="mystery_driver", lap=15, stop_number=1, duration_ms=24_000),
    ]

    with caplog.at_level("WARNING"):
        written = ingest_pit_stops(db.session, rd, api_stops, drivers_by_ref)
    db.session.commit()

    assert written == 1
    rows = db.session.query(PitStop).filter_by(round_id=rd.id).all()
    assert len(rows) == 1
    assert rows[0].driver_id == ham.id
    assert any("mystery_driver" in r.message for r in caplog.records)


def test_ingest_pit_stops_handles_missing_duration(db):
    """duration_ms is nullable — Jolpica sometimes omits it."""
    rd = _make_round(db)
    ham = _make_driver(db, "hamilton", family="Hamilton")
    drivers_by_ref = {"hamilton": ham}

    api_stops = [
        APIPitStop(driver_ref="hamilton", lap=12, stop_number=1, duration_ms=None),
    ]
    ingest_pit_stops(db.session, rd, api_stops, drivers_by_ref)
    db.session.commit()

    row = db.session.query(PitStop).filter_by(round_id=rd.id).one()
    assert row.duration_ms is None


def test_ingest_pit_stops_empty_input(db):
    """Empty API response writes nothing and doesn't error."""
    rd = _make_round(db)
    written = ingest_pit_stops(db.session, rd, [], {})
    db.session.commit()
    assert written == 0
    assert db.session.query(PitStop).filter_by(round_id=rd.id).count() == 0

# =============================================================================
# Round-level random selections
# =============================================================================


def _seed_round_with_lineup(db, team_rosters: dict[str, list[tuple]]):
    """team_rosters: {team_name: [(driver_ref, family, car_number), ...]}"""
    rd = Round(
        season=2026, round_number=1, gp_name="Test GP",
        weekend_type=WeekendType.STANDARD,
    )
    db.session.add(rd)
    db.session.flush()
    for team, roster in team_rosters.items():
        for driver_ref, family, car_number in roster:
            d = _make_driver(db, driver_ref, family=family)
            db.session.add(RoundDriver(
                round_id=rd.id,
                car_number=car_number,
                expected_driver_id=d.id,
                constructor_name=team,
            ))
    db.session.commit()
    return rd


def _standard_grid():
    """10 teams × 2 drivers = 20-car grid."""
    return {
        f"Team{i}": [
            (f"d{i}a", f"DriverA{i}", i * 2 + 1),
            (f"d{i}b", f"DriverB{i}", i * 2 + 2),
        ]
        for i in range(10)
    }


def test_assign_round_selections_populates_all_four(db):
    rd = _seed_round_with_lineup(db, _standard_grid())
    _assign_round_selections(db.session, rd)
    db.session.commit()

    assert rd.random_quali_driver_id is not None
    assert rd.qh2h_driver_a_id is not None
    assert rd.qh2h_driver_b_id is not None
    assert rd.qh2h_driver_a_id != rd.qh2h_driver_b_id
    assert rd.quali_nth_position is not None
    assert 11 <= rd.quali_nth_position <= 20
    assert rd.special_a_key in SPECIAL_KEYS
    assert rd.special_b_key in SPECIAL_KEYS
    assert rd.special_a_key != rd.special_b_key


def test_h2h_picks_teammates(db):
    rd = _seed_round_with_lineup(db, _standard_grid())
    _assign_round_selections(db.session, rd)
    db.session.commit()
    a = db.session.get(RoundDriver, rd.qh2h_driver_a_id)
    b = db.session.get(RoundDriver, rd.qh2h_driver_b_id)
    assert a.constructor_name == b.constructor_name


def test_h2h_skips_team_with_three_drivers(db):
    """Substitution round: a team with 3 drivers entered must not be
    picked for head-to-head."""
    rosters = {
        "TeamA": [("a1", "A1", 1), ("a2", "A2", 2)],
        "TeamB": [("b1", "B1", 3), ("b2", "B2", 4)],
        # Team C has 3 drivers — reserve called up alongside the regulars
        "TeamC_subbed": [
            ("c1", "C1", 5), ("c2", "C2", 6), ("c3", "C3", 7),
        ],
    }
    rd = _seed_round_with_lineup(db, rosters)

    # Run repeatedly with fresh state so accidental selection of TeamC
    # would be statistically impossible to miss.
    for _ in range(50):
        rd.qh2h_driver_a_id = None
        rd.qh2h_driver_b_id = None
        db.session.commit()
        _assign_round_selections(db.session, rd)
        db.session.commit()
        a = db.session.get(RoundDriver, rd.qh2h_driver_a_id)
        b = db.session.get(RoundDriver, rd.qh2h_driver_b_id)
        assert a.constructor_name == b.constructor_name
        assert a.constructor_name != "TeamC_subbed"


def test_assign_round_selections_is_idempotent(db):
    rd = _seed_round_with_lineup(db, _standard_grid())
    _assign_round_selections(db.session, rd)
    db.session.commit()
    snapshot = (
        rd.random_quali_driver_id,
        rd.qh2h_driver_a_id, rd.qh2h_driver_b_id,
        rd.quali_nth_position,
        rd.special_a_key, rd.special_b_key,
    )
    _assign_round_selections(db.session, rd)
    db.session.commit()
    assert (
        rd.random_quali_driver_id,
        rd.qh2h_driver_a_id, rd.qh2h_driver_b_id,
        rd.quali_nth_position,
        rd.special_a_key, rd.special_b_key,
    ) == snapshot


def test_assign_round_selections_no_lineup_skips_lineup_dependent_picks(db):
    """With no RoundDriver rows, the lineup-dependent selections stay null
    but specials are still drawn (they don't depend on the lineup)."""
    rd = Round(
        season=2026, round_number=99, gp_name="Empty GP",
        weekend_type=WeekendType.STANDARD,
    )
    db.session.add(rd)
    db.session.commit()
    _assign_round_selections(db.session, rd)
    db.session.commit()
    assert rd.random_quali_driver_id is None
    assert rd.qh2h_driver_a_id is None
    assert rd.qh2h_driver_b_id is None
    assert rd.quali_nth_position is None
    assert rd.special_a_key is not None
    assert rd.special_b_key is not None


def test_specials_catalogue_has_eight_unique_keys():
    assert len(SPECIAL_KEYS) == 8
    assert len(set(SPECIAL_KEYS)) == 8
