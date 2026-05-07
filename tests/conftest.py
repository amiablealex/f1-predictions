"""Pytest fixtures.

Strategy:
  - One session-scoped Flask app, configured for testing (CSRF disabled).
  - Schema is created once per session and dropped at the end.
  - Each test gets a fresh transaction that's rolled back at the end via
    the `db` fixture, giving full isolation between tests.
"""
from __future__ import annotations

import os

import pytest

# Force testing config before importing the app.
os.environ.setdefault("FLASK_ENV", "testing")

from app import create_app
from app.config import TestingConfig
from app.extensions import db as _db
from app.models.user import User


@pytest.fixture(scope="session")
def app():
    application = create_app(TestingConfig)
    with application.app_context():
        _db.drop_all()
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    """Yield the SQLAlchemy `db` and clear all rows between tests.

    Postgres + SQLAlchemy 2.x makes nested-transaction rollback patterns
    fragile across connections. For a small test suite, the simplest
    reliable approach is to truncate all tables between tests.
    """
    with app.app_context():
        yield _db
        _db.session.rollback()
        # Clear data without dropping schema (faster than drop+create).
        for table in reversed(_db.metadata.sorted_tables):
            _db.session.execute(table.delete())
        _db.session.commit()


# --------------------------------------------------------------- helpers


@pytest.fixture
def make_user(db):
    """Factory fixture: create a user with sensible defaults."""

    def _make(email="alice@example.com", username="alice", password="password123", is_admin=False):
        user = User(email=email, username=username, is_admin=is_admin)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user

    return _make


@pytest.fixture
def login(client):
    """Helper that logs a user in via the real login form."""

    def _login(email="alice@example.com", password="password123"):
        return client.post(
            "/auth/login",
            data={"email": email, "password": password, "remember": "y"},
            follow_redirects=False,
        )

    return _login
