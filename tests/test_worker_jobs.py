"""Integration tests for worker ingestion and jobs.

Strategy: build a fake Jolpica client whose methods return canned typed
dataclasses. Run the worker jobs against a real Postgres test DB and
inspect the resulting state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.api.jolpica import (
    APIDriver,
    APIQualifyingEntry,
    APIRaceEntry,
    APIRound,
    APIRoundEntry,
    APIScheduledSession,
)
from app.config import Config
from app.extensions import db as _db
from app.models.driver import Driver, RoundDriver
from app.models.prediction import (
    DnfCountPrediction,
    FastestLapPrediction,
    PoleTimePrediction,
    PredictionScore,
    PredictionType,
    Top10Prediction,
)
from app.models.round import (
    Round,
    RoundState,
    ScoringPhase,
    Session,
    SessionStatus,
    SessionType,
    WeekendType,
)
from worker.ingest import (
    ingest_qualifying_results,
    ingest_race_results,
    upsert_drivers,
    upsert_round_drivers_from_entries,
    upsert_round_with_sessions,
)
from worker.jobs import (
    deadline_lock_job,
    phase_scoring_job,
    schedule_sync_job,
    session_state_transitions_job,
)


# =============================================================================
# Fake Jolpica client
# =============================================================================


@dataclass
class FakeJolpicaClient:
    """In-memory stand-in. Set the attributes the test needs and pass it
    into a job."""
    season_schedule: list[APIRound] = field(default_factory=list)
    season_drivers: list[APIDriver] = field(default_factory=list)
    qualifying_results: dict[tuple[int, int], list[APIQualifyingEntry]] = field(default_factory=dict)
    race_results: dict[tuple[int, int], list[APIRaceEntry]] = field(default_factory=dict)
    sprint_race_results: dict[tuple[int, int], list[APIRaceEntry]] = field(default_factory=dict)
    round_entries: dict[tuple[int, int], list[APIRoundEntry]] = field(default_factory=dict)

    def get_season_schedule(self, season):
        return self.season_schedule

    def get_season_drivers(self, season):
        return self.season_drivers

    def get_qualifying_results(self, season, round_number):
        return self.qualifying_results[(season, round_number)]

    def get_race_results(self, season, round_number):
        return self.race_results[(season, round_number)]

    def get_sprint_race_results(self, season, round_number):
        return self.sprint_race_results[(season, round_number)]

    def get_round_entries(self, season, round_number, prefer_session="race"):
        return self.round_entries[(season, round_number)]


# =============================================================================
# Helpers
# =============================================================================


def _api_driver(ref: str, code: str, num: int, given="First", family="Last") -> APIDriver:
    return APIDriver(
        driver_ref=ref, given_name=given, family_name=family,
        code=code, permanent_number=num, nationality="Some",
    )


def _make_standard_round(season=2026, round_number=1, base=None) -> APIRound:
    base = base or datetime(2026, 5, 24, 13, 0, tzinfo=timezone.utc)
    return APIRound(
        season=season, round_number=round_number,
        gp_name="Test GP", country="Italy", country_code="IT",
        circuit_name="Test Circuit", circuit_ref="test_circuit",
        has_sprint=False,
        sessions=[
            APIScheduledSession("qualifying", base - timedelta(days=1)),
            APIScheduledSession("race", base),
        ],
    )


def _make_sprint_round(season=2026, round_number=2) -> APIRound:
    base = datetime(2026, 6, 7, 13, 0, tzinfo=timezone.utc)
    return APIRound(
        season=season, round_number=round_number,
        gp_name="Sprint GP", country="Belgium", country_code="BE",
        circuit_name="Spa", circuit_ref="spa",
        has_sprint=True,
        sessions=[
            APIScheduledSession("sprint_quali", base - timedelta(days=2, hours=2)),
            APIScheduledSession("sprint_race", base - timedelta(days=1, hours=4)),
            APIScheduledSession("qualifying", base - timedelta(days=1)),
            APIScheduledSession("race", base),
        ],
    )


# =============================================================================
# upsert_round_with_sessions
# =============================================================================


def test_upsert_round_creates_round_sessions_and_scoring_config(app, db):
    api_round = _make_standard_round()
    rd = upsert_round_with_sessions(db.session, api_round)
    db.session.commit()

    assert rd.gp_name == "Test GP"
    assert rd.country_code == "IT"
    assert rd.weekend_type == WeekendType.STANDARD
    assert {s.session_type for s in rd.sessions} == {SessionType.QUALIFYING, SessionType.RACE}
    # Scoring config snapshotted with current defaults
    assert rd.scoring_config is not None
    assert rd.scoring_config.race_top10_correct == Config.SCORING_DEFAULTS["race_top10_correct"]
    # Predictions deadline = first scoring session − 60 minutes
    quali = next(s for s in rd.sessions if s.session_type == SessionType.QUALIFYING)
    assert rd.predictions_deadline == quali.scheduled_start - timedelta(minutes=60)


def test_upsert_round_is_idempotent(app, db):
    api_round = _make_standard_round()
    upsert_round_with_sessions(db.session, api_round)
    db.session.commit()
    upsert_round_with_sessions(db.session, api_round)
    db.session.commit()
    rounds = db.session.query(Round).all()
    sessions = db.session.query(Session).all()
    assert len(rounds) == 1
    assert len(sessions) == 2


def test_upsert_round_does_not_overwrite_started_session_time(app, db):
    """Once a session has begun progressing, schedule_sync should not move
    its scheduled_start backwards (admin overrides win)."""
    api_round = _make_standard_round()
    rd = upsert_round_with_sessions(db.session, api_round)
    quali = next(s for s in rd.sessions if s.session_type == SessionType.QUALIFYING)
    quali.status = SessionStatus.IN_PROGRESS
    original_start = quali.scheduled_start
    db.session.commit()

    # API now reports a different start time
    new_round = APIRound(
        season=2026, round_number=1,
        gp_name="Test GP", country="Italy", country_code="IT",
        circuit_name="Test Circuit", circuit_ref="test_circuit",
        has_sprint=False,
        sessions=[
            APIScheduledSession("qualifying", original_start + timedelta(hours=2)),
            APIScheduledSession("race", original_start + timedelta(days=1)),
        ],
    )
    upsert_round_with_sessions(db.session, new_round)
    db.session.commit()

    refreshed = next(s for s in db.session.query(Session).all()
                     if s.session_type == SessionType.QUALIFYING)
    assert refreshed.scheduled_start == original_start


def test_upsert_round_with_sprint_creates_four_sessions(app, db):
    api_round = _make_sprint_round()
    rd = upsert_round_with_sessions(db.session, api_round)
    db.session.commit()

    assert rd.weekend_type == WeekendType.SPRINT
    assert {s.session_type for s in rd.sessions} == {
        SessionType.SPRINT_QUALI, SessionType.SPRINT_RACE,
        SessionType.QUALIFYING, SessionType.RACE,
    }
    # Deadline is 60 min before the EARLIEST scoring session (sprint_quali).
    sq = next(s for s in rd.sessions if s.session_type == SessionType.SPRINT_QUALI)
    assert rd.predictions_deadline == sq.scheduled_start - timedelta(minutes=60)


# =============================================================================
# Driver upsert
# =============================================================================


def test_upsert_drivers_idempotent(app, db):
    drivers = [_api_driver("hamilton", "HAM", 44), _api_driver("max_verstappen", "VER", 1)]
    upsert_drivers(db.session, drivers)
    upsert_drivers(db.session, drivers)
    db.session.commit()
    rows = db.session.query(Driver).all()
    assert len(rows) == 2


# =============================================================================
# Schedule sync (end-to-end with FakeJolpicaClient)
# =============================================================================


def test_schedule_sync_creates_rounds(app, db):
    fake = FakeJolpicaClient(
        season_schedule=[_make_standard_round(round_number=1), _make_sprint_round(round_number=2)]
    )
    schedule_sync_job(app, fake)

    with app.app_context():
        rounds = _db.session.query(Round).order_by(Round.round_number).all()
        assert len(rounds) == 2
        assert rounds[0].weekend_type == WeekendType.STANDARD
        assert rounds[1].weekend_type == WeekendType.SPRINT


def test_schedule_sync_seeds_round_drivers_from_previous(app, db):
    # Round 1 already seeded with two drivers
    api_round_1 = _make_standard_round(round_number=1)
    upsert_round_with_sessions(db.session, api_round_1)
    rd1 = db.session.query(Round).filter_by(round_number=1).one()

    ham = Driver(driver_ref="hamilton", given_name="Lewis", family_name="Hamilton", code="HAM")
    ver = Driver(driver_ref="max_verstappen", given_name="Max", family_name="Verstappen", code="VER")
    db.session.add_all([ham, ver])
    db.session.flush()
    db.session.add_all([
        RoundDriver(round_id=rd1.id, car_number=44, expected_driver_id=ham.id),
        RoundDriver(round_id=rd1.id, car_number=1, expected_driver_id=ver.id),
    ])
    db.session.commit()

    # Now schedule_sync runs and adds Round 2; should seed RoundDriver
    fake = FakeJolpicaClient(
        season_schedule=[api_round_1, _make_standard_round(round_number=2)]
    )
    schedule_sync_job(app, fake)

    with app.app_context():
        rd2 = _db.session.query(Round).filter_by(round_number=2).one()
        assert {(r.car_number, r.expected_driver_id) for r in rd2.round_drivers} == {
            (44, ham.id), (1, ver.id),
        }


# =============================================================================
# Result ingestion
# =============================================================================


def test_ingest_race_results_computes_dnf_and_fastest_lap(app, db):
    api_round = _make_standard_round()
    rd = upsert_round_with_sessions(db.session, api_round)
    race = next(s for s in rd.sessions if s.session_type == SessionType.RACE)
    race.status = SessionStatus.PENDING_RESULTS

    drivers = [_api_driver(f"d{i}", f"D{i}", i) for i in range(1, 6)]
    drivers_map = upsert_drivers(db.session, drivers)
    db.session.commit()

    api_results = [
        APIRaceEntry(1, 1, "d1", "Mercedes", "Finished", True, False),
        APIRaceEntry(2, 2, "d2", "Red Bull", "Finished", True, True),    # FL
        APIRaceEntry(3, 3, "d3", "Ferrari", "+1 Lap", True, False),
        APIRaceEntry(18, 4, "d4", "Williams", "Engine", False, False),
        APIRaceEntry(19, 5, "d5", "Sauber", "Accident", False, False),
    ]
    ingest_race_results(db.session, race, api_results, drivers_map)
    db.session.commit()

    assert race.dnf_count == 2
    assert race.fastest_lap_driver_id == drivers_map["d2"].id
    assert race.status == SessionStatus.COMPLETED


def test_ingest_qualifying_sets_pole_time(app, db):
    api_round = _make_standard_round()
    rd = upsert_round_with_sessions(db.session, api_round)
    quali = next(s for s in rd.sessions if s.session_type == SessionType.QUALIFYING)
    quali.status = SessionStatus.PENDING_RESULTS

    drivers_map = upsert_drivers(db.session, [_api_driver("p1", "P1", 1), _api_driver("p2", "P2", 2)])
    db.session.commit()

    api_results = [
        APIQualifyingEntry(1, 1, "p1", best_time_ms=83_456),
        APIQualifyingEntry(2, 2, "p2", best_time_ms=83_700),
    ]
    ingest_qualifying_results(db.session, quali, api_results, drivers_map)
    db.session.commit()

    assert quali.pole_time_ms == 83_456
    assert quali.status == SessionStatus.COMPLETED


def test_ingest_results_is_idempotent(app, db):
    """Running ingest twice replaces rather than duplicates results."""
    api_round = _make_standard_round()
    rd = upsert_round_with_sessions(db.session, api_round)
    race = next(s for s in rd.sessions if s.session_type == SessionType.RACE)
    drivers_map = upsert_drivers(db.session, [_api_driver("d1", "D1", 1)])
    api_results = [APIRaceEntry(1, 1, "d1", "Merc", "Finished", True, True)]

    ingest_race_results(db.session, race, api_results, drivers_map)
    ingest_race_results(db.session, race, api_results, drivers_map)
    db.session.commit()

    assert len(race.results) == 1


# =============================================================================
# Session state transitions
# =============================================================================


def test_session_state_transitions(app, db, monkeypatch):
    """upcoming → in_progress → pending_results based on the wall clock."""
    api_round = _make_standard_round()
    rd = upsert_round_with_sessions(db.session, api_round)
    quali = next(s for s in rd.sessions if s.session_type == SessionType.QUALIFYING)
    # Force scheduled_start to be in the past
    quali.scheduled_start = datetime.now(timezone.utc) - timedelta(hours=3)
    quali.status = SessionStatus.UPCOMING
    db.session.commit()

    fake = FakeJolpicaClient()
    session_state_transitions_job(app, fake)

    with app.app_context():
        refreshed = _db.session.get(Session, quali.id)
        # 3 hours past start, default quali duration 75 min → should be pending_results
        assert refreshed.status == SessionStatus.PENDING_RESULTS


# =============================================================================
# Deadline lock
# =============================================================================


def test_deadline_lock_locks_when_passed(app, db):
    api_round = _make_standard_round()
    rd = upsert_round_with_sessions(db.session, api_round)
    rd.predictions_deadline = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.session.commit()

    fake = FakeJolpicaClient()
    deadline_lock_job(app, fake)

    with app.app_context():
        refreshed = _db.session.get(Round, rd.id)
        assert refreshed.predictions_locked is True


def test_deadline_lock_leaves_future_unlocked(app, db):
    api_round = _make_standard_round()
    rd = upsert_round_with_sessions(db.session, api_round)
    rd.predictions_deadline = datetime.now(timezone.utc) + timedelta(hours=2)
    db.session.commit()

    deadline_lock_job(app, FakeJolpicaClient())

    with app.app_context():
        refreshed = _db.session.get(Round, rd.id)
        assert refreshed.predictions_locked is False


# =============================================================================
# Phase scoring (end-to-end)
# =============================================================================


def test_race_phase_scoring_writes_score_rows(app, db, make_user):
    """End-to-end: round + race results in DB → phase_scoring writes
    PredictionScore rows for users who predicted."""
    user = make_user()

    # Build round + sessions
    api_round = _make_standard_round()
    rd = upsert_round_with_sessions(db.session, api_round)

    # Drivers and round driver mapping
    api_drivers = [_api_driver(f"d{i}", f"D{i}", i) for i in range(1, 11)]
    drivers_map = upsert_drivers(db.session, api_drivers)
    for i, ad in enumerate(api_drivers, start=1):
        db.session.add(RoundDriver(
            round_id=rd.id, car_number=i, expected_driver_id=drivers_map[ad.driver_ref].id,
        ))
    db.session.commit()

    # User predicts driver "d1" for P1
    db.session.add(Top10Prediction(
        user_id=user.id, round_id=rd.id, position=1,
        predicted_driver_id=drivers_map["d1"].id,
    ))
    # User predicts fastest lap for "d2"
    db.session.add(FastestLapPrediction(
        user_id=user.id, round_id=rd.id,
        predicted_driver_id=drivers_map["d2"].id,
    ))
    # User predicts 2 DNFs
    db.session.add(DnfCountPrediction(
        user_id=user.id, round_id=rd.id, predicted_count=2,
    ))
    db.session.commit()

    # Race results: drivers finish in order, d2 sets FL, 2 DNFs
    race = next(s for s in rd.sessions if s.session_type == SessionType.RACE)
    api_results = [
        APIRaceEntry(i, i, f"d{i}", "C", "Finished", True, (i == 2))
        for i in range(1, 9)
    ] + [
        APIRaceEntry(9, 9, "d9", "C", "Engine", False, False),
        APIRaceEntry(10, 10, "d10", "C", "Accident", False, False),
    ]
    ingest_race_results(db.session, race, api_results, drivers_map)
    db.session.commit()

    # Run phase scoring
    phase_scoring_job(app, rd.id, ScoringPhase.RACE)

    with app.app_context():
        scores = _db.session.query(PredictionScore).filter_by(
            user_id=user.id, round_id=rd.id,
        ).all()
        # 10 top10 + 1 fastest_lap + 1 dnf_count = 12 rows
        assert len(scores) == 12
        # P1 prediction was correct (d1 in car 1 finished P1) → 10 points
        p1 = next(s for s in scores if s.kind == PredictionType.RACE_TOP10 and s.position == 1)
        assert p1.points == 10
        # FL prediction was correct (d2 set FL) → 10 points
        fl = next(s for s in scores if s.kind == PredictionType.FASTEST_LAP)
        assert fl.points == 10
        # DNF count: predicted 2, actual 2 → 10 points
        dnf = next(s for s in scores if s.kind == PredictionType.DNF_COUNT)
        assert dnf.points == 10


def test_phase_scoring_replaces_previous_scores(app, db, make_user):
    """Re-running scoring updates rather than duplicates."""
    user = make_user()
    api_round = _make_standard_round()
    rd = upsert_round_with_sessions(db.session, api_round)

    drivers_map = upsert_drivers(db.session, [_api_driver("d1", "D1", 1)])
    db.session.add(RoundDriver(round_id=rd.id, car_number=1, expected_driver_id=drivers_map["d1"].id))
    db.session.add(Top10Prediction(
        user_id=user.id, round_id=rd.id, position=1,
        predicted_driver_id=drivers_map["d1"].id,
    ))
    db.session.commit()

    race = next(s for s in rd.sessions if s.session_type == SessionType.RACE)
    api_results = [APIRaceEntry(1, 1, "d1", "C", "Finished", True, False)]
    ingest_race_results(db.session, race, api_results, drivers_map)
    db.session.commit()

    phase_scoring_job(app, rd.id, ScoringPhase.RACE)
    phase_scoring_job(app, rd.id, ScoringPhase.RACE)

    with app.app_context():
        scores = _db.session.query(PredictionScore).filter_by(
            user_id=user.id, round_id=rd.id, kind=PredictionType.RACE_TOP10, position=1,
        ).all()
        assert len(scores) == 1
        assert scores[0].points == 10
