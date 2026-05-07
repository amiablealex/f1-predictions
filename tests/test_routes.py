"""Smoke tests for the Phase 5 blueprints.

These confirm: routes register, auth gating works, admin gating works, and
the most important user flows (create league + join via code + leaderboard
renders + round view loads) hold together end-to-end.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.api.jolpica import APIRound, APIScheduledSession
from app.extensions import db as _db
from app.models.driver import Driver, RoundDriver
from app.models.league import League, LeagueMembership
from app.models.round import Round, SessionStatus, SessionType
from worker.ingest import upsert_drivers, upsert_round_with_sessions


# =============================================================================
# Fixtures
# =============================================================================


def _api_round(round_number=1):
    base = datetime.now(timezone.utc) + timedelta(days=7)
    return APIRound(
        season=2026, round_number=round_number,
        gp_name="Test GP", country="Italy", country_code="IT",
        circuit_name="Test Circuit", circuit_ref="test_circuit",
        has_sprint=False,
        sessions=[
            APIScheduledSession("qualifying", base - timedelta(hours=24)),
            APIScheduledSession("race", base),
        ],
    )


@pytest.fixture
def seeded_round(app, db):
    """A round with two regular drivers and a future deadline."""
    rd = upsert_round_with_sessions(db.session, _api_round())
    from app.api.jolpica import APIDriver
    drivers_map = upsert_drivers(db.session, [
        APIDriver("d1", "Lewis", "Hamilton", "HAM", 44, "British"),
        APIDriver("d2", "Max", "Verstappen", "VER", 1, "Dutch"),
    ])
    db.session.add_all([
        RoundDriver(round_id=rd.id, car_number=44, expected_driver_id=drivers_map["d1"].id),
        RoundDriver(round_id=rd.id, car_number=1, expected_driver_id=drivers_map["d2"].id),
    ])
    db.session.commit()
    return rd


# =============================================================================
# Auth gating: anonymous → login redirect
# =============================================================================


@pytest.mark.parametrize("path", [
    "/predictions",
    "/round/current",
    "/leagues/",
    "/leaderboard/",
    "/admin/",
])
def test_anonymous_redirected_to_login(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 302
    assert "/auth/login" in response.headers.get("Location", "")


def test_rules_is_public(client):
    """Rules page is the only authenticated-optional view."""
    response = client.get("/rules/")
    assert response.status_code == 200


# =============================================================================
# Admin gating
# =============================================================================


def test_admin_route_blocks_non_admin(client, make_user, login):
    make_user(is_admin=False)
    login()
    response = client.get("/admin/", follow_redirects=False)
    assert response.status_code == 403


def test_admin_route_allows_admin(client, make_user, login):
    make_user(is_admin=True)
    login()
    response = client.get("/admin/")
    assert response.status_code == 200


# =============================================================================
# Index dispatch
# =============================================================================


def test_index_authenticated_with_open_round_goes_to_predictions(
    client, make_user, login, seeded_round,
):
    make_user()
    login()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert "/predictions" in response.headers.get("Location", "")


# =============================================================================
# Predictions form
# =============================================================================


def test_predictions_form_renders(client, make_user, login, seeded_round):
    make_user()
    login()
    response = client.get("/predictions")
    assert response.status_code == 200
    assert b"Save predictions" in response.data
    assert b"Pole lap time" in response.data


def test_predictions_form_persists_submission(client, make_user, login, seeded_round, db):
    user = make_user()
    login()

    # Find driver IDs to submit
    drivers = {d.driver_ref: d for d in db.session.query(Driver).all()}
    response = client.post("/predictions", data={
        "csrf_token": _csrf(client, "/predictions"),
        "top10_1": str(drivers["d1"].id),
        "top10_2": str(drivers["d2"].id),
        "quali_top3_1": str(drivers["d1"].id),
        "pole_time": "1:23.456",
        "fastest_lap": str(drivers["d1"].id),
        "dnf_count": "3",
    }, follow_redirects=False)
    assert response.status_code == 302

    from app.models.prediction import (
        DnfCountPrediction, FastestLapPrediction,
        PoleTimePrediction, Top10Prediction,
    )
    top10 = db.session.query(Top10Prediction).filter_by(user_id=user.id).all()
    assert len(top10) == 2
    pole = db.session.query(PoleTimePrediction).filter_by(user_id=user.id).one()
    assert pole.predicted_time_ms == 83_456
    fl = db.session.query(FastestLapPrediction).filter_by(user_id=user.id).one()
    assert fl.predicted_driver_id == drivers["d1"].id
    dnf = db.session.query(DnfCountPrediction).filter_by(user_id=user.id).one()
    assert dnf.predicted_count == 3


def test_predictions_form_rejects_duplicate_driver_in_top10(client, make_user, login, seeded_round, db):
    make_user()
    login()
    drivers = {d.driver_ref: d for d in db.session.query(Driver).all()}
    response = client.post("/predictions", data={
        "csrf_token": _csrf(client, "/predictions"),
        "top10_1": str(drivers["d1"].id),
        "top10_2": str(drivers["d1"].id),    # same driver twice
    }, follow_redirects=True)
    assert b"each driver can only appear once" in response.data


# =============================================================================
# Round view
# =============================================================================


def test_round_view_loads_for_own_round(client, make_user, login, seeded_round):
    make_user()
    login()
    response = client.get(f"/round/{seeded_round.season}/{seeded_round.round_number}")
    assert response.status_code == 200
    assert b"Round" in response.data
    assert b"Test GP" in response.data


def test_friend_view_blocked_when_round_not_locked(client, make_user, login, seeded_round, db):
    make_user(email="me@example.com", username="me")
    friend = make_user(email="friend@example.com", username="friend")
    login(email="me@example.com")
    response = client.get(
        f"/round/{seeded_round.season}/{seeded_round.round_number}/u/friend",
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_friend_view_blocked_when_no_shared_league(client, make_user, login, seeded_round, db):
    make_user(email="me@example.com", username="me")
    make_user(email="friend@example.com", username="friend")
    seeded_round.predictions_locked = True
    db.session.commit()
    login(email="me@example.com")
    response = client.get(
        f"/round/{seeded_round.season}/{seeded_round.round_number}/u/friend",
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_friend_view_allowed_when_round_locked_and_share_league(
    client, make_user, login, seeded_round, db,
):
    me = make_user(email="me@example.com", username="me")
    friend = make_user(email="friend@example.com", username="friend")
    seeded_round.predictions_locked = True
    league = League(name="L", invite_code="ABC123", created_by_id=me.id)
    db.session.add(league)
    db.session.flush()
    db.session.add_all([
        LeagueMembership(league_id=league.id, user_id=me.id),
        LeagueMembership(league_id=league.id, user_id=friend.id),
    ])
    db.session.commit()
    login(email="me@example.com")
    response = client.get(
        f"/round/{seeded_round.season}/{seeded_round.round_number}/u/friend"
    )
    assert response.status_code == 200
    assert b"friend" in response.data


# =============================================================================
# Leagues
# =============================================================================


def test_create_league_then_join_with_code(client, make_user, login, db):
    creator = make_user(email="a@example.com", username="creator")
    login(email="a@example.com")
    response = client.post("/leagues/new", data={
        "csrf_token": _csrf(client, "/leagues/new"),
        "name": "The Boon Squad",
    }, follow_redirects=False)
    assert response.status_code == 302
    league = db.session.query(League).filter_by(name="The Boon Squad").one()
    assert league.created_by_id == creator.id

    # Different user joins via code
    client.post("/auth/logout", data={"csrf_token": _csrf(client, "/")})
    joiner = make_user(email="b@example.com", username="joiner")
    login(email="b@example.com")
    response = client.post("/leagues/join", data={
        "csrf_token": _csrf(client, "/leagues/join"),
        "invite_code": league.invite_code,
    }, follow_redirects=False)
    assert response.status_code == 302
    assert db.session.query(LeagueMembership).filter_by(
        league_id=league.id, user_id=joiner.id,
    ).first() is not None


def test_join_with_invalid_code(client, make_user, login):
    make_user()
    login()
    response = client.post("/leagues/join", data={
        "csrf_token": _csrf(client, "/leagues/join"),
        "invite_code": "ZZZZZZ",
    }, follow_redirects=True)
    assert b"match any league" in response.data


# =============================================================================
# Leaderboard
# =============================================================================


def test_leaderboard_renders_for_member(client, make_user, login, db):
    user = make_user()
    league = League(name="L", invite_code="ABC123", created_by_id=user.id)
    db.session.add(league)
    db.session.flush()
    db.session.add(LeagueMembership(league_id=league.id, user_id=user.id))
    db.session.commit()
    login()
    response = client.get(f"/leaderboard/{league.id}")
    assert response.status_code == 200
    assert b"Leaderboard" in response.data or b"Total points" in response.data


def test_leaderboard_blocks_non_member(client, make_user, login, db):
    other = make_user(email="other@example.com", username="other")
    league = League(name="Private", invite_code="PRIV12", created_by_id=other.id)
    db.session.add(league)
    db.session.flush()
    db.session.add(LeagueMembership(league_id=league.id, user_id=other.id))
    db.session.commit()
    make_user()
    login()
    response = client.get(f"/leaderboard/{league.id}", follow_redirects=False)
    assert response.status_code == 404


# =============================================================================
# Rules page reflects current config
# =============================================================================


def test_rules_renders_current_points(client):
    """Rules page reads SCORING_DEFAULTS at render time."""
    from app.config import Config
    response = client.get("/rules/")
    assert response.status_code == 200
    expected_top10 = str(Config.SCORING_DEFAULTS["race_top10_correct"]).encode()
    assert expected_top10 in response.data


# =============================================================================
# CSRF helper
# =============================================================================


def _csrf(client, path: str) -> str:
    """Pull a fresh CSRF token by GETting a form page first."""
    response = client.get(path)
    body = response.data.decode("utf-8", errors="ignore")
    marker = 'name="csrf_token" value="'
    idx = body.find(marker)
    if idx == -1:
        return ""
    start = idx + len(marker)
    end = body.find('"', start)
    return body[start:end]
