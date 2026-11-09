"""Admin blueprint.

Entry-points for the deployer (only) to override worker behaviour and
manually correct state. Every route requires `current_user.is_admin`.
"""
from __future__ import annotations

import logging

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from sqlalchemy.orm import selectinload

from app.api.jolpica import build_default_client, format_lap_time, parse_lap_time
from app.extensions import db
from app.models.league import League
from app.models.prediction import PredictionScore
from app.models.round import Round, RoundState, ScoringPhase, Session, SessionStatus, SessionType
from app.scoring.provisional import describe as describe_missing
from app.scoring.provisional import missing_inputs
from app.models.user import PasswordResetToken, User
from app.utils import admin_required
from worker.ingest import session_triggers_phase
from worker.jobs import (
    deadline_lock_job,
    driver_master_sync_job,
    finalise_session,
    phase_scoring_job,
    reassess_session,
    results_poll_job,
    run_full_pipeline,
    schedule_sync_job,
    session_state_transitions_job,
)

admin_bp = Blueprint("admin", __name__, template_folder="../templates")

log = logging.getLogger(__name__)


@admin_bp.route("/")
@login_required
@admin_required
def dashboard():
    rounds = (
        db.session.query(Round)
        .options(selectinload(Round.sessions))
        .filter(Round.season == current_app.config["F1_SEASON"])
        .order_by(Round.round_number.asc())
        .all()
    )
    return render_template(
        "admin/dashboard.html",
        rounds=rounds, attention=_needs_attention(rounds),
        bare_csrf=FlaskForm(), title="Admin",
    )


def _needs_attention(rounds: list[Round]) -> list[tuple[Round, Session, str]]:
    """Sessions the admin may want to act on, oldest first."""
    out: list[tuple[Round, Session, str]] = []
    for r in rounds:
        for s in r.sessions:
            if s.status == SessionStatus.PROVISIONAL:
                reason = f"provisional · missing {describe_missing(missing_inputs(s))}"
            elif s.status == SessionStatus.PENDING_RESULTS:
                reason = "awaiting results"
            elif (s.session_type == SessionType.QUALIFYING
                  and s.status == SessionStatus.COMPLETED
                  and s.pole_time_ms is None):
                reason = "no pole time"
            else:
                continue
            out.append((r, s, reason))
    return out


@admin_bp.route("/metrics")
@login_required
@admin_required
def usage_metrics():
    from app.admin.metrics import collect_metrics
    return render_template(
        "admin/metrics.html",
        m=collect_metrics(current_app.config["F1_SEASON"]),
        bare_csrf=FlaskForm(),
        title="Metrics",
    )



# =============================================================================
# Worker triggers (run inline — useful when the worker process is down or
# you want to force a refresh without waiting for the next interval)
# =============================================================================


def _client():
    return build_default_client(current_app.config)


@admin_bp.route("/pipeline/run", methods=["POST"])
@login_required
@admin_required
def run_pipeline():
    if not FlaskForm().validate_on_submit():
        abort(400)
    run_full_pipeline(current_app, _client())
    flash("Full pipeline complete.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/pipeline/schedule-sync", methods=["POST"])
@login_required
@admin_required
def trigger_schedule_sync():
    if not FlaskForm().validate_on_submit():
        abort(400)
    schedule_sync_job(current_app, _client())
    flash("Schedule sync complete.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/pipeline/driver-sync", methods=["POST"])
@login_required
@admin_required
def trigger_driver_sync():
    if not FlaskForm().validate_on_submit():
        abort(400)
    driver_master_sync_job(current_app, _client())
    flash("Driver sync complete.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/pipeline/results-poll", methods=["POST"])
@login_required
@admin_required
def trigger_results_poll():
    if not FlaskForm().validate_on_submit():
        abort(400)
    results_poll_job(current_app, _client())
    flash("Results poll complete.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/pipeline/state-transitions", methods=["POST"])
@login_required
@admin_required
def trigger_state_transitions():
    if not FlaskForm().validate_on_submit():
        abort(400)
    session_state_transitions_job(current_app, _client())
    flash("State transitions complete.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/pipeline/deadline-lock", methods=["POST"])
@login_required
@admin_required
def trigger_deadline_lock():
    if not FlaskForm().validate_on_submit():
        abort(400)
    deadline_lock_job(current_app, _client())
    flash("Deadline lock complete.", "success")
    return redirect(url_for("admin.dashboard"))


# =============================================================================
# Per-round page + actions. Every action returns to the round page.
# =============================================================================


def _round_or_404(round_id: int) -> Round:
    rd = db.session.get(Round, round_id)
    if rd is None:
        abort(404)
    return rd


def _session_or_404(round_id: int, session_id: int) -> Session:
    s = db.session.get(Session, session_id)
    if s is None or s.round_id != round_id:
        abort(404)
    return s


def _back(round_id: int):
    return redirect(url_for("admin.round_detail", round_id=round_id))


_RESULT_SESSION_TYPES = (SessionType.QUALIFYING, SessionType.SPRINT_RACE, SessionType.RACE)


@admin_bp.route("/round/<int:round_id>")
@login_required
@admin_required
def round_detail(round_id: int):
    rd = _round_or_404(round_id)
    missing = {
        s.id: describe_missing(missing_inputs(s))
        for s in rd.sessions
        if s.session_type in _RESULT_SESSION_TYPES
        and s.status in (SessionStatus.PROVISIONAL, SessionStatus.COMPLETED)
    }
    quali = next((s for s in rd.sessions if s.session_type == SessionType.QUALIFYING), None)
    return render_template(
        "admin/round.html",
        rd=rd, quali=quali, missing=missing,
        bare_csrf=FlaskForm(), title=f"Admin · R{rd.round_number}",
    )


@admin_bp.route("/round/<int:round_id>/lock-toggle", methods=["POST"])
@login_required
@admin_required
def round_lock_toggle(round_id: int):
    if not FlaskForm().validate_on_submit():
        abort(400)
    rd = _round_or_404(round_id)
    rd.predictions_locked = not rd.predictions_locked
    db.session.commit()
    flash(f"Predictions {'locked' if rd.predictions_locked else 'unlocked'}.", "info")
    return _back(round_id)


@admin_bp.route("/round/<int:round_id>/refresh-session/<int:session_id>", methods=["POST"])
@login_required
@admin_required
def round_refresh_session(round_id: int, session_id: int):
    """Force a session back into pending_results so the next results poll
    re-fetches it. Useful when Jolpica has corrected or filled in data
    after a session was completed."""
    if not FlaskForm().validate_on_submit():
        abort(400)
    s = _session_or_404(round_id, session_id)
    s.status = SessionStatus.PENDING_RESULTS
    s.results_fetched_at = None
    s.scored_at = None
    db.session.commit()
    flash(f"{s.session_type.value}: marked pending, re-fetches on the next poll.", "info")
    return _back(round_id)


@admin_bp.route("/round/<int:round_id>/finalise-session/<int:session_id>", methods=["POST"])
@login_required
@admin_required
def round_finalise_session(round_id: int, session_id: int):
    """Accept a provisional session's current data as final and re-score."""
    if not FlaskForm().validate_on_submit():
        abort(400)
    s = _session_or_404(round_id, session_id)
    if s.status != SessionStatus.PROVISIONAL:
        flash(f"{s.session_type.value} is not provisional.", "error")
        return _back(round_id)
    finalise_session(current_app, s)
    flash(f"{s.session_type.value}: finalised and re-scored.", "success")
    return _back(round_id)


# Sanity bounds for a manually-entered pole time.
_POLE_MIN_MS = 40_000
_POLE_MAX_MS = 180_000


@admin_bp.route("/round/<int:round_id>/pole-time", methods=["POST"])
@login_required
@admin_required
def round_set_pole_time(round_id: int):
    """Manually set the pole time when Jolpica hasn't published quali times.

    A time later published by Jolpica replaces it on the next re-fetch.
    """
    if not FlaskForm().validate_on_submit():
        abort(400)
    rd = _round_or_404(round_id)
    quali = next((s for s in rd.sessions if s.session_type == SessionType.QUALIFYING), None)
    if quali is None:
        abort(404)
    raw = (request.form.get("pole_time") or "").strip()
    ms = parse_lap_time(raw)
    if ms is None or not (_POLE_MIN_MS <= ms <= _POLE_MAX_MS):
        flash(f"Couldn't read '{raw}' as a pole time. Use m:ss.mmm, e.g. 1:10.123.", "error")
        return _back(round_id)
    quali.pole_time_ms = ms
    reassess_session(current_app, quali)
    flash(f"Pole time set to {format_lap_time(ms)}; quali {quali.status.value}.", "success")
    return _back(round_id)


@admin_bp.route("/round/<int:round_id>/rescore", methods=["POST"])
@login_required
@admin_required
def round_rescore(round_id: int):
    """Re-run scoring for every phase with a scored (provisional or
    completed) session. Idempotent — replaces existing rows."""
    if not FlaskForm().validate_on_submit():
        abort(400)
    rd = _round_or_404(round_id)
    triggered: list[str] = []
    for s in rd.sessions:
        if s.status not in (SessionStatus.COMPLETED, SessionStatus.PROVISIONAL):
            continue
        phase = session_triggers_phase(s.session_type)
        if phase is None:
            continue
        phase_scoring_job(current_app, rd.id, phase)
        triggered.append(phase.value)
    flash(
        f"Re-scored phases: {', '.join(triggered) or 'none (nothing with results yet)'}.",
        "success",
    )
    return _back(round_id)


# =============================================================================
# User actions
# =============================================================================


@admin_bp.route("/user/<int:user_id>/issue-reset", methods=["POST"])
@login_required
@admin_required
def issue_password_reset(user_id: int):
    """Generate a password-reset token and show the URL to the admin so
    they can deliver it manually if email isn't working."""
    if not FlaskForm().validate_on_submit():
        abort(400)
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    ttl = current_app.config["PASSWORD_RESET_TOKEN_TTL_HOURS"]
    token = PasswordResetToken.issue(user=user, ttl_hours=ttl)
    db.session.add(token)
    db.session.commit()
    reset_url = url_for("auth.reset_password", token=token.token, _external=True)
    flash(f"Reset link for {user.username} (valid {ttl}h): {reset_url}", "info")
    return redirect(url_for("admin.dashboard"))

@admin_bp.route("/user/<int:user_id>/contributor-toggle", methods=["POST"])
@login_required
@admin_required
def user_contributor_toggle(user_id: int):
    if not FlaskForm().validate_on_submit():
        abort(400)
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    user.is_contributor = not user.is_contributor
    db.session.commit()
    flash(
        f"{user.username}: contributor {'enabled' if user.is_contributor else 'disabled'}.",
        "info",
    )
    return redirect(url_for("admin.dashboard"))
