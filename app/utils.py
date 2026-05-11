"""Cross-blueprint helpers.

Pulled out here to avoid duplicating logic between the predictions form,
the round view, the friend's-view, and the admin overrides.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps

from flask import abort
from flask_login import current_user
from sqlalchemy.orm import joinedload

from app.api.jolpica import format_lap_time
from app.extensions import db
from app.models.driver import Driver, RoundDriver
from app.models.league import League, LeagueMembership
from app.models.prediction import (
    DnfCountPrediction,
    FastestLapPrediction,
    PlacesGainedPrediction,
    PoleTimePrediction,
    PredictionScore,
    PredictionType,
    QualiHeadToHeadPrediction,
    QualiNthPrediction,
    QualiRandomDriverPrediction,
    SpecialPrediction,
    Top3QualiPrediction,
    Top3SprintPrediction,
    Top10Prediction,
)
from app.models.special import SpecialOutcome
from app.models.result import SessionResult
from app.models.round import (
    Round,
    RoundState,
    Session,
    SessionStatus,
    SessionType,
    WeekendType,
)
from app.models.user import User


# =============================================================================
# Current round resolution
# =============================================================================


def get_current_round(season: int) -> Round | None:
    """The 'currently relevant' round.

    Picks the next upcoming round whose deadline is still in the future,
    falling back to the most recently completed/in-progress round if none.
    """
    now = datetime.now(timezone.utc)
    upcoming = (
        db.session.query(Round)
        .filter(Round.season == season,
                Round.predictions_deadline.is_not(None),
                Round.predictions_deadline > now)
        .order_by(Round.round_number.asc())
        .first()
    )
    if upcoming:
        return upcoming
    most_recent = (
        db.session.query(Round)
        .filter(Round.season == season)
        .order_by(Round.round_number.desc())
        .first()
    )
    return most_recent


def get_round_by_number(season: int, round_number: int) -> Round | None:
    return (
        db.session.query(Round)
        .filter(Round.season == season, Round.round_number == round_number)
        .one_or_none()
    )


def get_neighbour_rounds(
    round_obj: Round, *, locked_only: bool = False
) -> tuple[Round | None, Round | None]:
    """Return (previous, next) rounds in the same season, or (None, None).

    ``locked_only`` excludes unlocked rounds — used by the friend's view so
    navigation can't reveal unsubmitted predictions.
    """
    qprev = (
        db.session.query(Round)
        .filter(Round.season == round_obj.season,
                Round.round_number < round_obj.round_number)
    )
    qnxt = (
        db.session.query(Round)
        .filter(Round.season == round_obj.season,
                Round.round_number > round_obj.round_number)
    )
    if locked_only:
        qprev = qprev.filter(Round.predictions_locked.is_(True))
        qnxt = qnxt.filter(Round.predictions_locked.is_(True))
    previous = qprev.order_by(Round.round_number.desc()).first()
    nxt = qnxt.order_by(Round.round_number.asc()).first()
    return previous, nxt


# =============================================================================
# Country flag from ISO-2 code
# =============================================================================


def country_flag(iso2: str | None) -> str:
    """Return the regional-indicator flag emoji for an ISO-2 country code."""
    if not iso2 or len(iso2) != 2:
        return ""
    iso2 = iso2.upper()
    return chr(0x1F1E6 + ord(iso2[0]) - ord("A")) + chr(0x1F1E6 + ord(iso2[1]) - ord("A"))


# =============================================================================
# League access
# =============================================================================


def user_leagues(user_id: int) -> list[League]:
    return (
        db.session.query(League)
        .join(LeagueMembership, LeagueMembership.league_id == League.id)
        .filter(LeagueMembership.user_id == user_id)
        .order_by(League.name.asc()).all()
    )


def user_is_member(user_id: int, league_id: int) -> bool:
    return db.session.query(LeagueMembership).filter_by(
        user_id=user_id, league_id=league_id,
    ).first() is not None


def user_is_admin_of(user_id: int, league_id: int) -> bool:
    league = db.session.get(League, league_id)
    return league is not None and league.created_by_id == user_id


def assert_member(user_id: int, league_id: int) -> League:
    league = db.session.get(League, league_id)
    if league is None or not user_is_member(user_id, league_id):
        abort(404)
    return league


# =============================================================================
# Driver picker payload (for prediction form dropdowns)
# =============================================================================


@dataclass
class DriverChoice:
    driver_id: int
    label: str         # "VER · Verstappen (Red Bull)"
    car_number: int


@dataclass
class TeamChoice:
    name: str          # canonical team name (matches RoundDriver.constructor_name)
    label: str         # display label (currently same as name)


def round_team_choices(round_obj: Round) -> list[TeamChoice]:
    """Distinct constructor names for the round, alphabetically."""
    names: set[str] = {
        rd.constructor_name for rd in round_obj.round_drivers if rd.constructor_name
    }
    return [TeamChoice(name=n, label=n) for n in sorted(names)]


def round_driver_choices(round_obj: Round) -> list[DriverChoice]:
    """Build the driver-picker list for a round's prediction form."""
    rows = (
        db.session.query(RoundDriver)
        .options(joinedload(RoundDriver.expected_driver))
        .filter(RoundDriver.round_id == round_obj.id)
        .all()
    )
    out: list[DriverChoice] = []
    for rd in rows:
        d = rd.expected_driver
        code = d.code or d.driver_ref[:3].upper()
        constructor = f" ({rd.constructor_name})" if rd.constructor_name else ""
        out.append(DriverChoice(
            driver_id=d.id,
            label=f"{code} · {d.family_name}{constructor}",
            car_number=rd.car_number,
        ))
    out.sort(key=lambda c: c.label)
    return out


# =============================================================================
# Top mover — display helper for the round-detail "places gained" row
# =============================================================================


def compute_top_mover(round_obj: Round) -> tuple[Driver | None, int | None]:
    """Find the driver who gained the most places in this round's race.

    Returns (driver, places). Places can be negative if the field's biggest
    mover went backwards. Returns (None, None) if no race results or no
    grid data is available.

    Tie-break: better finish position wins the display slot.
    """
    race = next(
        (s for s in round_obj.sessions if s.session_type == SessionType.RACE),
        None,
    )
    if race is None or not race.results:
        return (None, None)
    classified = [
        r for r in race.results
        if r.is_classified and r.grid_position is not None
    ]
    if not classified:
        return (None, None)
    total = len(race.results)
    best_result: SessionResult | None = None
    best_gained: int | None = None
    for r in classified:
        grid = r.grid_position if r.grid_position != 0 else total
        gained = grid - r.position
        if (
            best_gained is None
            or gained > best_gained
            or (gained == best_gained and best_result is not None and r.position < best_result.position)
        ):
            best_gained = gained
            best_result = r
    if best_result is None:
        return (None, None)
    driver = db.session.get(Driver, best_result.actual_driver_id)
    return (driver, best_gained)


# =============================================================================
# Loading a user's predictions and scores for a round
# =============================================================================


@dataclass
class RoundUserState:
    """Everything the round-view template needs for one user's perspective."""
    round_obj: Round
    is_locked: bool
    deadline: datetime | None
    sessions: dict[SessionType, Session]
    drivers_by_id: dict[int, Driver]                 # for label rendering
    round_drivers: list[RoundDriver]
    # Predictions (any may be empty/None)
    top10: dict[int, Top10Prediction]
    quali_top3: dict[int, Top3QualiPrediction]
    sprint_top3: dict[int, Top3SprintPrediction]
    pole_time: PoleTimePrediction | None
    fastest_lap: FastestLapPrediction | None
    dnf_count: DnfCountPrediction | None
    places_gained: PlacesGainedPrediction | None
    quali_random_driver: QualiRandomDriverPrediction | None
    qh2h: "QualiHeadToHeadPrediction | None"
    qnth: "QualiNthPrediction | None"
    specials: dict[str, "SpecialPrediction"]
    special_outcomes: dict[str, "SpecialOutcome"]
    # Display helpers (populated for the round-detail view)
    top_mover_driver: Driver | None
    top_mover_places: int | None
    # Scores indexed by (kind, position-or-None) for non-special rows.
    scores: dict[tuple[PredictionType, int | None], PredictionScore]
    # Score rows for specials, keyed by special_key.
    special_scores: dict[str, PredictionScore]
    total_points: int


def _drivers_lookup(round_obj: Round) -> dict[int, Driver]:
    """Map driver_id → Driver for everyone who's ever been the regular for a
    car in this round, plus the actual drivers in any session results."""
    ids: set[int] = {rd.expected_driver_id for rd in round_obj.round_drivers}
    for s in round_obj.sessions:
        for r in s.results:
            ids.add(r.actual_driver_id)
    if not ids:
        return {}
    rows = db.session.query(Driver).filter(Driver.id.in_(ids)).all()
    return {d.id: d for d in rows}


def load_round_state(round_id: int, user_id: int) -> RoundUserState:
    """Materialise everything needed for the round view for this user."""
    rd = (
        db.session.query(Round)
        .options(
            joinedload(Round.sessions).joinedload(Session.results),
            joinedload(Round.round_drivers).joinedload(RoundDriver.expected_driver),
            joinedload(Round.random_quali_driver).joinedload(RoundDriver.expected_driver),
            joinedload(Round.qh2h_driver_a).joinedload(RoundDriver.expected_driver),
            joinedload(Round.qh2h_driver_b).joinedload(RoundDriver.expected_driver),
        )
        .filter(Round.id == round_id)
        .one_or_none()
    )
    if rd is None:
        abort(404)

    sessions = {s.session_type: s for s in rd.sessions}

    top10 = {p.position: p for p in db.session.query(Top10Prediction)
             .filter_by(user_id=user_id, round_id=round_id).all()}
    quali_top3 = {p.position: p for p in db.session.query(Top3QualiPrediction)
                  .filter_by(user_id=user_id, round_id=round_id).all()}
    sprint_top3 = {p.position: p for p in db.session.query(Top3SprintPrediction)
                   .filter_by(user_id=user_id, round_id=round_id).all()}
    pole_time = db.session.query(PoleTimePrediction).filter_by(
        user_id=user_id, round_id=round_id).one_or_none()
    fastest_lap = db.session.query(FastestLapPrediction).filter_by(
        user_id=user_id, round_id=round_id).one_or_none()
    dnf_count = db.session.query(DnfCountPrediction).filter_by(
        user_id=user_id, round_id=round_id).one_or_none()
    places_gained = db.session.query(PlacesGainedPrediction).filter_by(
        user_id=user_id, round_id=round_id).one_or_none()
    quali_random_driver = db.session.query(QualiRandomDriverPrediction).filter_by(
        user_id=user_id, round_id=round_id).one_or_none()
    qh2h = db.session.query(QualiHeadToHeadPrediction).filter_by(
        user_id=user_id, round_id=round_id).one_or_none()
    qnth = db.session.query(QualiNthPrediction).filter_by(
        user_id=user_id, round_id=round_id).one_or_none()
    specials_rows = db.session.query(SpecialPrediction).filter_by(
        user_id=user_id, round_id=round_id).all()
    specials = {p.special_key: p for p in specials_rows}
    outcomes_rows = db.session.query(SpecialOutcome).filter_by(
        round_id=round_id).all()
    special_outcomes = {o.special_key: o for o in outcomes_rows}

    score_rows = db.session.query(PredictionScore).filter_by(
        user_id=user_id, round_id=round_id,
    ).all()
    scores = {
        (s.kind, s.position): s for s in score_rows
        if s.kind != PredictionType.SPECIAL
    }
    special_scores = {
        s.special_key: s for s in score_rows
        if s.kind == PredictionType.SPECIAL and s.special_key is not None
    }
    total_points = sum(s.points for s in score_rows)

    top_mover_driver, top_mover_places = compute_top_mover(rd)

    return RoundUserState(
        round_obj=rd,
        is_locked=rd.predictions_locked,
        deadline=rd.predictions_deadline,
        sessions=sessions,
        drivers_by_id=_drivers_lookup(rd),
        round_drivers=list(rd.round_drivers),
        top10=top10, quali_top3=quali_top3, sprint_top3=sprint_top3,
        pole_time=pole_time,
        fastest_lap=fastest_lap, dnf_count=dnf_count,
        places_gained=places_gained,
        quali_random_driver=quali_random_driver,
        qh2h=qh2h, qnth=qnth,
        specials=specials,
        special_outcomes=special_outcomes,
        top_mover_driver=top_mover_driver,
        top_mover_places=top_mover_places,
        scores=scores, special_scores=special_scores,
        total_points=total_points,
    )


# =============================================================================
# Display helpers (used in templates via Jinja filters)
# =============================================================================


def driver_label(driver: Driver | None) -> str:
    if driver is None:
        return "—"
    code = driver.code or driver.driver_ref[:3].upper()
    return f"{code} · {driver.family_name}"


# Per-category bucket tables. Each list is (min_threshold, css_suffix),
# walked top-down — first match wins. Categories ending in negatives have
# the catch-all "neg" applied if no positive bucket matches.
_POINTS_BUCKETS: dict[str, list[tuple[int, str]]] = {
    "race_top10":          [(10, "p10"), (5, "p5"), (2, "p2"), (0, "p0")],
    "sprint_top3":         [(5, "p10"), (2, "p5"), (0, "p0")],
    "quali_top3":          [(5, "p10"), (2, "p5"), (1, "p2"), (0, "p0")],
    "quali_random_driver": [(5, "p10"), (2, "p5"), (1, "p2"), (0, "p0")],
    "quali_h2h":           [(5, "p10"), (0, "p0")],
    "quali_nth":           [(5, "p10"), (2, "p5"), (1, "p2"), (0, "p0")],
    "pole_time":           [(10, "p10"), (5, "p5"), (0, "p0")],
    "fastest_lap":         [(10, "p10"), (0, "p0")],
    "dnf_count":           [(10, "p10"), (5, "p5"), (0, "p0")],
    "places_gained":       [(5, "p10"), (1, "p5"), (0, "p0")],
    "special":             [(10, "p10"), (0, "p0")],
    # Generic fallback preserves old behaviour for any caller that doesn't
    # specify a category.
    "generic":             [(10, "p10"), (5, "p5"), (2, "p2"), (0, "p0")],
}
_CATEGORIES_WITH_NEGATIVE = {"quali_top3", "quali_random_driver", "quali_nth", "places_gained"}


def points_class(points: int | None, category: str = "generic") -> str:
    """CSS pill modifier for a points value within a category.

    Same point value renders different colours depending on category — e.g.
    +5 is "exact" (green) for sprint top 3 but "off-by-1" (yellow) for DNF
    count. Returns empty string for None to keep template branches simple.
    """
    if points is None:
        return ""
    cat = str(category)
    buckets = _POINTS_BUCKETS.get(cat, _POINTS_BUCKETS["generic"])
    for threshold, suffix in buckets:
        if points >= threshold:
            return f"pill pill--{suffix}"
    if cat in _CATEGORIES_WITH_NEGATIVE:
        return "pill pill--neg"
    return "pill pill--p0"


def format_pole_time_ms(ms: int | None) -> str:
    return format_lap_time(ms)


def session_status_class(status: SessionStatus | None) -> str:
    if status is None:
        return "pill pill--status-upcoming"
    return f"pill pill--status-{status.value.replace('_', '-')}"


def session_status_label(status: SessionStatus | None) -> str:
    """Human-readable label for a session status."""
    if status is None:
        return "scheduled"
    return {
        SessionStatus.UPCOMING: "scheduled",
        SessionStatus.IN_PROGRESS: "in progress",
        SessionStatus.PENDING_RESULTS: "results pending",
        SessionStatus.COMPLETED: "completed",
    }.get(status, status.value)


def local_time(dt: datetime | None, fmt: str = "%a %d %b %H:%M") -> str:
    """Format a UTC datetime in the configured local timezone.

    Used as a Jinja filter — render deadlines and session start times in
    the deployer's timezone rather than UTC.
    """
    if dt is None:
        return ""
    from flask import current_app
    tz = current_app.config["TIMEZONE"]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime(fmt)


def deadline_phrase(dt: datetime | None, threshold_hours: int = 24) -> str:
    """Render a deadline as a sentence-fitting phrase.

    Within ``threshold_hours``, returns a relative form ('in 3 hours');
    outside it, falls through to absolute local time prefixed with 'at'.
    Designed to slot into 'Locks ___'.
    """
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "(passed)"
    if secs < threshold_hours * 3600:
        if secs < 60:
            return "in under a minute"
        if secs < 3600:
            mins = int((secs + 59) // 60)
            return f"in {mins} minute{'s' if mins != 1 else ''}"
        hours = int(round(secs / 3600))
        return f"in {hours} hour{'s' if hours != 1 else ''}"
    return f"at {local_time(dt)}"


def round_status_summary(round_obj: Round, now: datetime | None = None) -> tuple[str, str]:
    """Return (label, css-class) for displaying a round in the season list."""
    now = now or datetime.now(timezone.utc)
    if round_obj.state == RoundState.COMPLETED:
        return ("completed", "pill pill--status-completed")
    if round_obj.state == RoundState.IN_PROGRESS:
        return ("live", "pill pill--status-in-progress")
    if round_obj.predictions_locked:
        return ("locked", "pill pill--status-pending")
    if round_obj.predictions_deadline and round_obj.predictions_deadline <= now:
        return ("locked", "pill pill--status-pending")
    if round_obj.predictions_deadline:
        return ("open", "pill pill--status-upcoming")
    return ("scheduled", "pill pill--status-upcoming")


def most_recent_visible_round(season: int) -> Round | None:
    """The most recent round whose predictions are visible to others
    (i.e. locked). Used as the landing for the friend's-view click-through
    from the leaderboard."""
    return (
        db.session.query(Round)
        .filter(Round.season == season, Round.predictions_locked.is_(True))
        .order_by(Round.round_number.desc())
        .first()
    )


# =============================================================================
# Decorators
# =============================================================================


def admin_required(view):
    """Require current_user.is_admin. Use after @login_required."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped
