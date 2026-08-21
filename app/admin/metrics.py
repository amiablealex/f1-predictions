"""Read-only usage metrics for the admin area.

Every figure on /admin/metrics derives from `_user_round_rows()` — a
UNION ALL across the prediction tables, collapsed to one row per
(user, round). Adding a new prediction type means appending it to
PREDICTION_TABLES and nothing else.

Note on timestamps: `_save_predictions` deletes and re-inserts every row
for a user/round on each save, so `submitted_at` is the time of the LAST
save, not the first submission. Timing figures below therefore measure
when people *finalised*, not when they first engaged.

Nothing in this module writes to the database.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, union_all

from app.extensions import db
from app.models.league import League, LeagueMembership
from app.models.prediction import (
    DnfCountPrediction,
    FastestLapPrediction,
    PlacesGainedPrediction,
    PoleTimePrediction,
    PredictionScore,
    QualiHeadToHeadPrediction,
    QualiNthPrediction,
    QualiRandomDriverPrediction,
    SpecialPrediction,
    Top3QualiPrediction,
    Top3SprintPrediction,
    Top10Prediction,
)
from app.models.round import Round, RoundState, Session, SessionStatus, SessionType
from app.models.user import User

# Every table carrying (user_id, round_id, submitted_at).
#
# ContributionPrediction is deliberately absent: it is keyed on
# contribution_id rather than round_id, so it would need a join through
# ContributionDefinition to resolve the round. It is written inside the
# same _save_predictions transaction as everything else, so anyone with
# contribution rows necessarily has rows here too — excluding it costs no
# participation signal, and the row-count totals stay comparable because
# the exclusion applies to every user equally.
PREDICTION_TABLES = (
    Top10Prediction,
    Top3QualiPrediction,
    Top3SprintPrediction,
    PoleTimePrediction,
    FastestLapPrediction,
    DnfCountPrediction,
    PlacesGainedPrediction,
    QualiRandomDriverPrediction,
    QualiHeadToHeadPrediction,
    QualiNthPrediction,
    SpecialPrediction,
)

LAPSED_WINDOW = 3       # rounds looked back over when deciding "lapsed"
WARM_LOGIN_DAYS = 30    # login recency that counts as "warm"
STALE_SESSION_HOURS = 6  # grace before a non-complete session looks stuck


# =============================================================================
# Spine
# =============================================================================


@dataclass(frozen=True)
class Activity:
    user_id: int
    round_id: int
    row_count: int
    submitted_at: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Guard against naive datetimes from a local dev database."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _activity_subq():
    """UNION ALL of (user_id, round_id, submitted_at) across every table."""
    stmts = [
        select(
            model.user_id.label("user_id"),
            model.round_id.label("round_id"),
            model.submitted_at.label("submitted_at"),
        )
        for model in PREDICTION_TABLES
    ]
    return union_all(*stmts).subquery("prediction_activity")


def _user_round_rows() -> list[Activity]:
    """One row per (user, round) that has any prediction activity."""
    a = _activity_subq()
    stmt = select(
        a.c.user_id,
        a.c.round_id,
        func.count().label("row_count"),
        func.max(a.c.submitted_at).label("submitted_at"),
    ).group_by(a.c.user_id, a.c.round_id)
    return [
        Activity(uid, rid, count, _aware(ts))
        for uid, rid, count, ts in db.session.execute(stmt).all()
    ]


def _season_rounds(season: int) -> list[Round]:
    return list(
        db.session.execute(
            select(Round)
            .where(Round.season == season)
            .order_by(Round.round_number.asc())
        ).scalars()
    )


def _closed_rounds(rounds: list[Round]) -> list[Round]:
    """Rounds whose deadline has passed — the ones a user could have
    participated in. Ordered oldest first."""
    now = _utcnow()
    return [
        r for r in rounds
        if _aware(r.predictions_deadline) is not None
        and _aware(r.predictions_deadline) < now
    ]


# =============================================================================
# Sections
# =============================================================================


def snapshot(activity: list[Activity], rounds: list[Round]) -> dict:
    total_users = db.session.scalar(select(func.count()).select_from(User)) or 0
    total_leagues = db.session.scalar(select(func.count()).select_from(League)) or 0
    total_memberships = db.session.scalar(
        select(func.count()).select_from(LeagueMembership)
    ) or 0

    predicted_ever = len({a.user_id for a in activity})
    closed = _closed_rounds(rounds)
    latest = closed[-1] if closed else None
    active_latest = (
        len({a.user_id for a in activity if a.round_id == latest.id}) if latest else 0
    )

    return {
        "total_users": total_users,
        "predicted_ever": predicted_ever,
        "never_predicted": total_users - predicted_ever,
        "total_leagues": total_leagues,
        "avg_league_size": round(total_memberships / total_leagues, 1) if total_leagues else 0.0,
        "active_latest": active_latest,
        "latest_round": latest,
    }


def participation_by_round(activity: list[Activity], rounds: list[Round]) -> list[dict]:
    """One row per round: how many eligible users submitted anything.

    "Eligible" = registered before the deadline. "Partial" = submitted
    fewer rows than the fullest submission for that round, which avoids
    hard-coding an expected row count per weekend type. "New" = users
    making their first-ever submission, so a signup wave doesn't read as
    engagement.

    Rounds whose deadline hasn't passed and which have no activity are
    omitted — otherwise every future round shows the full user base as
    eligible against zero submissions.
    """
    now = _utcnow()
    signups = sorted(
        ts for ts in (
            _aware(t) for (t,) in db.session.execute(select(User.created_at)).all()
        ) if ts is not None
    )

    by_round: dict[int, list[Activity]] = defaultdict(list)
    for a in activity:
        by_round[a.round_id].append(a)

    # Each user's first round, so we can split new from returning.
    round_numbers = {r.id: r.round_number for r in rounds}
    first_round: dict[int, int] = {}
    for a in activity:
        n = round_numbers.get(a.round_id)
        if n is None:
            continue
        if a.user_id not in first_round or n < first_round[a.user_id]:
            first_round[a.user_id] = n

    out = []
    for r in rounds:
        deadline = _aware(r.predictions_deadline)
        rows = by_round.get(r.id, [])
        is_closed = deadline is not None and deadline < now
        if not is_closed and not rows:
            continue

        eligible = bisect_right(signups, deadline) if deadline is not None else 0
        user_ids = {a.user_id for a in rows}
        submitted = len(user_ids)
        new = sum(1 for uid in user_ids if first_round.get(uid) == r.round_number)
        expected = max((a.row_count for a in rows), default=0)
        partial = sum(1 for a in rows if a.row_count < expected)

        out.append({
            "round": r,
            "is_closed": is_closed,
            "eligible": eligible,
            "submitted": submitted,
            "new": new,
            "returning": submitted - new,
            "pct": round(100 * submitted / eligible) if eligible else 0,
            "partial": partial,
            "expected_rows": expected,
        })
    out.reverse()  # most recent first
    return out


def signups_by_month(months: int = 12) -> list[dict]:
    bucket = func.date_trunc("month", User.created_at)
    rows = db.session.execute(
        select(bucket.label("month"), func.count().label("n")).group_by(bucket)
    ).all()
    counts = {_aware(m).strftime("%Y-%m"): n for m, n in rows if m is not None}

    now = _utcnow()
    labels = []
    year, month = now.year, now.month
    for _ in range(months):
        labels.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    labels.reverse()

    peak = max((counts.get(k, 0) for k in labels), default=0)
    return [
        {
            "label": k,
            "count": counts.get(k, 0),
            "width": round(100 * counts.get(k, 0) / peak) if peak else 0,
        }
        for k in labels
    ]


TIMING_BUCKETS = (
    ("More than 7 days", timedelta(days=7), None),
    ("1–7 days", timedelta(days=1), timedelta(days=7)),
    ("12–24 hours", timedelta(hours=12), timedelta(days=1)),
    ("1–12 hours", timedelta(hours=1), timedelta(hours=12)),
    ("Under 1 hour", timedelta(0), timedelta(hours=1)),
)


def submission_timing(activity: list[Activity], rounds: list[Round]) -> list[dict]:
    """How far before the deadline submissions were last touched."""
    deadlines = {
        r.id: _aware(r.predictions_deadline)
        for r in rounds if _aware(r.predictions_deadline) is not None
    }

    counts = {label: 0 for label, _, _ in TIMING_BUCKETS}
    counts["After deadline"] = 0

    for a in activity:
        deadline = deadlines.get(a.round_id)
        if deadline is None or a.submitted_at is None:
            continue
        lead = deadline - a.submitted_at
        if lead < timedelta(0):
            counts["After deadline"] += 1
            continue
        for label, lower, upper in TIMING_BUCKETS:
            if lead >= lower and (upper is None or lead < upper):
                counts[label] += 1
                break

    ordered = [label for label, _, _ in TIMING_BUCKETS]
    if counts["After deadline"]:
        ordered.append("After deadline")

    total = sum(counts[k] for k in ordered)
    peak = max((counts[k] for k in ordered), default=0)
    return [
        {
            "label": k,
            "count": counts[k],
            "pct": round(100 * counts[k] / total) if total else 0,
            "width": round(100 * counts[k] / peak) if peak else 0,
        }
        for k in ordered
    ]


def attention_lists(activity: list[Activity], rounds: list[Round]) -> dict:
    """Short named lists — the actionable counterpart to the aggregates."""
    users = list(db.session.execute(select(User).order_by(User.username.asc())).scalars())
    closed = _closed_rounds(rounds)
    latest = closed[-1] if closed else None
    recent_ids = {r.id for r in closed[-(LAPSED_WINDOW + 1):-1]} if len(closed) > 1 else set()

    predicted_ever = {a.user_id for a in activity}
    in_latest = {a.user_id for a in activity if latest and a.round_id == latest.id}
    in_recent = {a.user_id for a in activity if a.round_id in recent_ids}

    with_league = {
        uid for (uid,) in db.session.execute(
            select(LeagueMembership.user_id).distinct()
        ).all()
    }

    warm_cutoff = _utcnow() - timedelta(days=WARM_LOGIN_DAYS)

    lapsed, warm_inactive, never, no_league, void = [], [], [], [], []
    for u in users:
        last_login = _aware(u.last_login_at)
        if u.id not in predicted_ever:
            never.append(u)
        elif u.id not in in_latest:
            if u.id in in_recent:
                lapsed.append(u)
            elif last_login is not None and last_login >= warm_cutoff:
                warm_inactive.append(u)
        if u.id not in with_league:
            no_league.append(u)
            # Predicting with no league means no leaderboard and no
            # comparison view — submitting into the void.
            if u.id in predicted_ever:
                void.append(u)

    return {
        "lapsed": lapsed,
        "warm_inactive": warm_inactive,
        "never_predicted": never,
        "no_league": no_league,
        "predicted_no_league": void,
        "latest_round": latest,
    }


def user_rows(activity: list[Activity], rounds: list[Round]) -> list[dict]:
    """Roster with participation stats. Actions are rendered by the template."""
    users = list(db.session.execute(select(User).order_by(User.username.asc())).scalars())
    round_numbers = {r.id: r.round_number for r in rounds}
    closed_desc = list(reversed(_closed_rounds(rounds)))

    by_user: dict[int, set[int]] = defaultdict(set)
    last_seen: dict[int, datetime] = {}
    for a in activity:
        by_user[a.user_id].add(a.round_id)
        if a.submitted_at and (
            a.user_id not in last_seen or a.submitted_at > last_seen[a.user_id]
        ):
            last_seen[a.user_id] = a.submitted_at

    out = []
    for u in users:
        rids = by_user.get(u.id, set())
        streak = 0
        for r in closed_desc:
            if r.id in rids:
                streak += 1
            else:
                break
        last_round = max(
            (round_numbers[rid] for rid in rids if rid in round_numbers), default=None
        )
        out.append({
            "user": u,
            "rounds_predicted": len(rids),
            "streak": streak,
            "last_round": last_round,
            "last_submitted_at": last_seen.get(u.id),
        })
    return out


def data_health(season: int, rounds: list[Round]) -> dict:
    """Not usage, but it catches silent worker failures."""
    cutoff = _utcnow() - timedelta(hours=STALE_SESSION_HOURS)
    round_ids = [r.id for r in rounds]

    stale = 0
    unscored = 0
    if round_ids:
        stale = db.session.scalar(
            select(func.count())
            .select_from(Session)
            .where(
                Session.round_id.in_(round_ids),
                Session.status != SessionStatus.COMPLETED,
                func.coalesce(Session.scheduled_end, Session.scheduled_start) < cutoff,
            )
        ) or 0
        unscored = db.session.scalar(
            select(func.count())
            .select_from(Session)
            .where(
                Session.round_id.in_(round_ids),
                Session.status == SessionStatus.COMPLETED,
                Session.scored_at.is_(None),
                # Sprint quali never triggers a reveal phase of its own,
                # so a null scored_at there is expected, not a failure.
                Session.session_type != SessionType.SPRINT_QUALI,
            )
        ) or 0

    scored_round_ids = {
        rid for (rid,) in db.session.execute(
            select(PredictionScore.round_id).distinct()
        ).all()
    }
    completed = sum(1 for r in rounds if r.state == RoundState.COMPLETED)

    return {
        "stale_sessions": stale,
        "unscored_sessions": unscored,
        "rounds_total": len(rounds),
        "rounds_completed": completed,
        "rounds_with_scores": sum(1 for r in rounds if r.id in scored_round_ids),
    }


# =============================================================================
# Orchestrator
# =============================================================================


def collect_metrics(season: int) -> dict:
    """Single entry point — the route calls this and nothing else."""
    rounds = _season_rounds(season)
    activity = _user_round_rows()

    season_round_ids = {r.id for r in rounds}
    season_activity = [a for a in activity if a.round_id in season_round_ids]

    return {
        "snapshot": snapshot(season_activity, rounds),
        "participation": participation_by_round(season_activity, rounds),
        "signups": signups_by_month(),
        "timing": submission_timing(season_activity, rounds),
        "attention": attention_lists(season_activity, rounds),
        "users": user_rows(season_activity, rounds),
        "health": data_health(season, rounds),
    }
