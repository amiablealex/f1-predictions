"""Lineup editor, debutant ingestion, and end-to-end seat scoring."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.api.exceptions import JolpicaNotFoundError
from app.api.jolpica import APIDriver, APIQualifyingEntry, APIRaceEntry, APIRound, APIScheduledSession
from app.extensions import db as _db
from app.lineups import LineupError, add_placeholder_driver, update_lineup
from app.models.driver import Driver, RoundDriver
from app.models.prediction import PredictionScore, PredictionType, Top10Prediction
from app.models.round import Round, SessionStatus, SessionType
from worker.ingest import upsert_driver, upsert_drivers, upsert_round_with_sessions
from worker.jobs import results_poll_job

RBR, RB, HAAS = "Red Bull", "RB F1 Team", "Haas F1 Team"
REGULARS = [("ver", 1, RBR), ("had", 6, RBR), ("law", 30, RB), ("lin", 41, RB),
            ("bea", 87, HAAS), ("oco", 31, HAAS)]


def _api_driver(ref, num=None):
    return APIDriver(ref, ref.title(), ref.title(), ref[:3].upper(), num, "X")


def _make_round(db, number, *, open_=True, race_start=None):
    start = race_start or datetime.now(timezone.utc) + timedelta(days=3 + number)
    rd = upsert_round_with_sessions(db.session, APIRound(
        season=2026, round_number=number, gp_name=f"GP {number}", country=None,
        country_code=None, circuit_name=None, circuit_ref=None, has_sprint=False,
        sessions=[APIScheduledSession("qualifying", start - timedelta(days=1)),
                  APIScheduledSession("race", start)],
    ))
    rd.predictions_locked = not open_
    drivers = upsert_drivers(db.session, [_api_driver(r, n) for r, n, _ in REGULARS])
    for ref, num, team in REGULARS:
        db.session.add(RoundDriver(round_id=rd.id, car_number=num,
                                   expected_driver_id=drivers[ref].id, constructor_name=team))
    db.session.flush()
    return rd, drivers


def _seat(rd, driver_id):
    return next(s for s in rd.round_drivers if s.expected_driver_id == driver_id)


def _pick(db, user, rd, pos, driver):
    db.session.add(Top10Prediction(user_id=user.id, round_id=rd.id, position=pos,
                                   predicted_driver_id=driver.id))


def _picks(rd_id, user_id):
    _db.session.expire_all()
    rows = _db.session.query(Top10Prediction).filter_by(round_id=rd_id, user_id=user_id).all()
    return {p.position: p.predicted_driver.driver_ref for p in rows}


class TestUpdateLineup:
    def test_single_round_relists_seat_and_moves_picks(self, db, make_user):
        user = make_user()
        rd, d = _make_round(db, 21)
        new = upsert_driver(db.session, _api_driver("new", 99))
        _pick(db, user, rd, 5, d["oco"])
        db.session.commit()

        change = update_lineup(rd, {_seat(rd, d["oco"].id).id: new.id}, "round")
        db.session.commit()

        assert (change.rounds, change.seats, change.picks) == ([21], 1, 1)
        assert _picks(rd.id, user.id) == {5: "new"}
        seat = _db.session.query(RoundDriver).filter_by(round_id=rd.id, expected_driver_id=new.id).one()
        assert seat.constructor_name == HAAS

    def test_shuffle_in_one_save(self, db, make_user):
        # LAW promoted into HAD's seat, TSU into LAW's: a chain that
        # transiently duplicates LAW — the deferred constraint allows it.
        user = make_user()
        rd, d = _make_round(db, 21)
        tsu = upsert_driver(db.session, _api_driver("tsu", 22))
        _pick(db, user, rd, 3, d["had"])
        _pick(db, user, rd, 4, d["law"])
        db.session.commit()

        update_lineup(rd, {_seat(rd, d["had"].id).id: d["law"].id,
                           _seat(rd, d["law"].id).id: tsu.id}, "round")
        db.session.commit()

        assert _picks(rd.id, user.id) == {3: "law", 4: "tsu"}
        lineup = {(s.expected_driver.driver_ref, s.constructor_name)
                  for s in _db.session.get(Round, rd.id).round_drivers}
        assert ("law", RBR) in lineup and ("tsu", RB) in lineup

    def test_duplicate_driver_rejected_without_changes(self, db, make_user):
        user = make_user()
        rd, d = _make_round(db, 21)
        _pick(db, user, rd, 3, d["had"])
        db.session.commit()
        with pytest.raises(LineupError, match="two seats"):
            update_lineup(rd, {_seat(rd, d["had"].id).id: d["law"].id}, "round")
        db.session.rollback()
        assert _picks(rd.id, user.id) == {3: "had"}

    def test_season_scope_reaches_later_open_rounds_only(self, db, make_user):
        r20, d = _make_round(db, 20)
        r21, _ = _make_round(db, 21)
        r22, _ = _make_round(db, 22)
        r22.predictions_locked = True     # e.g. admin-locked; must not change
        new = upsert_driver(db.session, _api_driver("new", 99))
        db.session.commit()

        change = update_lineup(r21, {_seat(r21, d["oco"].id).id: new.id}, "season")
        db.session.commit()

        assert change.rounds == [21]
        def has(rd, ref):
            return any(s.expected_driver.driver_ref == ref for s in _db.session.get(Round, rd.id).round_drivers)
        assert has(r20, "oco") and has(r22, "oco") and has(r21, "new")

    def test_closed_round_rejected(self, db):
        rd, d = _make_round(db, 21, open_=False)
        new = upsert_driver(db.session, _api_driver("new", 99))
        with pytest.raises(LineupError, match="closed"):
            update_lineup(rd, {_seat(rd, d["oco"].id).id: new.id}, "round")


class TestPlaceholder:
    def test_jolpica_record_claims_placeholder(self, db):
        p = add_placeholder_driver("Arvid", "Lindblad", "lin")
        db.session.commit()
        claimed = upsert_driver(db.session, APIDriver("arvid_lindblad", "Arvid", "Lindblad", "LIN", 41, "British"))
        assert claimed.id == p.id and claimed.driver_ref == "arvid_lindblad"
        assert db.session.query(Driver).filter(Driver.family_name == "Lindblad").count() == 1

    def test_bad_code_rejected(self, db):
        with pytest.raises(LineupError):
            add_placeholder_driver("A", "B", "toolong")


class _Fake:
    def __init__(self, quali=(), race=()):
        self.quali, self.race = list(quali), list(race)

    def get_qualifying_results(self, season, rnd):
        if not self.quali:
            raise JolpicaNotFoundError("none")
        return self.quali

    def get_race_results(self, season, rnd):
        if not self.race:
            raise JolpicaNotFoundError("none")
        return self.race

    def get_pit_stops(self, season, rnd):
        raise JolpicaNotFoundError("none")

    def get_round_entries(self, season, rnd, prefer_session="race"):
        raise AssertionError("existing lineup must not be rebuilt")


def test_debut_reserve_is_ingested_and_scored_for_the_seat(app, db, make_user):
    """OCO replaced by a driver the app has never seen: his result row is
    kept, the lineup is untouched, and an OCO pick scores his finish."""
    user = make_user()
    rd, d = _make_round(db, 19, open_=False,
                        race_start=datetime.now(timezone.utc) - timedelta(hours=2))
    _pick(db, user, rd, 6, d["oco"])
    for s in rd.sessions:
        s.status = SessionStatus.PENDING_RESULTS
    db.session.commit()
    rid = rd.id

    order = ["ver", "had", "law", "lin", "bea"]
    teams = {r: t for r, _, t in REGULARS}
    nums = {r: n for r, n, _ in REGULARS}
    race = [APIRaceEntry(i, nums[r], r, teams[r], "Finished", True, i == 1,
                         grid=i, laps_completed=50, driver=_api_driver(r, nums[r]))
            for i, r in enumerate(order, 1)]
    race.append(APIRaceEntry(6, 99, "rookie", HAAS, "Finished", True, False,
                             grid=6, laps_completed=50, driver=_api_driver("rookie", 99)))
    quali = [APIQualifyingEntry(e.position, e.car_number, e.driver_ref, 80_000,
                                constructor_name=e.constructor_name, driver=e.driver)
             for e in race]

    results_poll_job(app, _Fake(quali=quali, race=race))

    _db.session.expire_all()
    rd = _db.session.get(Round, rid)
    assert len(rd.round_drivers) == 6
    assert any(s.expected_driver.driver_ref == "oco" for s in rd.round_drivers)
    race_s = next(s for s in rd.sessions if s.session_type == SessionType.RACE)
    rookie_row = next(r for r in race_s.results if r.actual_driver.driver_ref == "rookie")
    assert rookie_row.constructor_name == HAAS
    score = _db.session.query(PredictionScore).filter_by(
        round_id=rid, user_id=user.id, kind=PredictionType.RACE_TOP10, position=6,
    ).one()
    assert score.points == rd.scoring_config.race_top10_correct


def test_admin_round_page_renders_lineup(client, db, make_user, login):
    make_user(is_admin=True)
    login()
    rd, _ = _make_round(db, 21)
    locked, _ = _make_round(db, 15, open_=False)
    db.session.commit()
    assert b"Save lineup" in client.get(f"/admin/round/{rd.id}").data
    assert b"lineup fixed" in client.get(f"/admin/round/{locked.id}").data


def test_admin_lineup_post(client, db, make_user, login):
    make_user(is_admin=True)
    login()
    rd, d = _make_round(db, 21)
    new = upsert_driver(db.session, _api_driver("new", 99))
    db.session.commit()
    form = {f"seat_{s.id}": str(s.expected_driver_id) for s in rd.round_drivers}
    form[f"seat_{_seat(rd, d['oco'].id).id}"] = str(new.id)
    form["scope"] = "season"
    resp = client.post(f"/admin/round/{rd.id}/lineup", data=form, follow_redirects=True)
    assert b"Updated 1 seat" in resp.data


def test_repair_seats_restores_lineup_and_rescores(app, db, make_user):
    """R12 as the old worker left it: LAW's RB seat overwritten to Red
    Bull, TSU added as a 7th seat. Repair restores the form lineup and
    re-scores: HAD pick gets LAW's drive, LAW pick gets TSU's."""
    from app.models.result import SessionResult
    from worker.jobs import _phase_scoring_inner
    from app.models.round import ScoringPhase

    user = make_user()
    r12, d = _make_round(db, 12, open_=False,
                         race_start=datetime.now(timezone.utc) - timedelta(days=30))
    _make_round(db, 21)                                    # untouched reference
    tsu = upsert_driver(db.session, _api_driver("tsu", 22))
    _seat(r12, d["law"].id).constructor_name = RBR          # old refresh damage
    db.session.add(RoundDriver(round_id=r12.id, car_number=22,
                               expected_driver_id=tsu.id, constructor_name=RB))
    race = next(s for s in r12.sessions if s.session_type == SessionType.RACE)
    rows = [("ver", RBR), ("law", RBR), ("tsu", RB), ("lin", RB), ("bea", HAAS), ("oco", HAAS)]
    drivers = {**d, "tsu": tsu}
    for pos, (ref, team) in enumerate(rows, 1):
        db.session.add(SessionResult(session_id=race.id, position=pos, car_number=drivers[ref].permanent_number,
                                     actual_driver_id=drivers[ref].id, constructor_name=team,
                                     status="Finished", is_classified=True, is_fastest_lap=False))
    race.status = SessionStatus.COMPLETED
    _pick(db, user, r12, 2, d["had"])
    _pick(db, user, r12, 3, d["law"])
    db.session.commit()
    _phase_scoring_inner(r12.id, ScoringPhase.RACE)

    def pts(pos):
        _db.session.expire_all()
        return _db.session.query(PredictionScore).filter_by(
            round_id=r12.id, user_id=user.id, kind=PredictionType.RACE_TOP10, position=pos,
        ).one().points
    assert (pts(2), pts(3)) == (0, 5)        # broken: HAD scores nothing, LAW gets his own Red Bull P2

    out = app.test_cli_runner().invoke(args=["repair-seats", "--season", "2026", "--no-refetch"])
    assert "remove seat TSU" in out.output and "Dry run" in out.output
    assert (pts(2), pts(3)) == (0, 5)        # dry run changed nothing

    out = app.test_cli_runner().invoke(args=["repair-seats", "--season", "2026", "--no-refetch", "--apply"])
    assert "Applied." in out.output, out.output
    assert "LAW drove HAD's seat" in out.output
    full = _db.session.get(Round, r12.id).scoring_config.race_top10_correct
    assert (pts(2), pts(3)) == (full, full)
    lineup = {(s.expected_driver.driver_ref, s.constructor_name) for s in _db.session.get(Round, r12.id).round_drivers}
    assert ("law", RB) in lineup and len(lineup) == 6


def test_repair_seats_refetch_fills_legacy_rows(app, db, make_user, monkeypatch):
    """R19 as ingested before this change: no team on result rows, and the
    debutant's row skipped. The re-fetch fills both; OCO's pick then
    scores the rookie's P6."""
    from app.models.result import SessionResult
    import app.cli as cli

    user = make_user()
    r19, d = _make_round(db, 19, open_=False,
                         race_start=datetime.now(timezone.utc) - timedelta(days=10))
    _make_round(db, 21)
    race = next(s for s in r19.sessions if s.session_type == SessionType.RACE)
    order = ["ver", "had", "law", "lin", "bea"]
    for pos, ref in enumerate(order, 1):
        db.session.add(SessionResult(session_id=race.id, position=pos, car_number=d[ref].permanent_number,
                                     actual_driver_id=d[ref].id, status="Finished",
                                     is_classified=True, is_fastest_lap=(pos == 1)))
    race.status = SessionStatus.COMPLETED
    _pick(db, user, r19, 6, d["oco"])
    db.session.commit()

    teams = {r: t for r, _, t in REGULARS}
    nums = {r: n for r, n, _ in REGULARS}
    api = [APIRaceEntry(i, nums[r], r, teams[r], "Finished", True, i == 1, grid=i,
                        laps_completed=50, driver=_api_driver(r, nums[r]))
           for i, r in enumerate(order, 1)]
    api.append(APIRaceEntry(6, 99, "rookie", HAAS, "Finished", True, False, grid=6,
                            laps_completed=50, driver=_api_driver("rookie", 99)))
    monkeypatch.setattr(cli, "build_default_client", lambda config: _Fake(race=api))

    out = app.test_cli_runner().invoke(args=["repair-seats", "--season", "2026", "--apply"])
    assert "ROO drove OCO's seat" in out.output, out.output
    _db.session.expire_all()
    race = _db.session.get(type(race), race.id)
    assert race.status == SessionStatus.COMPLETED
    assert all(r.constructor_name for r in race.results) and len(race.results) == 6
    score = _db.session.query(PredictionScore).filter_by(
        round_id=r19.id, user_id=user.id, kind=PredictionType.RACE_TOP10, position=6).one()
    assert score.points == _db.session.get(Round, r19.id).scoring_config.race_top10_correct
