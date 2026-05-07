"""Leagues blueprint.

Leagues are the unit of leaderboard scope. A user can be in any number of
leagues. Joining is via 6-character invite code; the creator is admin
and can rename, remove members, or delete the league.
"""
from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired, Length

from app.extensions import db
from app.models.league import League, LeagueMembership
from app.utils import user_is_admin_of, user_is_member, user_leagues

leagues_bp = Blueprint("leagues", __name__, template_folder="../templates")


# =============================================================================
# Forms
# =============================================================================


class CreateLeagueForm(FlaskForm):
    name = StringField("League name", validators=[DataRequired(), Length(min=2, max=80)])
    submit = SubmitField("Create league")


class JoinLeagueForm(FlaskForm):
    invite_code = StringField(
        "Invite code",
        validators=[DataRequired(), Length(min=4, max=16)],
    )
    submit = SubmitField("Join")


class RenameLeagueForm(FlaskForm):
    name = StringField("League name", validators=[DataRequired(), Length(min=2, max=80)])
    submit = SubmitField("Rename")


# =============================================================================
# Routes
# =============================================================================


@leagues_bp.route("/")
@login_required
def index():
    leagues = user_leagues(current_user.id)
    return render_template(
        "leagues/list.html", leagues=leagues, title="Leagues",
    )


@leagues_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    form = CreateLeagueForm()
    if form.validate_on_submit():
        invite_code = _generate_unique_invite_code()
        league = League(
            name=form.name.data.strip(),
            invite_code=invite_code,
            created_by_id=current_user.id,
        )
        db.session.add(league)
        db.session.flush()
        # Creator is automatically a member.
        db.session.add(LeagueMembership(league_id=league.id, user_id=current_user.id))
        db.session.commit()
        flash(f"League created. Share code: {invite_code}", "success")
        return redirect(url_for("leagues.detail", league_id=league.id))
    return render_template("leagues/new.html", form=form, title="New league")


@leagues_bp.route("/join", methods=["GET", "POST"])
@login_required
def join():
    form = JoinLeagueForm()
    if form.validate_on_submit():
        code = form.invite_code.data.strip().upper()
        league = db.session.query(League).filter_by(invite_code=code).one_or_none()
        if league is None:
            flash("That invite code didn't match any league.", "error")
            return render_template("leagues/join.html", form=form, title="Join league")
        if user_is_member(current_user.id, league.id):
            flash("You're already in that league.", "info")
            return redirect(url_for("leagues.detail", league_id=league.id))
        db.session.add(LeagueMembership(league_id=league.id, user_id=current_user.id))
        db.session.commit()
        flash(f"Joined {league.name}.", "success")
        return redirect(url_for("leagues.detail", league_id=league.id))
    return render_template("leagues/join.html", form=form, title="Join league")


@leagues_bp.route("/<int:league_id>")
@login_required
def detail(league_id: int):
    if not user_is_member(current_user.id, league_id):
        abort(404)
    league = db.session.get(League, league_id)
    members = (
        db.session.query(LeagueMembership)
        .filter_by(league_id=league_id)
        .all()
    )
    rename_form = RenameLeagueForm(name=league.name)
    return render_template(
        "leagues/detail.html",
        league=league,
        members=members,
        is_admin=user_is_admin_of(current_user.id, league_id),
        rename_form=rename_form,
        bare_csrf=FlaskForm(),
        title=league.name,
    )


@leagues_bp.route("/<int:league_id>/rename", methods=["POST"])
@login_required
def rename(league_id: int):
    if not user_is_admin_of(current_user.id, league_id):
        abort(403)
    league = db.session.get(League, league_id)
    form = RenameLeagueForm()
    if form.validate_on_submit():
        league.name = form.name.data.strip()
        db.session.commit()
        flash("League renamed.", "success")
    return redirect(url_for("leagues.detail", league_id=league_id))


@leagues_bp.route("/<int:league_id>/leave", methods=["POST"])
@login_required
def leave(league_id: int):
    form = FlaskForm()
    if not form.validate_on_submit():
        abort(400)
    league = db.session.get(League, league_id)
    if league is None:
        abort(404)
    if league.created_by_id == current_user.id:
        flash("You can't leave a league you created — delete it instead.", "error")
        return redirect(url_for("leagues.detail", league_id=league_id))
    membership = db.session.query(LeagueMembership).filter_by(
        league_id=league_id, user_id=current_user.id,
    ).first()
    if membership:
        db.session.delete(membership)
        db.session.commit()
    flash("Left the league.", "info")
    return redirect(url_for("leagues.index"))


@leagues_bp.route("/<int:league_id>/remove/<int:user_id>", methods=["POST"])
@login_required
def remove_member(league_id: int, user_id: int):
    if not user_is_admin_of(current_user.id, league_id):
        abort(403)
    form = FlaskForm()
    if not form.validate_on_submit():
        abort(400)
    if user_id == current_user.id:
        flash("Use Delete League instead of removing yourself.", "error")
        return redirect(url_for("leagues.detail", league_id=league_id))
    membership = db.session.query(LeagueMembership).filter_by(
        league_id=league_id, user_id=user_id,
    ).first()
    if membership:
        db.session.delete(membership)
        db.session.commit()
        flash("Member removed.", "success")
    return redirect(url_for("leagues.detail", league_id=league_id))


@leagues_bp.route("/<int:league_id>/delete", methods=["POST"])
@login_required
def delete_league(league_id: int):
    if not user_is_admin_of(current_user.id, league_id):
        abort(403)
    form = FlaskForm()
    if not form.validate_on_submit():
        abort(400)
    league = db.session.get(League, league_id)
    if league:
        db.session.delete(league)
        db.session.commit()
        flash("League deleted.", "info")
    return redirect(url_for("leagues.index"))


# =============================================================================
# Helpers
# =============================================================================


def _generate_unique_invite_code() -> str:
    """Try a few random codes until we find one not already in use."""
    for _ in range(20):
        code = League.generate_invite_code()
        if not db.session.query(League).filter_by(invite_code=code).first():
            return code
    raise RuntimeError("Could not allocate a unique invite code")
