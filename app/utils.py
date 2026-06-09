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
from sqlalchemy import func

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
from app.models.contribution import ContributionDefinition, ContributionPrediction
from app.round_display import (
    ActualDisplay,
    actual_for_dnf_count,
    actual_for_fastest_lap,
    actual_for_h2h,
    actual_for_special,
    actual_places_gained_for_pick,
    actual_position_for_pick,
)
from app.specials import SPECIALS_BY_KEY
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


def get_active_round(season: int) -> Round | None:
    """The currently 'active' round: predictions locked, not yet completed.

    Used by the landing page to send users to where the action is, and
    by the predictions form to surface a banner pointing back here.
    """
    return (
        db.session.query(Round)
        .filter(
            Round.season == season,
            Round.predictions_locked.is_(True),
            Round.state != RoundState.COMPLETED,
        )
        .order_by(Round.round_number.desc())
        .first()
    )


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


def _build_actuals(
    rd: Round,
    sessions: dict[SessionType, Session],
    round_drivers: list[RoundDriver],
    drivers_by_id: dict[int, Driver],
    top10: dict[int, Top10Prediction],
    quali_top3: dict[int, Top3QualiPrediction],
    sprint_top3: dict[int, Top3SprintPrediction],
    qnth: QualiNthPrediction | None,
    qh2h: QualiHeadToHeadPrediction | None,
    fastest_lap: FastestLapPrediction | None,
    dnf_count_pred: DnfCountPrediction | None,
    places_gained: PlacesGainedPrediction | None,
    quali_random_driver: QualiRandomDriverPrediction | None,
    specials: dict[str, SpecialPrediction],
    special_outcomes: dict[str, SpecialOutcome],
) -> tuple[
    dict[tuple[PredictionType, int | None], ActualDisplay],
    dict[str, ActualDisplay],
    bool,
]:
    """Pre-compute every 'actual outcome' cell.

    Returns (actuals, special_actuals, any_substitution_flag).
    """
    actuals: dict[tuple[PredictionType, int | None], ActualDisplay] = {}
    special_actuals: dict[str, ActualDisplay] = {}
    any_sub = False

    race = sessions.get(SessionType.RACE)
    quali = sessions.get(SessionType.QUALIFYING)
    sprint = sessions.get(SessionType.SPRINT_RACE)

    def _add(key, ad):
        nonlocal any_sub
        if ad is None:
            return
        actuals[key] = ad
        if ad.substituted:
            any_sub = True

    for pos, pred in top10.items():
        _add(
            (PredictionType.RACE_TOP10, pos),
            actual_position_for_pick(
                pred.predicted_driver_id, pos, race, round_drivers,
            ),
        )
    for pos, pred in quali_top3.items():
        _add(
            (PredictionType.QUALI_TOP3, pos),
            actual_position_for_pick(
                pred.predicted_driver_id, pos, quali, round_drivers,
            ),
        )
    for pos, pred in sprint_top3.items():
        _add(
            (PredictionType.SPRINT_TOP3, pos),
            actual_position_for_pick(
                pred.predicted_driver_id, pos, sprint, round_drivers,
            ),
        )

    if qnth is not None and rd.quali_nth_position is not None:
        _add(
            (PredictionType.QUALI_NTH, None),
            actual_position_for_pick(
                qnth.predicted_driver_id,
                rd.quali_nth_position,
                quali,
                round_drivers,
            ),
        )

    if quali_random_driver is not None and rd.random_quali_driver is not None:
        _add(
            (PredictionType.QUALI_RANDOM_DRIVER, None),
            actual_position_for_pick(
                rd.random_quali_driver.expected_driver_id,
                quali_random_driver.predicted_position,
                quali,
                round_drivers,
            ),
        )

    if places_gained is not None:
        _add(
            (PredictionType.PLACES_GAINED, None),
            actual_places_gained_for_pick(
                places_gained.predicted_driver_id, race, round_drivers,
            ),
        )

    # H2H — populate regardless of whether the user submitted a pick,
    # so the cell renders the winner once quali completes.
    _add(
        (PredictionType.QUALI_HEAD_TO_HEAD, None),
        actual_for_h2h(
            qh2h.predicted_driver_id if qh2h is not None else None,
            rd.qh2h_driver_a, rd.qh2h_driver_b,
            quali, drivers_by_id,
        ),
    )

    # Fastest lap — same: render the actual setter regardless.
    _add(
        (PredictionType.FASTEST_LAP, None),
        actual_for_fastest_lap(
            fastest_lap.predicted_driver_id if fastest_lap is not None else None,
            race, round_drivers, drivers_by_id,
        ),
    )

    # DNF count — render the actual once we have it.
    _add(
        (PredictionType.DNF_COUNT, None),
        actual_for_dnf_count(
            dnf_count_pred.predicted_count if dnf_count_pred is not None else None,
            race.dnf_count if race is not None else None,
        ),
    )

    # Specials — one entry per active special on the round.
    for key in (rd.special_a_key, rd.special_b_key):
        if not key:
            continue
        sp = SPECIALS_BY_KEY.get(key)
        if sp is None:
            continue
        ad = actual_for_special(
            sp, specials.get(key), special_outcomes.get(key), drivers_by_id,
        )
        if ad is not None:
            special_actuals[key] = ad

    return actuals, special_actuals, any_sub

def _build_contribution_actuals(
    contribution_defs: list,
    contribution_preds: dict,
    drivers_by_id: dict[int, Driver],
) -> dict[int, ActualDisplay]:
    """Pre-compute the actual cell for each wildcard on the round."""
    from app.round_display import actual_for_contribution
    out: dict[int, ActualDisplay] = {}
    for d in contribution_defs:
        ad = actual_for_contribution(
            d, contribution_preds.get(d.id), drivers_by_id,
        )
        if ad is not None:
            out[d.id] = ad
    return out

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

    # ---- Wildcards (contributions) ----
    contribution_defs = (
        db.session.query(ContributionDefinition)
        .filter_by(round_id=round_id)
        .order_by(ContributionDefinition.created_at.asc())
        .all()
    )
    contribution_preds = {
        cp.contribution_id: cp for cp in db.session.query(ContributionPrediction)
        .join(ContributionDefinition,
              ContributionDefinition.id == ContributionPrediction.contribution_id)
        .filter(ContributionDefinition.round_id == round_id,
                ContributionPrediction.user_id == user_id)
        .all()
    }

    score_rows = db.session.query(PredictionScore).filter_by(
        user_id=user_id, round_id=round_id,
    ).all()
    scores = {
        (s.kind, s.position): s for s in score_rows
        if s.kind not in (PredictionType.SPECIAL, PredictionType.CONTRIBUTION)
    }
    special_scores = {
        s.special_key: s for s in score_rows
        if s.kind == PredictionType.SPECIAL and s.special_key is not None
    }
    contribution_scores = {
        s.contribution_id: s for s in score_rows
        if s.kind == PredictionType.CONTRIBUTION and s.contribution_id is not None
    }
    total_points = sum(s.points for s in score_rows)

    round_drivers_list = list(rd.round_drivers)
    drivers_by_id = _drivers_lookup(rd)
    actuals, special_actuals, any_sub = _build_actuals(
        rd, sessions, round_drivers_list, drivers_by_id,
        top10, quali_top3, sprint_top3,
        qnth, qh2h, fastest_lap, dnf_count,
        places_gained, quali_random_driver,
        specials, special_outcomes,
    )

    contribution_actuals = _build_contribution_actuals(
        contribution_defs, contribution_preds, drivers_by_id,
    )

    # Per-phase point sums. None when no rows for that phase exist yet
    # (phase not scored); 0 when scored to 0 points.
    _sprint_kinds = {PredictionType.SPRINT_TOP3}
    _quali_kinds = {
        PredictionType.QUALI_TOP3, PredictionType.POLE_TIME,
        PredictionType.QUALI_RANDOM_DRIVER, PredictionType.QUALI_HEAD_TO_HEAD,
        PredictionType.QUALI_NTH,
    }
    _race_kinds = {
        PredictionType.RACE_TOP10, PredictionType.FASTEST_LAP,
        PredictionType.DNF_COUNT, PredictionType.PLACES_GAINED,
        PredictionType.SPECIAL,
    }
    sprint_points: int | None = None
    quali_points: int | None = None
    race_points: int | None = None
    contribution_points: int | None = None
    for row in score_rows:
        if row.kind in _sprint_kinds:
            sprint_points = (sprint_points or 0) + row.points
        elif row.kind in _quali_kinds:
            quali_points = (quali_points or 0) + row.points
        elif row.kind in _race_kinds:
            race_points = (race_points or 0) + row.points
        elif row.kind == PredictionType.CONTRIBUTION:
            contribution_points = (contribution_points or 0) + row.points

    avg, highest = round_average_and_highest(round_id)

    return RoundUserState(
        round_obj=rd,
        is_locked=rd.predictions_locked,
        deadline=rd.predictions_deadline,
        sessions=sessions,
        drivers_by_id=drivers_by_id,
        round_drivers=round_drivers_list,
        top10=top10, quali_top3=quali_top3, sprint_top3=sprint_top3,
        pole_time=pole_time,
        fastest_lap=fastest_lap, dnf_count=dnf_count,
        places_gained=places_gained,
        quali_random_driver=quali_random_driver,
        qh2h=qh2h, qnth=qnth,
        specials=specials,
        special_outcomes=special_outcomes,
        actuals=actuals,
        special_actuals=special_actuals,
        contribution_defs=contribution_defs,
        contribution_preds=contribution_preds,
        contribution_actuals=contribution_actuals,
        contribution_scores=contribution_scores,
        contribution_points=contribution_points,
        any_substitution=any_sub,
        sprint_points=sprint_points,
        quali_points=quali_points,
        race_points=race_points,
        scores=scores, special_scores=special_scores,
        total_points=total_points,
        average_total_points=avg,
        highest_total_points=highest,
    )


def round_average_and_highest(round_id: int) -> tuple[int | None, int | None]:
    """Mean and max PredictionScore totals for this round, computed
    across users who actually scored (sum > 0). Returns (None, None)
    when nobody qualifies. One query, two reference points used by the
    round-view header trio.
    """
    user_totals = (
        db.session.query(
            PredictionScore.user_id,
            func.coalesce(func.sum(PredictionScore.points), 0),
        )
        .filter(PredictionScore.round_id == round_id)
        .group_by(PredictionScore.user_id)
        .all()
    )
    scores = [int(total) for _, total in user_totals if int(total) > 0]
    if not scores:
        return (None, None)
    return (round(sum(scores) / len(scores)), max(scores))


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
    # Pre-computed "actual outcome" cells for non-special prediction kinds.
    # Keyed by (kind, position-or-None) — same shape as `scores`. Entries
    # are absent when results aren't in yet; the template renders an
    # empty cell in that case.
    actuals: dict[tuple[PredictionType, int | None], ActualDisplay]
    # Specials get their own dict, keyed by special_key.
    special_actuals: dict[str, ActualDisplay]
    # True iff any computed actual involved a substitute driver — drives
    # the footnote at the bottom of the round page.
    any_substitution: bool
    # Per-phase point sums. None when the phase hasn't been scored yet
    # (no rows for that phase exist); 0 when scored but no points earned.
    sprint_points: int | None
    quali_points: int | None
    race_points: int | None
    # Scores indexed by (kind, position-or-None) for non-special rows.
    scores: dict[tuple[PredictionType, int | None], PredictionScore]
    # Score rows for specials, keyed by special_key.
    special_scores: dict[str, PredictionScore]
    contribution_defs: list                       # list[ContributionDefinition]
    contribution_preds: dict                       # {contribution_id: ContributionPrediction}
    contribution_actuals: dict                     # {contribution_id: ActualDisplay}
    contribution_scores: dict                      # {contribution_id: PredictionScore}
    contribution_points: int | None
    total_points: int
    # Reference points from across the app for the round-view header trio.
    # Both None when nobody has any score for this round yet.
    average_total_points: int | None
    highest_total_points: int | None


def _drivers_lookup(round_obj: Round) -> dict[int, Driver]:
    """Map driver_id → Driver for everyone who's ever been the regular for a
    car in this round, plus the actual drivers in any session results."""
    ids: set[int] = {rd.expected_driver_id for rd in round_obj.round_drivers}
    for s in round_obj.sessions:
        for r in s.results:
            ids.add(r.actual_driver_id)
    # Wildcard driver picks + actuals for this round.
    for d in db.session.query(ContributionDefinition).filter_by(round_id=round_obj.id).all():
        if d.actual_driver_id:
            ids.add(d.actual_driver_id)
        if d.allowed_driver_ids:
            ids.update(d.allowed_driver_ids)
    for cp in (
        db.session.query(ContributionPrediction)
        .join(ContributionDefinition, ContributionDefinition.id == ContributionPrediction.contribution_id)
        .filter(ContributionDefinition.round_id == round_obj.id)
        .all()
    ):
        if cp.predicted_driver_id:
            ids.add(cp.predicted_driver_id)
    if not ids:
        return {}
    rows = db.session.query(Driver).filter(Driver.id.in_(ids)).all()
    return {d.id: d for d in rows}


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

def contributor_required(view):
    """Require current_user.is_contributor. Use after @login_required."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_contributor:
            abort(403)
        return view(*args, **kwargs)
    return wrapped

def contributor_or_admin_required(view):
    """Require current_user.is_contributor OR is_admin. Used for routes an
    admin may reach in their oversight capacity (the contributions overview
    and the actual-submission safety net) without being a contributor."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not (
            current_user.is_contributor or current_user.is_admin
        ):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


# =============================================================================
# Contribution Helpers
# =============================================================================


def contribution_edit_cutoff(round_obj: Round) -> datetime | None:
    """The instant after which a round's wildcard definitions lock.

    Local midnight (00:00) of the predictions deadline's calendar date,
    returned as UTC. Deadline 20 Jun 14:00 local → cutoff 20 Jun 00:00
    local → editable through 19 Jun 23:59:59. None if no deadline set.
    """
    if round_obj.predictions_deadline is None:
        return None
    from flask import current_app
    tz = current_app.config["TIMEZONE"]
    deadline = round_obj.predictions_deadline
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    local_date = deadline.astimezone(tz).date()
    local_midnight = datetime(
        local_date.year, local_date.month, local_date.day, tzinfo=tz,
    )
    return local_midnight.astimezone(timezone.utc)


def contribution_window_open(round_obj: Round, now: datetime | None = None) -> bool:
    """True if a contributor may still create/edit/delete a definition for
    this round (i.e. before the day-before cutoff)."""
    cutoff = contribution_edit_cutoff(round_obj)
    if cutoff is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now < cutoff


def contribution_prediction_count(contribution_id: int) -> int:
    """How many users have entered a prediction for this wildcard."""
    return (
        db.session.query(ContributionPrediction)
        .filter_by(contribution_id=contribution_id)
        .count()
    )

def heatmap_band(points: int) -> str:
    """Map a round score to a heatmap band name (config-driven).

    Bands are checked top to bottom; the first whose threshold is met wins.
    A threshold of None is the catch-all, covering 0 and any negative."""
    from flask import current_app
    for threshold, band in current_app.config["HEATMAP_THRESHOLDS"]:
        if threshold is None or points >= threshold:
            return band
    return current_app.config["HEATMAP_THRESHOLDS"][-1][1]
