"""Leaderboard blueprint.

Two views per league:
  - 'total' (default): cumulative points across the season + most recent
    round's score in a "Last" column.
  - 'h2h': count of rounds where each user was the top scorer in this
    league. Ties: every user tying for the highest score in a round earns
    1 H2H point. Last column = 1 if tied for top in the most recent
    scored round, 0 otherwise.
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
from app.utils import (
    assert_member,
    get_neighbour_rounds,
    get_round_by_number,
    load_round_comparison,
    most_recent_visible_round,
    user_leagues,
)

leaderboard_bp = Blueprint("leaderboard", __name__, template_folder="../templates")


@dataclass
class LeaderboardRow:
    user_id: int
    username: str
    score: int                  # primary score (total points or H2H total)
    last_score: int             # most-recent-scored-round score
    rank: int                   # 1-based dense rank on `score` (ties share)
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
    if view_kind not in ("total", "h2h", "heatmap"):
        view_kind = "total"

    member_ids = _league_member_ids(league_id)
    members_by_id = {u.id: u for u in db.session.query(User).filter(User.id.in_(member_ids)).all()}

    last_scored = _most_recent_scored_round()

    rows: list[LeaderboardRow] = []
    heatmap = None
    if view_kind == "heatmap":
        heatmap = _build_heatmap(members_by_id)
    elif view_kind == "h2h":
        rows = _build_h2h_rows(league_id, members_by_id, last_scored)
    else:
        rows = _build_total_rows(league_id, members_by_id, last_scored)

    return render_template(
        "leaderboard/view.html",
        league=league,
        leagues=user_leagues(current_user.id),
        view_kind=view_kind,
        rows=rows,
        heatmap=heatmap,
        friend_landing=most_recent_visible_round(current_app.config["F1_SEASON"]),
        last_scored=last_scored,
        title=f"{league.name} · Leaderboard",
    )


@leaderboard_bp.route("/<int:league_id>/compare")
@login_required
def compare(league_id: int):
    """Everyone's per-prediction scores for one round, side by side.

    League-scoped. Defaults to the most recent COMPLETED round; ?round=<n>
    selects a specific one. Only locked rounds are viewable (scores for an
    unlocked round would leak in-progress predictions). Prev/next arrows page
    through locked rounds, mirroring the round view.
    """
    league = assert_member(current_user.id, league_id)
    season = current_app.config["F1_SEASON"]

    round_number = request.args.get("round", type=int)
    if round_number is not None:
        rd = get_round_by_number(season, round_number)
        if rd is None:
            abort(404)
        if not rd.predictions_locked:
            abort(403)
    else:
        rd = (
            db.session.query(Round)
            .filter(Round.season == season, Round.state == RoundState.COMPLETED)
            .order_by(Round.round_number.desc())
            .first()
        )

    member_ids = _league_member_ids(league_id)
    members_by_id = {
        u.id: u for u in db.session.query(User).filter(User.id.in_(member_ids)).all()
    }

    comparison = None
    prev_round = next_round = None
    if rd is not None:
        comparison = load_round_comparison(rd.id, members_by_id, current_user.id)
        prev_round, next_round = get_neighbour_rounds(rd, locked_only=True)

    return render_template(
        "leaderboard/compare.html",
        league=league,
        leagues=user_leagues(current_user.id),
        comparison=comparison,
        round_obj=rd,
        prev_round=prev_round,
        next_round=next_round,
        title=f"{league.name} · Compare",
    )


# =============================================================================
# Builders
# =============================================================================


def _league_member_ids(league_id: int) -> list[int]:
    return [
        m.user_id for m in db.session.query(LeagueMembership)
        .filter_by(league_id=league_id).all()
    ]


def _most_recent_scored_round() -> Round | None:
    """The round with the highest round_number that has at least one
    PredictionScore row. Used as the anchor for the leaderboard's
    'Last' column."""
    return (
        db.session.query(Round)
        .join(PredictionScore, PredictionScore.round_id == Round.id)
        .order_by(Round.round_number.desc())
        .first()
    )


def _build_total_rows(
    league_id: int,
    members_by_id: dict[int, User],
    last_scored: Round | None,
) -> list[LeaderboardRow]:
    """Sum PredictionScore.points per user across the whole season,
    plus per-user score for the most recent scored round."""
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

    last_scores: dict[int, int] = {}
    if last_scored is not None:
        last_scores = dict(
            db.session.query(
                PredictionScore.user_id,
                func.coalesce(func.sum(PredictionScore.points), 0),
            )
            .filter(
                PredictionScore.user_id.in_(member_ids),
                PredictionScore.round_id == last_scored.id,
            )
            .group_by(PredictionScore.user_id)
            .all()
        )

    triples = [
        (u.id, int(totals.get(u.id, 0)), int(last_scores.get(u.id, 0)))
        for u in members_by_id.values()
    ]
    triples.sort(key=lambda p: (-p[1], -p[2], members_by_id[p[0]].username.lower()))
    return _rank(triples, members_by_id)


def _build_h2h_rows(
    league_id: int,
    members_by_id: dict[int, User],
    last_scored: Round | None,
) -> list[LeaderboardRow]:
    """For each completed round, give 1 point to every league member who
    tied for the highest score in that round.

    Last column = 1 if user tied for top in `last_scored`, else 0.
    """
    member_ids = list(members_by_id.keys())
    if not member_ids:
        return []

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

    completed_rounds = {
        r.id for r in db.session.query(Round.id, Round.state).all() if r.state == RoundState.COMPLETED
    }

    h2h_total: dict[int, int] = {uid: 0 for uid in member_ids}
    for round_id, scoreboard in by_round.items():
        if round_id not in completed_rounds or not scoreboard:
            continue
        top_score = max(s for _, s in scoreboard)
        if top_score == 0:
            continue
        for uid, score in scoreboard:
            if score == top_score:
                h2h_total[uid] += 1

    # Last-round H2H — 1 if tied for top in the most recent scored round.
    h2h_last: dict[int, int] = {uid: 0 for uid in member_ids}
    if last_scored is not None and last_scored.id in by_round:
        scoreboard = by_round[last_scored.id]
        if scoreboard:
            top_score = max(s for _, s in scoreboard)
            if top_score > 0:
                for uid, score in scoreboard:
                    if score == top_score:
                        h2h_last[uid] = 1

    triples = [(uid, h2h_total[uid], h2h_last[uid]) for uid in member_ids]
    triples.sort(key=lambda p: (-p[1], -p[2], members_by_id[p[0]].username.lower()))
    return _rank(triples, members_by_id)


def _build_heatmap(members_by_id: dict[int, User]) -> dict:
    """Users×rounds score matrix for the heatmap view.

    Columns are every COMPLETED round, ascending. A cell is the user's total
    points for that round, or None when they submitted nothing (no
    PredictionScore rows) — distinguishing a genuine 0 from a non-entry.
    Rows are ordered by the sum of displayed scores, descending.
    """
    member_ids = list(members_by_id.keys())

    completed = (
        db.session.query(Round)
        .filter(Round.state == RoundState.COMPLETED)
        .order_by(Round.round_number.asc())
        .all()
    )
    if not member_ids or not completed:
        return {"rounds": completed, "users": []}

    round_ids = [r.id for r in completed]

    # Per (user, round): summed points + row count. count > 0 ⇒ participated,
    # so a real 0 shows as "0" rather than a dash.
    cell: dict[tuple[int, int], tuple[int, int]] = {}
    for uid, rid, pts, cnt in (
        db.session.query(
            PredictionScore.user_id,
            PredictionScore.round_id,
            func.coalesce(func.sum(PredictionScore.points), 0),
            func.count(PredictionScore.id),
        )
        .filter(
            PredictionScore.user_id.in_(member_ids),
            PredictionScore.round_id.in_(round_ids),
        )
        .group_by(PredictionScore.user_id, PredictionScore.round_id)
        .all()
    ):
        cell[(uid, rid)] = (int(pts), int(cnt))

    totals: dict[int, int] = defaultdict(int)
    for (uid, _rid), (pts, _cnt) in cell.items():
        totals[uid] += pts

    ordered = sorted(
        members_by_id.values(),
        key=lambda u: (-totals.get(u.id, 0), u.username.lower()),
    )

    users = []
    rank = 0
    last_total: int | None = None
    for u in ordered:
        u_total = totals.get(u.id, 0)
        if u_total != last_total:
            rank += 1
            last_total = u_total
        cells = []
        for rid in round_ids:
            c = cell.get((u.id, rid))
            cells.append(None if c is None or c[1] == 0 else c[0])
        users.append({
            "rank": rank,
            "username": u.username,
            "is_self": u.id == current_user.id,
            "cells": cells,
        })

    return {"rounds": completed, "users": users}


def _rank(
    triples: list[tuple[int, int, int]],
    members_by_id: dict[int, User],
) -> list[LeaderboardRow]:
    """Convert (user_id, primary_score, last_score) triples (sorted desc)
    into ranked rows. Dense ranking on primary score — ties share.
    """
    out: list[LeaderboardRow] = []
    last_primary: int | None = None
    rank = 0
    for user_id, primary, last in triples:
        if primary != last_primary:
            rank += 1
            last_primary = primary
        u = members_by_id[user_id]
        out.append(LeaderboardRow(
            user_id=user_id,
            username=u.username,
            score=primary,
            last_score=last,
            rank=rank,
            is_self=(user_id == current_user.id),
        ))
    return out
