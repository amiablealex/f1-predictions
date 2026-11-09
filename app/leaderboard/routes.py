"""Leaderboard blueprint.

Views per league (the virtual Global league shows Total points only):
  - 'total' (default): cumulative points across the season + most recent
    round's score in a "Last" column.
  - 'h2h': count of rounds where each user was the top scorer in this
    league. Ties: every user tying for the highest score in a round earns
    1 H2H point. Last column = 1 if tied for top in the most recent
    scored round, 0 otherwise.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from flask import Blueprint, abort, current_app, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import func

from app.extensions import db
from app.models.league import League, LeagueMembership
from app.models.prediction import PredictionScore
from app.models.round import Round, RoundState, Session, SessionStatus
from app.models.user import User
from app.utils import (
    assert_member,
    get_neighbour_rounds,
    get_round_by_number,
    load_round_comparison,
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
    pprp: float | None = None   # points per round participated (total view only)
    tied: bool = False          # another row shares this rank — shown as "=3"


@leaderboard_bp.route("/")
@login_required
def index():
    """The Leagues landing page replaced this; kept for old links."""
    from flask import redirect, url_for
    return redirect(url_for("leagues.index"))


@leaderboard_bp.route("/global")
@login_required
def global_view():
    """Every opted-in player. Names only link through where the viewer
    shares a private league, so predictions stay league-scoped."""
    from app.utils import league_mate_ids
    return _render_board(
        name="Global",
        member_ids=global_member_ids(current_user),
        linkable_ids=league_mate_ids(current_user.id),
        endpoint_args={},
        league=None,
    )


@leaderboard_bp.route("/<int:league_id>")
@login_required
def view(league_id: int):
    league = assert_member(current_user.id, league_id)
    member_ids = _league_member_ids(league_id)
    return _render_board(
        name=league.name,
        member_ids=member_ids,
        linkable_ids=set(member_ids),
        endpoint_args={"league_id": league_id},
        league=league,
    )


def _render_board(
    name: str,
    member_ids: list[int],
    linkable_ids: set[int],
    endpoint_args: dict,
    league: League | None,
):
    view_kind = request.args.get("view", "total")
    if league is None or view_kind not in ("total", "h2h", "heatmap"):
        view_kind = "total"

    members_by_id = {u.id: u for u in db.session.query(User).filter(User.id.in_(member_ids)).all()}

    last_scored = _most_recent_scored_round()

    rows: list[LeaderboardRow] = []
    heatmap = None
    if view_kind == "heatmap":
        heatmap = _build_heatmap(members_by_id)
    elif view_kind == "h2h":
        rows = _build_h2h_rows(members_by_id, last_scored)
    else:
        rows = _build_total_rows(members_by_id, last_scored)

    # While the current round is open, tick league-mates who've saved picks.
    # Private leagues only; Global never shows it.
    open_round = _open_round() if league else None
    submitted_ids = _submitted_ids(open_round.id, member_ids) if open_round else set()

    return render_template(
        "leaderboard/view.html",
        league=league,
        open_round=open_round,
        submitted_ids=submitted_ids,
        member_count=len(member_ids),
        board_name=name,
        board_endpoint="leaderboard.view" if league else "leaderboard.global_view",
        board_args=endpoint_args,
        linkable_ids=linkable_ids,
        view_kind=view_kind,
        rows=rows,
        heatmap=heatmap,
        last_scored=last_scored,
        provisional_note=_provisional_note(),
        title=f"{name} · Leaderboard",
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


def _provisional_note() -> str | None:
    """'Includes provisional points from Round X. These may change.' when
    any scored round this season still has a provisional session."""
    numbers = sorted({
        n for (n,) in db.session.query(Round.round_number)
        .join(Session, Session.round_id == Round.id)
        .join(PredictionScore, PredictionScore.round_id == Round.id)
        .filter(
            Round.season == current_app.config["F1_SEASON"],
            Session.status == SessionStatus.PROVISIONAL,
        )
        .distinct()
        .all()
    })
    if not numbers:
        return None
    if len(numbers) == 1:
        label = f"Round {numbers[0]}"
    else:
        label = "Rounds " + ", ".join(str(n) for n in numbers[:-1]) + f" and {numbers[-1]}"
    return f"Includes provisional points from {label}. These may change."


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

    part_counts = dict(
        db.session.query(
            PredictionScore.user_id,
            func.count(func.distinct(PredictionScore.round_id)),
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
    rows = _rank(triples, members_by_id)
    for row in rows:
        n = int(part_counts.get(row.user_id, 0))
        row.pprp = round(row.score / n, 1) if n else None
    return rows


def _build_h2h_rows(
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
            "user_id": u.id,
            "rank": rank,
            "username": u.username,
            "is_self": u.id == current_user.id,
            "cells": cells,
        })

    counts = Counter(u["rank"] for u in users)
    for u in users:
        u["tied"] = counts[u["rank"]] > 1
    return {"rounds": completed, "users": users}


def _open_round() -> Round | None:
    """The current round if it is still taking predictions."""
    from datetime import datetime, timezone
    from app.utils import get_current_round
    rd = get_current_round(current_app.config["F1_SEASON"])
    if rd is None or rd.predictions_locked or rd.predictions_deadline is None:
        return None
    return rd if rd.predictions_deadline > datetime.now(timezone.utc) else None


def _submitted_ids(round_id: int, member_ids: list[int]) -> set[int]:
    """Members with at least one saved prediction for the round."""
    from sqlalchemy import select, union
    from app.admin.metrics import PREDICTION_TABLES
    if not member_ids:
        return set()
    stmt = union(*[
        select(m.user_id).where(m.round_id == round_id, m.user_id.in_(member_ids))
        for m in PREDICTION_TABLES
    ])
    return {uid for (uid,) in db.session.execute(stmt).all()}


def global_member_ids(viewer: User) -> list[int]:
    """Opted-in users with at least one scored round this season, plus the
    viewer if opted in, so a newcomer still sees where they stand."""
    ids = {
        uid for (uid,) in db.session.query(PredictionScore.user_id)
        .join(Round, Round.id == PredictionScore.round_id)
        .join(User, User.id == PredictionScore.user_id)
        .filter(
            Round.season == current_app.config["F1_SEASON"],
            User.show_on_global.is_(True),
        )
        .distinct()
        .all()
    }
    if viewer.show_on_global:
        ids.add(viewer.id)
    return sorted(ids)


@dataclass
class Standing:
    name: str
    league_id: int | None       # None for Global
    rank: int | None            # None when the viewer is hidden from Global
    size: int
    movement: str | None        # 'up' | 'down' | 'same' | None (no earlier round)
    tied: bool = False

    @property
    def position(self) -> str:
        if self.rank is None:
            return "Hidden"
        suffix = "th" if 10 <= self.rank % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(self.rank % 10, "th")
        return f"{'=' if self.tied else ''}{self.rank}{suffix} of {self.size}"


def user_standings(viewer: User) -> list[Standing]:
    """The viewer's total-points position in Global and each private league,
    with movement caused by the most recent scored round.

    Previous position = the same ranking with that round's points removed,
    over the same members, so nothing is stored and joiners can't move you.
    """
    totals: dict[int, int] = {
        uid: int(pts) for uid, pts in db.session.query(
            PredictionScore.user_id, func.coalesce(func.sum(PredictionScore.points), 0),
        ).group_by(PredictionScore.user_id).all()
    }
    last_scored = _most_recent_scored_round()
    previous: dict[int, int] | None = None
    if last_scored is not None:
        has_earlier = db.session.query(PredictionScore.id).filter(
            PredictionScore.round_id != last_scored.id,
        ).first() is not None
        if has_earlier:
            last_pts = dict(
                db.session.query(
                    PredictionScore.user_id, func.coalesce(func.sum(PredictionScore.points), 0),
                )
                .filter(PredictionScore.round_id == last_scored.id)
                .group_by(PredictionScore.user_id)
                .all()
            )
            previous = {uid: t - int(last_pts.get(uid, 0)) for uid, t in totals.items()}

    def standing(name: str, league_id: int | None, member_ids: list[int]) -> Standing:
        if viewer.id not in member_ids:
            return Standing(name, league_id, None, len(member_ids), None)
        rank = _dense_rank(totals, member_ids, viewer.id)
        movement = None
        if previous is not None:
            before = _dense_rank(previous, member_ids, viewer.id)
            movement = "up" if rank < before else "down" if rank > before else "same"
        mine = totals.get(viewer.id, 0)
        tied = sum(1 for uid in member_ids if totals.get(uid, 0) == mine) > 1
        return Standing(name, league_id, rank, len(member_ids), movement, tied)

    out = [standing("Global", None, global_member_ids(viewer))]
    for league in user_leagues(viewer.id):
        out.append(standing(league.name, league.id, _league_member_ids(league.id)))
    return out


def _dense_rank(totals: dict[int, int], member_ids: list[int], user_id: int) -> int:
    """Dense rank of `user_id` among `member_ids`, matching `_rank`."""
    mine = totals.get(user_id, 0)
    return 1 + len({totals.get(uid, 0) for uid in member_ids if totals.get(uid, 0) > mine})


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
    counts = Counter(r.rank for r in out)
    for r in out:
        r.tied = counts[r.rank] > 1
    return out
