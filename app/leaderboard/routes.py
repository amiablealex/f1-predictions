"""Leaderboard blueprint.

Two views per league:
  - 'total' (default): cumulative points across the season
  - 'h2h': count of rounds where each user was the top scorer in this
    league. Ties: every user tying for the highest score in a round earns
    1 H2H point (per scope discussion).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from flask import Blueprint, abort, current_app, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import func

from app.extensions import db
from app.models.league import League, LeagueMembership
from app.models.prediction import PredictionScore
from app.models.round import Round, RoundState
from app.models.user import User
from app.utils import assert_member, most_recent_visible_round, user_leagues

leaderboard_bp = Blueprint("leaderboard", __name__, template_folder="../templates")


@dataclass
class LeaderboardRow:
    user_id: int
    username: str
    score: int
    rank: int                # 1-based dense rank (ties share rank)
    is_self: bool


@leaderboard_bp.route("/")
@login_required
def index():
    """Default to the user's first league, or show 'no leagues' state."""
    leagues = user_leagues(current_user.id)
    if not leagues:
        return render_template("leaderboard/no_leagues.html", title="Leaderboard")
    from flask import redirect, url_for
    return redirect(url_for("leaderboard.view", league_id=leagues[0].id))


@leaderboard_bp.route("/<int:league_id>")
@login_required
def view(league_id: int):
    league = assert_member(current_user.id, league_id)
    view_kind = request.args.get("view", "total")
    if view_kind not in ("total", "h2h"):
        view_kind = "total"

    member_ids = _league_member_ids(league_id)
    members_by_id = {u.id: u for u in db.session.query(User).filter(User.id.in_(member_ids)).all()}

    if view_kind == "h2h":
        rows = _build_h2h_rows(league_id, members_by_id)
    else:
        rows = _build_total_rows(league_id, members_by_id)

    return render_template(
        "leaderboard/view.html",
        league=league,
        leagues=user_leagues(current_user.id),
        view_kind=view_kind,
        rows=rows,
        friend_landing=most_recent_visible_round(current_app.config["F1_SEASON"]),
        title=f"{league.name} · Leaderboard",
    )


# =============================================================================
# Builders
# =============================================================================


def _league_member_ids(league_id: int) -> list[int]:
    return [
        m.user_id for m in db.session.query(LeagueMembership)
        .filter_by(league_id=league_id).all()
    ]


def _build_total_rows(league_id: int, members_by_id: dict[int, User]) -> list[LeaderboardRow]:
    """Sum PredictionScore.points per user across the whole season."""
    member_ids = list(members_by_id.keys())
    if not member_ids:
        return []
    totals = dict(
        db.session.query(
            PredictionScore.user_id, func.coalesce(func.sum(PredictionScore.points), 0),
        )
        .filter(PredictionScore.user_id.in_(member_ids))
        .group_by(PredictionScore.user_id)
        .all()
    )
    pairs = [(u.id, int(totals.get(u.id, 0))) for u in members_by_id.values()]
    pairs.sort(key=lambda p: (-p[1], members_by_id[p[0]].username.lower()))
    return _rank(pairs, members_by_id)


def _build_h2h_rows(league_id: int, members_by_id: dict[int, User]) -> list[LeaderboardRow]:
    """For each completed round, give 1 point to every league member who
    tied for the highest score in that round."""
    member_ids = list(members_by_id.keys())
    if not member_ids:
        return []

    # Per-round per-member totals
    rows = (
        db.session.query(
            PredictionScore.round_id,
            PredictionScore.user_id,
            func.coalesce(func.sum(PredictionScore.points), 0),
        )
        .filter(PredictionScore.user_id.in_(member_ids))
        .group_by(PredictionScore.round_id, PredictionScore.user_id)
        .all()
    )

    by_round: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for round_id, user_id, total in rows:
        by_round[round_id].append((user_id, int(total)))

    # Only count rounds whose state is COMPLETED (avoid mid-round leads).
    completed_rounds = {
        r.id for r in db.session.query(Round.id, Round.state).all() if r.state == RoundState.COMPLETED
    }

    h2h: dict[int, int] = {uid: 0 for uid in member_ids}
    for round_id, scoreboard in by_round.items():
        if round_id not in completed_rounds or not scoreboard:
            continue
        top_score = max(s for _, s in scoreboard)
        if top_score == 0:
            continue  # no one scored anything; don't award H2H
        for uid, score in scoreboard:
            if score == top_score:
                h2h[uid] += 1

    pairs = list(h2h.items())
    pairs.sort(key=lambda p: (-p[1], members_by_id[p[0]].username.lower()))
    return _rank(pairs, members_by_id)


def _rank(pairs: list[tuple[int, int]], members_by_id: dict[int, User]) -> list[LeaderboardRow]:
    """Convert (user_id, score) pairs (already sorted desc) into ranked rows.

    Uses dense ranking — ties share the same rank, the next user gets the
    next integer (1, 2, 2, 3) — the small-group-friendly choice.
    """
    out: list[LeaderboardRow] = []
    last_score: int | None = None
    rank = 0
    for user_id, score in pairs:
        if score != last_score:
            rank += 1
            last_score = score
        u = members_by_id[user_id]
        out.append(LeaderboardRow(
            user_id=user_id,
            username=u.username,
            score=score,
            rank=rank,
            is_self=(user_id == current_user.id),
        ))
    return out
