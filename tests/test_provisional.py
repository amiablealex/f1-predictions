"""Provisional results: incomplete Jolpica data is scored straight away,
withheld picks stay unscored, and the session completes once the data
does (or at the cut-off)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from app.api.exceptions import JolpicaNotFoundError
from app.api.jolpica import (
    APIPitStop,
    APIQualifyingEntry,
    APIRaceEntry,
    APIRound,
    APIScheduledSession,
    APIDriver,
)
from app.extensions import db as _db
from app.models.driver import RoundDriver
from app.models.prediction import (
    FastestLapPrediction,
    PlacesGainedPrediction,
    PoleTimePrediction,
    PredictionScore,
    PredictionType,
    Top3QualiPrediction,
    Top10Prediction,
)
from app.models.round import Round, RoundState, Session, SessionStatus, SessionType
from app.scoring.provisional import FASTEST_LAP, GRID, PIT_STOPS, POLE_TIME, missing_inputs
from worker.ingest import upsert_drivers, upsert_round_with_sessions
from worker.jobs import results_poll_job


@dataclass
class Fake:
    race: list = field(default_factory=list)
    quali: list = field(default_factory=list)
    stops: list | None = None

    def get_race_results(self, season, rnd):
        if not self.race:
            raise JolpicaNotFoundError("none")
        return self.race

    def get_qualifying_results(self, season, rnd):
        if not self.quali:
            raise JolpicaNotFoundError("none")
        return self.quali

    def get_pit_stops(self, season, rnd):
        if self.stops is None:
            raise JolpicaNotFoundError("none")
        return self.stops

    def get_round_entries(self, season, rnd, prefer_session="race"):
        raise JolpicaNotFoundError("none")


N = 5


def _race_rows(grid=True, fl=True):
    return [
        APIRaceEntry(
            i, i, f"d{i}", "C", "Finished", True, fl and i == 2,
            grid=(N + 1 - i) if grid else None, laps_completed=50,
            race_time_ms=5_000_000 + i * 1000,
        )
        for i in range(1, N + 1)
    ]


def _stops():
    return [APIPitStop(f"d{i}", 20, 1, 22_000) for i in range(1, N + 1)]


def _setup(db, user, race_started_hours_ago=2):
    race_start = datetime.now(timezone.utc) - timedelta(hours=race_started_hours_ago)
    api_round = APIRound(
        season=2026, round_number=20, gp_name="Test GP", country="Brazil",
        country_code="BR", circuit_name="Interlagos", circuit_ref="interlagos",
        has_sprint=False,
        sessions=[
            APIScheduledSession("qualifying", race_start - timedelta(days=1)),
            APIScheduledSession("race", race_start),
        ],
    )
    rd = upsert_round_with_sessions(db.session, api_round)
    rd.predictions_locked = True
    drivers = upsert_drivers(db.session, [
        APIDriver(f"d{i}", "G", f"F{i}", f"D{i}", i, "X") for i in range(1, N + 1)
    ])
    for i in range(1, N + 1):
        db.session.add(RoundDriver(round_id=rd.id, car_number=i,
                                   expected_driver_id=drivers[f"d{i}"].id))
    db.session.add(Top10Prediction(user_id=user.id, round_id=rd.id, position=1,
                                   predicted_driver_id=drivers["d1"].id))
    db.session.add(FastestLapPrediction(user_id=user.id, round_id=rd.id,
                                        predicted_driver_id=drivers["d2"].id))
    db.session.add(PlacesGainedPrediction(user_id=user.id, round_id=rd.id,
                                          predicted_driver_id=drivers["d1"].id))
    db.session.add(PoleTimePrediction(user_id=user.id, round_id=rd.id,
                                      predicted_time_ms=70_000))
    db.session.add(Top3QualiPrediction(user_id=user.id, round_id=rd.id, position=1,
                                       predicted_driver_id=drivers["d1"].id))
    for s in rd.sessions:
        s.status = SessionStatus.PENDING_RESULTS
    db.session.commit()
    return rd.id


def _session(round_id, stype):
    _db.session.expire_all()
    return _db.session.query(Session).filter_by(round_id=round_id, session_type=stype).one()


def _scores(round_id, user_id):
    _db.session.expire_all()
    rows = _db.session.query(PredictionScore).filter_by(round_id=round_id, user_id=user_id).all()
    return {(r.kind, r.position): r.points for r in rows}


def test_partial_race_goes_provisional_and_withholds(app, db, make_user):
    user = make_user()
    rid = _setup(db, user)
    results_poll_job(app, Fake(race=_race_rows(grid=False, fl=False),
                               quali=[APIQualifyingEntry(1, 1, "d1", 70_000)]))

    race = _session(rid, SessionType.RACE)
    assert race.status == SessionStatus.PROVISIONAL
    assert missing_inputs(race) == {GRID, FASTEST_LAP, PIT_STOPS}
    scores = _scores(rid, user.id)
    assert scores[(PredictionType.RACE_TOP10, 1)] == 10
    assert (PredictionType.FASTEST_LAP, None) not in scores
    assert (PredictionType.PLACES_GAINED, None) not in scores
    assert _db.session.get(Round, rid).state == RoundState.IN_PROGRESS


def test_provisional_race_completes_when_data_fills_in(app, db, make_user):
    user = make_user()
    rid = _setup(db, user)
    fake = Fake(race=_race_rows(grid=False, fl=False),
                quali=[APIQualifyingEntry(1, 1, "d1", 70_000)])
    results_poll_job(app, fake)

    fake.race, fake.stops = _race_rows(), _stops()
    results_poll_job(app, fake)

    race = _session(rid, SessionType.RACE)
    assert race.status == SessionStatus.COMPLETED
    scores = _scores(rid, user.id)
    assert scores[(PredictionType.FASTEST_LAP, None)] == 10
    assert scores[(PredictionType.PLACES_GAINED, None)] == N - 1   # grid 5 → P1
    assert _db.session.get(Round, rid).state == RoundState.COMPLETED


def test_unchanged_provisional_poll_does_not_rescore(app, db, make_user):
    user = make_user()
    rid = _setup(db, user)
    fake = Fake(race=_race_rows(grid=False), quali=[APIQualifyingEntry(1, 1, "d1", 70_000)])
    results_poll_job(app, fake)
    first = _session(rid, SessionType.RACE).scored_at

    results_poll_job(app, fake)
    race = _session(rid, SessionType.RACE)
    assert race.status == SessionStatus.PROVISIONAL
    assert race.scored_at == first


def test_provisional_finalised_after_cutoff(app, db, make_user):
    user = make_user()
    rid = _setup(db, user, race_started_hours_ago=49)
    results_poll_job(app, Fake(race=_race_rows(grid=False),
                               quali=[APIQualifyingEntry(1, 1, "d1", 70_000)]))

    race = _session(rid, SessionType.RACE)
    assert race.status == SessionStatus.COMPLETED
    # Past the cut-off, missing inputs score as 0 rather than being withheld.
    assert _scores(rid, user.id)[(PredictionType.PLACES_GAINED, None)] == 0


def test_quali_without_times_withholds_pole(app, db, make_user):
    user = make_user()
    rid = _setup(db, user)
    results_poll_job(app, Fake(quali=[APIQualifyingEntry(1, 1, "d1", None)]))

    quali = _session(rid, SessionType.QUALIFYING)
    assert quali.status == SessionStatus.PROVISIONAL
    assert missing_inputs(quali) == {POLE_TIME}
    scores = _scores(rid, user.id)
    assert (PredictionType.POLE_TIME, None) not in scores
    assert scores[(PredictionType.QUALI_TOP3, 1)] > 0


def test_flag_off_keeps_old_gate(app, db, make_user):
    user = make_user()
    rid = _setup(db, user)
    app.config["PROVISIONAL_RESULTS_ENABLED"] = False
    try:
        results_poll_job(app, Fake(race=_race_rows(grid=False),
                                   quali=[APIQualifyingEntry(1, 1, "d1", None)]))
    finally:
        app.config["PROVISIONAL_RESULTS_ENABLED"] = True
    assert _session(rid, SessionType.RACE).status == SessionStatus.PENDING_RESULTS
    assert _session(rid, SessionType.QUALIFYING).status == SessionStatus.COMPLETED


# =============================================================================
# Web: admin pole time, finalise, banners
# =============================================================================


def test_admin_pole_time_completes_quali_and_survives_refetch(app, db, client, make_user, login):
    admin = make_user(is_admin=True)
    login()
    rid = _setup(db, admin)
    fake = Fake(quali=[APIQualifyingEntry(1, 1, "d1", None)])
    results_poll_job(app, fake)

    resp = client.post(f"/admin/round/{rid}/pole-time", data={"pole_time": "1:10.000"})
    assert resp.status_code == 302
    quali = _session(rid, SessionType.QUALIFYING)
    assert quali.pole_time_ms == 70_000
    assert quali.status == SessionStatus.COMPLETED
    assert _scores(rid, admin.id)[(PredictionType.POLE_TIME, None)] > 0

    # Refresh + re-fetch with Jolpica still timeless keeps the manual time.
    client.post(f"/admin/round/{rid}/refresh-session/{quali.id}")
    results_poll_job(app, fake)
    assert _session(rid, SessionType.QUALIFYING).pole_time_ms == 70_000


def test_admin_pole_time_rejects_garbage(app, db, client, make_user, login):
    admin = make_user(is_admin=True)
    login()
    rid = _setup(db, admin)
    resp = client.post(f"/admin/round/{rid}/pole-time", data={"pole_time": "70"},
                       follow_redirects=True)
    assert b"Couldn" in resp.data
    assert _session(rid, SessionType.QUALIFYING).pole_time_ms is None


def test_admin_finalise(app, db, client, make_user, login):
    admin = make_user(is_admin=True)
    login()
    rid = _setup(db, admin)
    results_poll_job(app, Fake(race=_race_rows(grid=False),
                               quali=[APIQualifyingEntry(1, 1, "d1", 70_000)]))
    race = _session(rid, SessionType.RACE)
    client.post(f"/admin/round/{rid}/finalise-session/{race.id}")
    assert _session(rid, SessionType.RACE).status == SessionStatus.COMPLETED
    assert (PredictionType.PLACES_GAINED, None) in _scores(rid, admin.id)


def test_admin_pages_render(app, db, client, make_user, login):
    admin = make_user(is_admin=True)
    login()
    rid = _setup(db, admin)
    results_poll_job(app, Fake(race=_race_rows(grid=False),
                               quali=[APIQualifyingEntry(1, 1, "d1", 70_000)]))
    dash = client.get("/admin/")
    assert dash.status_code == 200
    assert b"Needs attention" in dash.data
    page = client.get(f"/admin/round/{rid}")
    assert page.status_code == 200
    assert b"missing: starting grid, pit stops" in page.data
    assert b"Finalise" in page.data


def test_round_page_and_leaderboard_show_provisional(app, db, client, make_user, login):
    from app.models.league import League, LeagueMembership
    user = make_user()
    login()
    rid = _setup(db, user)
    league = League(name="L", invite_code="abc123", created_by_id=user.id)
    db.session.add(league)
    db.session.flush()
    db.session.add(LeagueMembership(league_id=league.id, user_id=user.id))
    db.session.commit()
    results_poll_job(app, Fake(race=_race_rows(grid=False),
                               quali=[APIQualifyingEntry(1, 1, "d1", 70_000)]))

    page = client.get("/round/2026/20")
    assert b"Provisional points" in page.data
    assert b"worker" not in page.data
    board = client.get(f"/leaderboard/{league.id}")
    assert b"Includes provisional points from Round 20. These may change." in board.data
