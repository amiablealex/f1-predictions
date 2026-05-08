"""Round-view blueprint.

Read-only views of rounds:
  - /results                              season list (yours)
  - /results/u/<username>                 season list (a friend's, locked rounds only)
  - /round/current                        redirect to current round detail
  - /round/<season>/<round>               round detail (yours)
  - /round/<season>/<round>/u/<username>  round detail (a friend's, locked only)

Friend variants enforce two preconditions: the viewer and friend share at
least one league, and the round (or rounds) must be locked. Friend lists
are pre-filtered to locked-only so navigation can't reveal an unsubmitted
prediction.
"""
from __future__ import annotations

from dataclasses import dataclass

from flask import Blueprint, abort, current_app, redirect, render_template, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from app.extensions import db
from app.models.prediction import PredictionScore
from app.models.round import Round, RoundState
from app.models.user import User
from app.utils import (
    get_current_round,
    get_neighbour_rounds,
    get_round_by_number,
    load_round_state,
    round_status_summary,
    user_leagues,
)

rounds_bp = Blueprint("rounds", __name__, template_folder="../templates")


# =============================================================================
# Helpers
# =============================================================================


@dataclass
class RoundListEntry:
    round: Round
    status_label: str
    status_class: str
    points: int | None  # None means "not yet scored" (don't show 0)


def _build_list_rows(season: int, user_id: int) -> list[RoundListEntry]:
    """Build the rows for the season list.

    Only locked rounds appear — an unlocked round has nothing meaningful
    to show (no scores, predictions still editable). Ordered newest first
    so the most relevant round is at the top.
    """
    rounds = (
        db.session.query(Round)
        .filter(Round.season == season,
                Round.predictions_locked.is_(True))
        .order_by(Round.round_number.desc())
        .all()
    )
    if not rounds:
        return []

    round_ids = [r.id for r in rounds]
    sums = dict(
        db.session.query(PredictionScore.round_id, func.sum(PredictionScore.points))
        .filter(PredictionScore.user_id == user_id,
                PredictionScore.round_id.in_(round_ids))
        .group_by(PredictionScore.round_id)
        .all()
    )

    out: list[RoundListEntry] = []
    for r in rounds:
        label, css = round_status_summary(r)
        if r.state in (RoundState.IN_PROGRESS, RoundState.COMPLETED):
            points = int(sums.get(r.id, 0) or 0)
        else:
            points = None
        out.append(RoundListEntry(
            round=r,
            status_label=label,
            status_class=css,
            points=points,
        ))
    return out


def _users_share_a_league(a_id: int, b_id: int) -> bool:
    from app.models.league import LeagueMembership
    a_leagues = {m.league_id for m in db.session.query(LeagueMembership)
                 .filter_by(user_id=a_id).all()}
    if not a_leagues:
        return False
    return db.session.query(LeagueMembership).filter(
        LeagueMembership.user_id == b_id,
        LeagueMembership.league_id.in_(a_leagues),
    ).first() is not None


# =============================================================================
# Season list
# =============================================================================


@rounds_bp.route("/results")
@login_required
def list():
    season = current_app.config["F1_SEASON"]
    rows = _build_list_rows(season, current_user.id)
    return render_template(
        "rounds/list.html",
        rows=rows,
        season=season,
        is_self=True,
        viewed_user=current_user,
        title="Results",
    )


@rounds_bp.route("/results/u/<username>")
@login_required
def list_friend(username: str):
    friend = db.session.query(User).filter_by(username=username).one_or_none()
    if friend is None:
        abort(404)
    if friend.id == current_user.id:
        return redirect(url_for("rounds.list"))
    if not _users_share_a_league(current_user.id, friend.id):
        abort(403)

    season = current_app.config["F1_SEASON"]
    rows = _build_list_rows(season, friend.id)
    return render_template(
        "rounds/list.html",
        rows=rows,
        season=season,
        is_self=False,
        viewed_user=friend,
        title=f"{friend.username} · results",
    )


# =============================================================================
# Round detail
# =============================================================================


@rounds_bp.route("/round/current")
@login_required
def current():
    rd = get_current_round(current_app.config["F1_SEASON"])
    if rd is None:
        return render_template("rounds/empty.html", title="Round")
    return redirect(url_for("rounds.view", season=rd.season, round_number=rd.round_number))


@rounds_bp.route("/round/<int:season>/<int:round_number>")
@login_required
def view(season: int, round_number: int):
    rd = get_round_by_number(season, round_number)
    if rd is None:
        abort(404)
    state = load_round_state(rd.id, current_user.id)
    prev_rd, next_rd = get_neighbour_rounds(rd, locked_only=True)
    return render_template(
        "rounds/round.html",
        state=state,
        prev_round=prev_rd,
        next_round=next_rd,
        is_self=True,
        viewed_user=current_user,
        leagues=user_leagues(current_user.id),
        friend_username=None,
        title=rd.display_label,
    )


@rounds_bp.route("/round/<int:season>/<int:round_number>/u/<username>")
@login_required
def view_friend(season: int, round_number: int, username: str):
    rd = get_round_by_number(season, round_number)
    if rd is None:
        abort(404)
    if not rd.predictions_locked:
        abort(403)
    friend = db.session.query(User).filter_by(username=username).one_or_none()
    if friend is None:
        abort(404)
    if friend.id == current_user.id:
        return redirect(url_for("rounds.view", season=season, round_number=round_number))
    if not _users_share_a_league(current_user.id, friend.id):
        abort(403)

    state = load_round_state(rd.id, friend.id)
    prev_rd, next_rd = get_neighbour_rounds(rd, locked_only=True)
    return render_template(
        "rounds/round.html",
        state=state,
        prev_round=prev_rd,
        next_round=next_rd,
        is_self=False,
        viewed_user=friend,
        leagues=user_leagues(current_user.id),
        friend_username=friend.username,
        title=f"{friend.username} · {rd.display_label}",
    )
