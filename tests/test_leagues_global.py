"""Leagues landing, Global leaderboard and the global opt-in setting."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.api.jolpica import APIRound, APIScheduledSession
from app.models.league import League, LeagueMembership
from app.models.prediction import PredictionScore, PredictionType
from app.models.round import RoundState
from app.models.user import User
from worker.ingest import upsert_round_with_sessions


def _round(db, n):
    base = datetime.now(timezone.utc) - timedelta(days=30 - n)
    rd = upsert_round_with_sessions(db.session, APIRound(
        season=2026, round_number=n, gp_name=f"GP {n}", country="Italy",
        country_code="IT", circuit_name="C", circuit_ref=f"c{n}", has_sprint=False,
        sessions=[APIScheduledSession("race", base)],
    ))
    rd.state = RoundState.COMPLETED
    db.session.commit()
    return rd


def _score(db, user, rd, points):
    db.session.add(PredictionScore(
        user_id=user.id, round_id=rd.id, kind=PredictionType.POLE_TIME, points=points,
    ))
    db.session.commit()


def _league(db, name, code, *users):
    league = League(name=name, invite_code=code, created_by_id=users[0].id)
    db.session.add(league)
    db.session.flush()
    for u in users:
        db.session.add(LeagueMembership(league_id=league.id, user_id=u.id))
    db.session.commit()
    return league


@pytest.fixture
def trio(make_user):
    alice = make_user()
    bob = make_user(email="bob@example.com", username="bob")
    stranger = make_user(email="s@example.com", username="stranger")
    return alice, bob, stranger


# --------------------------------------------------------------- opt-in setting


def test_register_defaults_to_global(client, db):
    client.post("/auth/register", data={
        "email": "new@example.com", "username": "newbie",
        "password": "password123", "confirm_password": "password123",
        "show_on_global": "y",
    })
    assert db.session.query(User).filter_by(username="newbie").one().show_on_global is True


def test_register_can_opt_out(client, db):
    client.post("/auth/register", data={
        "email": "new@example.com", "username": "newbie",
        "password": "password123", "confirm_password": "password123",
    })
    assert db.session.query(User).filter_by(username="newbie").one().show_on_global is False


def test_register_form_checkbox_checked_by_default(client):
    html = client.get("/auth/register").get_data(as_text=True)
    assert 'name="show_on_global"' in html
    assert "checked" in html


def test_account_toggle(client, make_user, login, db):
    user = make_user()
    login()
    client.post("/auth/account/global", data={})
    db.session.refresh(user)
    assert user.show_on_global is False
    client.post("/auth/account/global", data={"show_on_global": "y"})
    db.session.refresh(user)
    assert user.show_on_global is True


# --------------------------------------------------------------- global board


def test_global_membership(app, db, trio):
    from app.leaderboard.routes import global_member_ids
    alice, bob, stranger = trio
    rd = _round(db, 1)
    _score(db, bob, rd, 5)
    _score(db, stranger, rd, 5)
    stranger.show_on_global = False
    db.session.commit()
    # Alice has no scores but is the viewer; stranger opted out.
    assert global_member_ids(alice) == sorted([alice.id, bob.id])
    # From Bob's view, scoreless Alice is left off.
    assert global_member_ids(bob) == [bob.id]


def test_global_view_links_only_league_mates(client, login, db, trio):
    alice, bob, stranger = trio
    _league(db, "Mates", "MATE01", alice, bob)
    rd = _round(db, 1)
    for u in trio:
        _score(db, u, rd, 3)
    login()
    html = client.get("/leaderboard/global").get_data(as_text=True)
    assert "/results/u/bob" in html
    assert "stranger" in html
    assert "/u/stranger" not in html
    assert "Compare a round" not in html
    assert "Don&#39;t want to appear here?" in html or "Don't want to appear here?" in html


def test_global_view_opted_out_banner(client, make_user, login, db):
    user = make_user()
    user.show_on_global = False
    db.session.commit()
    login()
    html = client.get("/leaderboard/global?view=heatmap").get_data(as_text=True)
    assert "opted out of the global leaderboard" in html


def test_global_is_total_only(client, login, db, trio):
    alice, bob, _ = trio
    rd = _round(db, 1)
    _score(db, alice, rd, 4)
    _score(db, bob, rd, 6)
    login()
    for view in ("total", "h2h", "heatmap"):
        html = client.get(f"/leaderboard/global?view={view}").get_data(as_text=True)
        assert "leaderboard-tabs" not in html
        assert "heatmap-card" not in html
        assert "pprp" in html


# --------------------------------------------------------------- landing page


def test_landing_lists_global_first_with_movement(client, login, db, trio):
    alice, bob, _ = trio
    _league(db, "Mates", "MATE01", alice, bob)
    r1, r2 = _round(db, 1), _round(db, 2)
    _score(db, alice, r1, 10)
    _score(db, bob, r1, 0)
    _score(db, alice, r2, 0)
    _score(db, bob, r2, 20)
    login()
    html = client.get("/leagues/").get_data(as_text=True)
    assert html.index("Global") < html.index("Mates")
    assert "2nd of 2" in html
    assert "movement--down" in html


def test_standings_movement(app, db, trio):
    from app.leaderboard.routes import user_standings
    alice, bob, stranger = trio
    _league(db, "Mates", "MATE01", alice, bob)
    r1 = _round(db, 1)
    _score(db, alice, r1, 10)
    _score(db, bob, r1, 0)
    # One scored round: no arrows yet.
    assert {s.movement for s in user_standings(bob)} == {None}

    r2 = _round(db, 2)
    _score(db, alice, r2, 0)
    _score(db, bob, r2, 20)
    mates = [s for s in user_standings(bob) if s.name == "Mates"][0]
    assert (mates.rank, mates.size, mates.movement, mates.position) == (1, 2, "up", "1st of 2")

    stranger.show_on_global = False
    db.session.commit()
    glob = user_standings(stranger)[0]
    assert glob.name == "Global" and glob.position == "Hidden" and glob.movement is None


def test_landing_without_private_leagues(client, make_user, login):
    make_user()
    login()
    html = client.get("/leagues/").get_data(as_text=True)
    assert "Global" in html
    assert "not in any private leagues" in html


# --------------------------------------------------------------- moved routes


def test_old_routes_redirect(client, make_user, login, db):
    user = make_user()
    league = _league(db, "L", "ABC123", user)
    login()
    r = client.get("/leaderboard/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["Location"].endswith("/leagues/")
    r = client.get(f"/leagues/{league.id}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/leaderboard/{league.id}")


def test_league_leaderboard_has_settings_and_share(client, make_user, login, db):
    user = make_user()
    league = _league(db, "L", "ABC123", user)
    login()
    html = client.get(f"/leaderboard/{league.id}").get_data(as_text=True)
    assert f"/leagues/{league.id}/settings" in html
    assert "data-share-league" in html
    assert "leaderboard-tabs" in html
    assert client.get(f"/leagues/{league.id}/settings").status_code == 200


def test_settings_blocks_non_member(client, make_user, login, db):
    other = make_user(email="o@example.com", username="other")
    league = _league(db, "P", "PRIV12", other)
    make_user()
    login()
    assert client.get(f"/leagues/{league.id}/settings").status_code == 404


@pytest.mark.parametrize("path", ["/leagues/join", "/leagues/new"])
def test_league_forms_link_back(client, make_user, login, path):
    make_user()
    login()
    html = client.get(path).get_data(as_text=True)
    assert 'class="back-link" href="/leagues/"' in html
