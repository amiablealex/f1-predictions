"""Auth routes: register, login, logout, password reset, account."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.auth.email import send_password_reset_email
from app.auth.forms import (
    ChangeEmailForm,
    ChangePasswordForm,
    ChangeUsernameForm,
    DeleteAccountForm,
    ForgotPasswordForm,
    LoginForm,
    RegisterForm,
    ResetPasswordForm,
)
from app.extensions import db
from app.models.user import PasswordResetToken, User
from app.auth.rate_limit import is_blocked, record_failure, reset, retry_after_seconds

auth_bp = Blueprint("auth", __name__, template_folder="../templates")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _is_safe_redirect(target: str | None) -> bool:
    """Allow only same-host redirects after login."""
    if not target:
        return False
    parsed = urlparse(target)
    return parsed.scheme == "" and parsed.netloc == "" and target.startswith("/")


def _utcnow():
    return datetime.now(timezone.utc)


# -----------------------------------------------------------------------------
# Register
# -----------------------------------------------------------------------------


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    form = RegisterForm()
    if form.validate_on_submit():
        user = User(
            email=form.email.data.strip().lower(),
            username=form.username.data.strip(),
        )
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()

        login_user(user, remember=True)
        user.last_login_at = _utcnow()
        db.session.commit()

        flash("Account created — welcome.", "success")
        return redirect(url_for("index"))

    return render_template("auth/register.html", form=form, title="Create account")


# -----------------------------------------------------------------------------
# Login / Logout
# -----------------------------------------------------------------------------

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    form = LoginForm()
    ip = request.remote_addr or "?"

    if request.method == "POST" and is_blocked(ip):
        wait = retry_after_seconds(ip)
        minutes = max(1, (wait + 59) // 60)
        flash(
            f"Too many failed attempts. Try again in {minutes} minute(s).",
            "error",
        )
        return render_template("auth/login.html", form=form, title="Sign in"), 429

    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = db.session.query(User).filter_by(email=email).first()
        if user is None or not user.check_password(form.password.data):
            record_failure(ip)
            flash("Email or password incorrect.", "error")
            return render_template("auth/login.html", form=form, title="Sign in"), 401

        reset(ip)
        login_user(user, remember=form.remember.data)
        user.last_login_at = _utcnow()
        db.session.commit()

        next_url = request.args.get("next")
        if _is_safe_redirect(next_url):
            return redirect(next_url)
        return redirect(url_for("index"))

    return render_template("auth/login.html", form=form, title="Sign in")


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Signed out.", "info")
    return redirect(url_for("auth.login"))


# -----------------------------------------------------------------------------
# Forgot / reset password
# -----------------------------------------------------------------------------


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = db.session.query(User).filter_by(email=email).first()
        if user is not None:
            ttl = current_app.config["PASSWORD_RESET_TOKEN_TTL_HOURS"]
            token = PasswordResetToken.issue(user=user, ttl_hours=ttl)
            db.session.add(token)
            db.session.commit()
            send_password_reset_email(user, token)
        # Identical message regardless of whether the email exists.
        flash(
            "If an account exists for that email, a reset link is on its way.",
            "info",
        )
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html", form=form, title="Forgot password")


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    reset = db.session.query(PasswordResetToken).filter_by(token=token).first()
    if reset is None or not reset.is_valid:
        flash("That reset link is invalid or has expired.", "error")
        return redirect(url_for("auth.forgot_password"))

    form = ResetPasswordForm()
    if form.validate_on_submit():
        reset.user.set_password(form.password.data)
        reset.consume()
        db.session.commit()
        flash("Password updated. You can now sign in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", form=form, title="Reset password")


# -----------------------------------------------------------------------------
# Account
# -----------------------------------------------------------------------------


@auth_bp.route("/account", methods=["GET"])
@login_required
def account():
    username_form = ChangeUsernameForm(
        current_user_id=current_user.id, username=current_user.username
    )
    email_form = ChangeEmailForm(
        current_user_id=current_user.id, email=current_user.email
    )
    password_form = ChangePasswordForm()
    delete_form = DeleteAccountForm()
    return render_template(
        "auth/account.html",
        username_form=username_form,
        email_form=email_form,
        password_form=password_form,
        delete_form=delete_form,
        title="Account",
    )


@auth_bp.route("/account/username", methods=["POST"])
@login_required
def change_username():
    form = ChangeUsernameForm(current_user_id=current_user.id)
    if form.validate_on_submit():
        current_user.username = form.username.data.strip()
        db.session.commit()
        flash("Display name updated.", "success")
    else:
        for errors in form.errors.values():
            for err in errors:
                flash(err, "error")
    return redirect(url_for("auth.account"))


@auth_bp.route("/account/email", methods=["POST"])
@login_required
def change_email():
    form = ChangeEmailForm(current_user_id=current_user.id)
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash("Current password incorrect.", "error")
            return redirect(url_for("auth.account"))
        current_user.email = form.email.data.strip().lower()
        db.session.commit()
        flash("Email updated.", "success")
    else:
        for errors in form.errors.values():
            for err in errors:
                flash(err, "error")
    return redirect(url_for("auth.account"))


@auth_bp.route("/account/password", methods=["POST"])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash("Current password incorrect.", "error")
            return redirect(url_for("auth.account"))
        current_user.set_password(form.new_password.data)
        db.session.commit()
        flash("Password updated.", "success")
    else:
        for errors in form.errors.values():
            for err in errors:
                flash(err, "error")
    return redirect(url_for("auth.account"))


@auth_bp.route("/account/delete", methods=["POST"])
@login_required
def delete_account():
    form = DeleteAccountForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash("Current password incorrect.", "error")
            return redirect(url_for("auth.account"))

        user_id = current_user.id
        logout_user()
        user = db.session.get(User, user_id)
        if user is not None:
            db.session.delete(user)
            db.session.commit()
        flash("Account deleted. Goodbye.", "info")
        return redirect(url_for("auth.login"))

    for errors in form.errors.values():
        for err in errors:
            flash(err, "error")
    return redirect(url_for("auth.account"))
