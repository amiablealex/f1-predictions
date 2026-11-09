"""results_recheck_job: completed sessions from the last week are re-fetched
and re-scored when Jolpica has changed them (post-race penalties)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.exceptions import JolpicaNotFoundError, JolpicaTransientError
from app.api.jolpica import APIRaceEntry
from app.extensions import db as _db
from app.models.prediction import PredictionScore, PredictionType, Top10Prediction
from app.models.round import Session, SessionStatus, SessionType
from tests.test_lineups import REGULARS, _api_driver, _make_round
from worker.jobs import phase_scoring_job, results_recheck_job
from app.models.round import ScoringPhase

ORDER = ["ver", "had", "law", "lin", "bea", "oco"]
TEAMS = {r: t for r, _, t in REGULARS}
NUMS = {r: n for r, n, _ in REGULARS}


def _rows(order):
    return [APIRaceEntry(i, NUMS[r], r, TEAMS[r], "Finished", True, i == 1, grid=i,
                         laps_completed=50, driver=_api_driver(r, NUMS[r]))
            for i, r in enumerate(order, 1)]


class Fake:
    def __init__(self, race=None, error=None):
        self.race, self.error, self.calls = race, error, 0

    def get_race_results(self, season, rnd):
        self.calls += 1
        if self.error:
            raise self.error
        return self.race

    def get_qualifying_results(self, season, rnd):
        raise JolpicaNotFoundError("none")

    def get_pit_stops(self, season, rnd):
        raise JolpicaNotFoundError("none")


def _setup(app, db, make_user, days_ago=2):
    """Completed, scored race from `days_ago`; user picked LAW for P3."""
    from worker.ingest import ingest_race_results, upsert_drivers

    user = make_user()
    rd, d = _make_round(db, 18, open_=False,
                        race_start=datetime.now(timezone.utc) - timedelta(days=days_ago))
    race = next(s for s in rd.sessions if s.session_type == SessionType.RACE)
    quali = next(s for s in rd.sessions if s.session_type == SessionType.QUALIFYING)
    quali.status = SessionStatus.UPCOMING        # keep the test to the race
    rows = _rows(ORDER)
    ingest_race_results(db.session, race, rows, upsert_drivers(db.session, [r.driver for r in rows]))
    db.session.add(Top10Prediction(user_id=user.id, round_id=rd.id, position=3,
                                   predicted_driver_id=d["law"].id))
    db.session.commit()
    phase_scoring_job(app, rd.id, ScoringPhase.RACE)
    return user.id, rd.id, race.id


def _points(rid, uid):
    _db.session.expire_all()
    return _db.session.query(PredictionScore).filter_by(
        round_id=rid, user_id=uid, kind=PredictionType.RACE_TOP10, position=3).one().points


def _scored_at(sid):
    _db.session.expire_all()
    return _db.session.get(Session, sid).scored_at


def test_penalty_reorder_is_rescored(app, db, make_user):
    uid, rid, sid = _setup(app, db, make_user)
    assert _points(rid, uid) == 10                       # LAW P3 exact
    penalised = ["ver", "had", "lin", "law", "bea", "oco"]   # LAW drops to P4
    results_recheck_job(app, Fake(race=_rows(penalised)))
    assert _points(rid, uid) == 5
    assert _db.session.get(Session, sid).status == SessionStatus.COMPLETED


def test_unchanged_results_are_not_rescored(app, db, make_user):
    uid, rid, sid = _setup(app, db, make_user)
    stamp = _scored_at(sid)
    results_recheck_job(app, Fake(race=_rows(ORDER)))
    assert _scored_at(sid) == stamp


def test_shrinking_result_set_is_discarded(app, db, make_user):
    uid, rid, sid = _setup(app, db, make_user)
    results_recheck_job(app, Fake(race=_rows(["ver", "had", "lin", "law"])))
    assert _points(rid, uid) == 10
    _db.session.expire_all()
    assert len(_db.session.get(Session, sid).results) == 6


def test_mid_edit_data_is_ignored(app, db, make_user):
    uid, rid, sid = _setup(app, db, make_user)
    results_recheck_job(app, Fake(error=JolpicaTransientError("position(s) 3 repeated")))
    assert _points(rid, uid) == 10


def test_old_rounds_are_not_fetched(app, db, make_user):
    _setup(app, db, make_user, days_ago=9)
    fake = Fake(race=_rows(ORDER))
    results_recheck_job(app, fake)
    assert fake.calls == 0
