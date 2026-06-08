"""Admin blueprint.

Entry-points for the deployer (only) to override worker behaviour and
manually correct state. Every route requires `current_user.is_admin`.
"""
from __future__ import annotations

import logging

from flask import Blueprint, abort, current_app, flash, redirect, render_template, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm

from app.api.jolpica import build_default_client
from app.extensions import db
from app.models.league import League
from app.models.prediction import PredictionScore
from app.models.round import Round, RoundState, ScoringPhase, Session, SessionStatus
from app.models.user import PasswordResetToken, User
from app.utils import admin_required
from worker.ingest import session_triggers_phase
from worker.jobs import (
    deadline_lock_job,
    driver_master_sync_job,
    phase_scoring_job,
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
        .filter(Round.season == current_app.config["F1_SEASON"])
        .order_by(Round.round_number.asc())
        .all()
    )
    leagues = db.session.query(League).order_by(League.created_at.desc()).all()
    users = db.session.query(User).order_by(User.username.asc()).all()
    bare = FlaskForm()
    return render_template(
        "admin/dashboard.html",
        rounds=rounds, leagues=leagues, users=users,
        bare_csrf=bare, title="Admin",
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
# Per-round actions
# =============================================================================


@admin_bp.route("/round/<int:round_id>/lock-toggle", methods=["POST"])
@login_required
@admin_required
def round_lock_toggle(round_id: int):
    if not FlaskForm().validate_on_submit():
        abort(400)
    rd = db.session.get(Round, round_id)
    if rd is None:
        abort(404)
    rd.predictions_locked = not rd.predictions_locked
    db.session.commit()
    flash(
        f"Round {rd.round_number}: predictions {'LOCKED' if rd.predictions_locked else 'UNLOCKED'}.",
        "info",
    )
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/round/<int:round_id>/refresh-session/<int:session_id>", methods=["POST"])
@login_required
@admin_required
def round_refresh_session(round_id: int, session_id: int):
    """Force a session back into pending_results so the next results poll
    re-fetches it. Useful when the API has updated provisional results to
    final results after stewards' decisions."""
    if not FlaskForm().validate_on_submit():
        abort(400)
    s = db.session.get(Session, session_id)
    if s is None or s.round_id != round_id:
        abort(404)
    s.status = SessionStatus.PENDING_RESULTS
    s.results_fetched_at = None
    s.scored_at = None
    db.session.commit()
    flash(f"Session {s.session_type.value} marked pending; will re-fetch on next poll.", "info")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/round/<int:round_id>/rescore", methods=["POST"])
@login_required
@admin_required
def round_rescore(round_id: int):
    """Re-run scoring for every phase that has completed sessions.

    Idempotent — calls phase_scoring_job which replaces existing rows.
    """
    if not FlaskForm().validate_on_submit():
        abort(400)
    rd = db.session.get(Round, round_id)
    if rd is None:
        abort(404)
    triggered: list[str] = []
    for s in rd.sessions:
        if s.status != SessionStatus.COMPLETED:
            continue
        phase = session_triggers_phase(s.session_type)
        if phase is None:
            continue
        phase_scoring_job(current_app, rd.id, phase)
        triggered.append(phase.value)
    flash(
        f"Rescored phases: {', '.join(triggered) or 'none (no completed scoring sessions)'}.",
        "success",
    )
    return redirect(url_for("admin.dashboard"))


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
