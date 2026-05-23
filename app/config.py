"""Application configuration.

All customisable values live here. Anything that might be tweaked across
environments is sourced from the environment with a sensible default.

Important: scoring point values defined here represent the CURRENT default.
When a round is created, these values are *snapshotted* into the
`round_scoring_config` table so that changing values mid-season does not
retroactively alter past leaderboards. See app/models/round.py.
"""
from __future__ import annotations

import os
from datetime import timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Load .env if present (no-op in production where env is set by Railway).
load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


class Config:
    # -------------------------------------------------------------------------
    # Flask core
    # -------------------------------------------------------------------------
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    FLASK_ENV = os.environ.get("FLASK_ENV", "production")
    DEBUG = FLASK_ENV == "development"
    TESTING = False

    # Sessions / security
    SESSION_COOKIE_SECURE = not DEBUG
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = timedelta(days=30)

    # -------------------------------------------------------------------------
    # Database
    # Railway provides `DATABASE_URL`. SQLAlchemy 2.x requires the
    # "postgresql://" scheme rather than the legacy "postgres://" some
    # providers emit, so normalise it here.
    # -------------------------------------------------------------------------
    _raw_db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://f1user:f1pass@localhost:5432/f1predictions",
    )
    if _raw_db_url.startswith("postgres://"):
        _raw_db_url = _raw_db_url.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = _raw_db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    # -------------------------------------------------------------------------
    # Email (Resend) — only used for password reset.
    # -------------------------------------------------------------------------
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
    RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "noreply@example.com")
    RESEND_FROM_NAME = os.environ.get("RESEND_FROM_NAME", "F1 Predictions")
    APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000")
    PASSWORD_RESET_TOKEN_TTL_HOURS = 2

    # -------------------------------------------------------------------------
    # Jolpica F1 API
    # -------------------------------------------------------------------------
    JOLPICA_BASE_URL = os.environ.get(
        "JOLPICA_BASE_URL", "https://api.jolpi.ca/ergast/f1"
    ).rstrip("/")
    JOLPICA_USER_AGENT = os.environ.get(
        "JOLPICA_USER_AGENT", "f1-predictions/1.0"
    )
    # Conservative client-side rate limit. Jolpica is 4 req/s, 500/hr.
    JOLPICA_MIN_REQUEST_INTERVAL_SECONDS = 0.3
    JOLPICA_REQUEST_TIMEOUT_SECONDS = 15

    # -------------------------------------------------------------------------
    # Season + scheduler
    # -------------------------------------------------------------------------
    F1_SEASON = _env_int("F1_SEASON", 2026)
    TIMEZONE = ZoneInfo(os.environ.get("SCHEDULER_TIMEZONE", "Europe/London"))

    # How long before the first scoring event predictions lock.
    DEADLINE_OFFSET_MINUTES = 60

    # Worker cadences
    RESULTS_POLL_INTERVAL_MINUTES = _env_int("RESULTS_POLL_INTERVAL_MINUTES", 5)
    SCHEDULE_SYNC_INTERVAL_HOURS = _env_int("SCHEDULE_SYNC_INTERVAL_HOURS", 12)
    RESULTS_PENDING_TIMEOUT_HOURS = _env_int("RESULTS_PENDING_TIMEOUT_HOURS", 6)

    # Estimated session durations — used to transition status from
    # `in_progress` to `pending_results`.
    SESSION_DURATION_MINUTES = {
        "sprint_quali": 50,
        "sprint_race": 45,
        "qualifying": 70,
        "race": 130,
    }

    # -------------------------------------------------------------------------
    # Scoring — CURRENT default values. Snapshot per round on round creation.
    # Edit here to change defaults for FUTURE rounds only.
    # -------------------------------------------------------------------------
    SCORING_DEFAULTS = {
        # Race top 10 — per slot
        "race_top10_correct": 10,
        "race_top10_one_off": 5,
        "race_top10_two_off": 2,
        # Qualifying top 3 + random driver — bucketed scoring (shared scheme).
        # Ordered most-precise-first; the first matching bucket wins. Last
        # bucket is the catch-all for very-far-off guesses.
        "quali_position_buckets": [
            {"max_delta": 0, "points": 5},
            {"max_delta": 1, "points": 2},
            {"max_delta": 2, "points": 1},
            {"max_delta": 5, "points": 0},
            {"max_delta": 8, "points": -2},
            {"max_delta": 999, "points": -5},
        ],
        # Pole lap time (qualifying) — proximity buckets, awarded once
        # The list is ordered most-precise first; the first matching bucket wins.
        "pole_time_buckets": [
            {"within_seconds": 0.2, "points": 10},
            {"within_seconds": 1.0, "points": 5},
        ],
        # Sprint qualifying is deadline-only (no scoring) — Jolpica has no
        # sprint qualifying endpoint, so we keep the session for timing
        # purposes and skip scoring of it.
        # Sprint race top 3 — per slot
        "sprint_top3_correct": 5,
        "sprint_top3_one_off": 2,
        # Fastest lap (main race) — flat
        "fastest_lap_correct": 10,
        # DNF count (main race) — proximity
        "dnf_count_correct": 10,
        "dnf_count_one_off": 5,
        # Quali head-to-head — binary
        "qh2h_correct": 5,
        # Specials (RACE phase) — one entry per bank item
        "special_first_retirement": 10,
        "special_most_pitstops": 10,
        "special_last_classified": 10,
        "special_margin_of_victory": 10,
        "special_lap_of_first_pitstop": 10,
        "special_pole_sitter_wins": 10,
        "special_longest_stint": 10,
        "special_biggest_team_gap": 10,
    }

    # -------------------------------------------------------------------------
    # League invite codes
    # -------------------------------------------------------------------------
    INVITE_CODE_LENGTH = 6
    INVITE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I

    # -------------------------------------------------------------------------
    # UI palette (hex, used in Jinja templates and CSS variables).
    # Centralised here so styling is consistent and tweakable.
    # -------------------------------------------------------------------------
    PALETTE = {
        "background": "#f3ecd9",        # parchment
        "surface": "#fbf6e8",           # lighter parchment for cards
        "surface_alt": "#ede4c9",       # subtle alt fill
        "ink": "#2c2a26",               # primary text
        "ink_muted": "#6f6a5e",         # secondary text
        "border": "#d6cdb1",            # hairline borders
        "accent": "#8b1f1f",            # F1-ish red, used very sparingly
        # Points scale
        "points_10": "#4f6b3a",         # deep moss green
        "points_5":  "#b8862c",         # muted ochre
        "points_2":  "#5d7088",         # soft slate blue
        "points_0":  "#9c968a",         # dim grey
        "points_neg": "#a85a4d",         # muted brick red
        # Status indicators
        "status_upcoming": "#6f6a5e",
        "status_in_progress": "#b8862c",
        "status_pending": "#8a7d52",
        "status_completed": "#4f6b3a",
    }

    # Hard cap on items per leaderboard page (small group app, not paginated heavily)
    LEADERBOARD_PAGE_SIZE = 100


class DevelopmentConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False


class TestingConfig(Config):
    TESTING = True
    DEBUG = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://f1user:f1pass@localhost:5432/f1predictions_test",
    )
    SECRET_KEY = "test-secret"


class ProductionConfig(Config):
    DEBUG = False


def get_config() -> type[Config]:
    """Return the config class appropriate to FLASK_ENV."""
    env = os.environ.get("FLASK_ENV", "production").lower()
    if env == "development":
        return DevelopmentConfig
    if env == "testing":
        return TestingConfig
    return ProductionConfig
