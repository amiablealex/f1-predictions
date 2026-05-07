"""Round-view blueprint.

Read-only views of a round — your own predictions/results/scores, plus
the friend's-view variant reached from a leaderboard tap.
"""
from __future__ import annotations

from flask import Blueprint, abort, current_app, redirect, render_template, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models.user import User
from app.utils import (
    get_current_round,
    get_neighbour_rounds,
    get_round_by_number,
    load_round_state,
    user_leagues,
)

rounds_bp = Blueprint("rounds", __name__, template_folder="../templates")


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
    prev_rd, next_rd = get_neighbour_rounds(rd)
    return render_template(
        "rounds/round.html",
        state=state,
        prev_round=prev_rd,
        next_round=next_rd,
        is_self=True,
        viewed_user=current_user,
        leagues=user_leagues(current_user.id),
        title=rd.display_label,
    )


@rounds_bp.route("/round/<int:season>/<int:round_number>/u/<username>")
@login_required
def view_friend(season: int, round_number: int, username: str):
    """View a friend's predictions for a round.

    Two preconditions:
      - the round must be locked (deadline passed); we never reveal a
        friend's predictions before lock.
      - the viewer and friend must share at least one league.
    """
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
    prev_rd, next_rd = get_neighbour_rounds(rd)
    return render_template(
        "rounds/round.html",
        state=state,
        prev_round=prev_rd,
        next_round=next_rd,
        is_self=False,
        viewed_user=friend,
        leagues=user_leagues(current_user.id),
        title=f"{friend.username} · {rd.display_label}",
    )


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
