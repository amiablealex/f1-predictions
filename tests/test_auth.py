"""Tests for auth flows."""
from __future__ import annotations

from datetime import timedelta

from app.extensions import db as _db
from app.models.user import PasswordResetToken, User


# --------------------------------------------------------------- register


def test_register_creates_user_and_logs_in(client, db):
    response = client.post(
        "/auth/register",
        data={
            "email": "new@example.com",
            "username": "new_user",
            "password": "supersecret1",
            "confirm_password": "supersecret1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    user = db.session.query(User).filter_by(email="new@example.com").one()
    assert user.username == "new_user"
    assert user.check_password("supersecret1")


def test_register_rejects_duplicate_email(client, make_user):
    make_user(email="dup@example.com", username="first")
    response = client.post(
        "/auth/register",
        data={
            "email": "dup@example.com",
            "username": "second",
            "password": "supersecret1",
            "confirm_password": "supersecret1",
        },
    )
    assert response.status_code == 200
    assert b"already exists" in response.data


def test_register_rejects_duplicate_username(client, make_user):
    make_user(email="a@example.com", username="taken")
    response = client.post(
        "/auth/register",
        data={
            "email": "b@example.com",
            "username": "taken",
            "password": "supersecret1",
            "confirm_password": "supersecret1",
        },
    )
    assert response.status_code == 200
    assert b"taken" in response.data


def test_register_rejects_password_mismatch(client):
    response = client.post(
        "/auth/register",
        data={
            "email": "c@example.com",
            "username": "userc",
            "password": "supersecret1",
            "confirm_password": "different1",
        },
    )
    assert response.status_code == 200
    assert b"don&#39;t match" in response.data or b"Passwords don" in response.data


def test_register_rejects_password_without_digit(client):
    response = client.post(
        "/auth/register",
        data={
            "email": "nodigit@example.com",
            "username": "nodigit",
            "password": "alllowercase",
            "confirm_password": "alllowercase",
        },
    )
    assert response.status_code == 200
    assert b"digit" in response.data or b"number" in response.data


def test_register_rejects_short_password(client):
    response = client.post(
        "/auth/register",
        data={
            "email": "d@example.com",
            "username": "userd",
            "password": "short",
            "confirm_password": "short",
        },
    )
    assert response.status_code == 200
    assert b"At least 8" in response.data


# --------------------------------------------------------------- login


def test_login_success(client, make_user, login):
    make_user()
    response = login()
    assert response.status_code == 302


def test_login_wrong_password(client, make_user, login):
    make_user()
    response = login(password="wrongwrong")
    assert response.status_code == 401
    assert b"incorrect" in response.data


def test_login_unknown_email(client, login):
    response = login(email="nobody@example.com")
    assert response.status_code == 401


def test_login_open_redirect_blocked(client, make_user):
    make_user()
    response = client.post(
        "/auth/login?next=https://evil.example.com/x",
        data={"email": "alice@example.com", "password": "password123", "remember": "y"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    # Should NOT redirect to external host
    assert "evil.example.com" not in response.headers.get("Location", "")


def test_logout_requires_login(client):
    response = client.post("/auth/logout", follow_redirects=False)
    # Flask-Login redirects unauthenticated requests to the login view
    assert response.status_code == 302


# --------------------------------------------------- forgot / reset password


def test_forgot_password_creates_token_for_existing_user(client, make_user, db):
    make_user(email="reset@example.com")
    response = client.post(
        "/auth/forgot-password",
        data={"email": "reset@example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    tokens = db.session.query(PasswordResetToken).all()
    assert len(tokens) == 1
    assert tokens[0].user.email == "reset@example.com"


def test_forgot_password_silent_for_unknown_email(client, db):
    response = client.post(
        "/auth/forgot-password",
        data={"email": "noone@example.com"},
        follow_redirects=False,
    )
    # Same redirect either way — no enumeration.
    assert response.status_code == 302
    assert db.session.query(PasswordResetToken).count() == 0


def test_reset_password_with_valid_token(client, make_user, db):
    user = make_user(email="r@example.com")
    token = PasswordResetToken.issue(user=user, ttl_hours=2)
    db.session.add(token)
    db.session.commit()

    response = client.post(
        f"/auth/reset-password/{token.token}",
        data={"password": "newpassword1", "confirm_password": "newpassword1"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    refreshed = db.session.get(User, user.id)
    assert refreshed.check_password("newpassword1")
    refreshed_token = db.session.get(PasswordResetToken, token.id)
    assert refreshed_token.used_at is not None


def test_reset_password_token_is_single_use(client, make_user, db):
    user = make_user(email="single@example.com")
    token = PasswordResetToken.issue(user=user, ttl_hours=2)
    db.session.add(token)
    db.session.commit()

    # First use succeeds
    client.post(
        f"/auth/reset-password/{token.token}",
        data={"password": "firstpass1", "confirm_password": "firstpass1"},
    )
    # Second use is rejected
    response = client.post(
        f"/auth/reset-password/{token.token}",
        data={"password": "secondpass1", "confirm_password": "secondpass1"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    refreshed = db.session.get(User, user.id)
    assert refreshed.check_password("firstpass1")
    assert not refreshed.check_password("secondpass1")


def test_reset_password_expired_token_rejected(client, make_user, db):
    user = make_user(email="exp@example.com")
    token = PasswordResetToken.issue(user=user, ttl_hours=2)
    # Force expiry
    from datetime import datetime, timezone
    token.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.session.add(token)
    db.session.commit()

    response = client.get(f"/auth/reset-password/{token.token}", follow_redirects=False)
    assert response.status_code == 302
    # Lands on forgot-password page.
    assert "/forgot-password" in response.headers.get("Location", "")


# --------------------------------------------------------------- account


def test_change_username(client, make_user, login, db):
    user = make_user()
    login()
    response = client.post(
        "/auth/account/username",
        data={"username": "alice2"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert db.session.get(User, user.id).username == "alice2"


def test_change_username_rejects_duplicate(client, make_user, login, db):
    make_user(email="bob@example.com", username="bob")
    user = make_user()  # alice
    login()
    response = client.post(
        "/auth/account/username",
        data={"username": "bob"},
        follow_redirects=True,
    )
    assert b"taken" in response.data
    assert db.session.get(User, user.id).username == "alice"


def test_change_email_requires_current_password(client, make_user, login, db):
    user = make_user()
    login()
    response = client.post(
        "/auth/account/email",
        data={"email": "newemail@example.com", "current_password": "wrong"},
        follow_redirects=True,
    )
    assert b"incorrect" in response.data
    assert db.session.get(User, user.id).email == "alice@example.com"


def test_change_password_requires_current_password(client, make_user, login, db):
    user = make_user()
    login()
    response = client.post(
        "/auth/account/password",
        data={
            "current_password": "wrong",
            "new_password": "brandnew1",
            "confirm_password": "brandnew1",
        },
        follow_redirects=True,
    )
    assert b"incorrect" in response.data
    assert db.session.get(User, user.id).check_password("password123")


def test_change_password_success(client, make_user, login, db):
    user = make_user()
    login()
    response = client.post(
        "/auth/account/password",
        data={
            "current_password": "password123",
            "new_password": "brandnew1",
            "confirm_password": "brandnew1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert db.session.get(User, user.id).check_password("brandnew1")


def test_delete_account_requires_phrase_and_password(client, make_user, login, db):
    user = make_user()
    login()
    # Wrong phrase
    client.post(
        "/auth/account/delete",
        data={"current_password": "password123", "confirm_phrase": "delete"},
    )
    assert db.session.get(User, user.id) is not None

    # Wrong password
    client.post(
        "/auth/account/delete",
        data={"current_password": "wrong", "confirm_phrase": "delete my account"},
    )
    assert db.session.get(User, user.id) is not None

    # Correct
    response = client.post(
        "/auth/account/delete",
        data={"current_password": "password123", "confirm_phrase": "delete my account"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert db.session.get(User, user.id) is None
