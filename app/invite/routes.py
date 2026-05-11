"""Invite blueprint — public landing for a league's invite link.

Flow:
  GET  /invite/<code>          — landing page (public or authenticated variant)
  POST /invite/<code>/join     — authenticated, confirms the join

For logged-out visitors the code is stashed on the Flask session so the
register/login round-trip can auto-join after successful auth (see
`consume_pending_invite` and the auth routes).
"""
from __future__ import annotations

import logging
import time

from flask import (
    Blueprint, abort, flash, redirect, render_template, request, session, url_for,
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm

from app.extensions import db
from app.invite.rate_limit import is_rate_limited
from app.models.league import League, LeagueMembership
from app.utils import user_is_member

invite_bp = Blueprint("invite", __name__, template_folder="../templates")
log = logging.getLogger(__name__)

_PENDING_INVITE_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days
PENDING_INVITE_KEY = "pending_invite_code"


# =============================================================================
# Session helpers — used here and from app/auth/routes.py
# =============================================================================


def set_pending_invite(code: str) -> None:
    session[PENDING_INVITE_KEY] = {"code": code, "expires_at": time.time() + _PENDING_INVITE_TTL_SECONDS}


def consume_pending_invite(user) -> tuple[League | None, bool]:
    """Pop the pending invite from the session and join the user to the
    league if valid.

    Returns (league, newly_joined). league is None if no pending invite,
    or if the code no longer resolves (league deleted). newly_joined is
    True if we added a membership, False if the user was already in.
    """
    raw = session.pop(PENDING_INVITE_KEY, None)
    if not raw:
        return (None, False)
    # Tolerate the old shape from in-flight sessions during deploy.
    if isinstance(raw, str):
        code = raw
    elif isinstance(raw, dict):
        if raw.get("expires_at", 0) < time.time():
            return (None, False)
        code = raw.get("code")
    else:
        return (None, False)
    if not code:
        return (None, False)
    league = db.session.query(League).filter_by(invite_code=code).one_or_none()
    if league is None:
        return (None, False)
    if user_is_member(user.id, league.id):
        return (league, False)
    db.session.add(LeagueMembership(league_id=league.id, user_id=user.id))
    db.session.commit()
    return (league, True)


# =============================================================================
# Routes
# =============================================================================


@invite_bp.route("/<code>")
def landing(code: str):
    ip = request.remote_addr or "?"
    if is_rate_limited(ip):
        abort(429)

    code = code.strip().upper()
    league = db.session.query(League).filter_by(invite_code=code).one_or_none()
    if league is None:
        return render_template("invite/not_found.html", title="Invite"), 404

    if current_user.is_authenticated:
        if user_is_member(current_user.id, league.id):
            flash(f"You're already in {league.name}.", "info")
            return redirect(url_for("leaderboard.view", league_id=league.id))
        return render_template(
            "invite/confirm.html",
            league=league,
            form=FlaskForm(),
            title=f"Join {league.name}",
        )

    # Not logged in: stash the code and show the public landing.
    set_pending_invite(code)
    return render_template(
        "invite/landing.html",
        league=league,
        title=f"Join {league.name}",
    )


@invite_bp.route("/<code>/join", methods=["POST"])
@login_required
def join(code: str):
    form = FlaskForm()
    if not form.validate_on_submit():
        abort(400)
    code = code.strip().upper()
    league = db.session.query(League).filter_by(invite_code=code).one_or_none()
    if league is None:
        flash("That invite link is no longer valid.", "error")
        return redirect(url_for("leagues.index"))
    if user_is_member(current_user.id, league.id):
        flash(f"You're already in {league.name}.", "info")
        return redirect(url_for("leaderboard.view", league_id=league.id))
    db.session.add(LeagueMembership(league_id=league.id, user_id=current_user.id))
    db.session.commit()
    flash(f"Joined {league.name}.", "success")
    return redirect(url_for("leaderboard.view", league_id=league.id))
