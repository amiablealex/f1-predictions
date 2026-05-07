"""Email delivery via Resend.

The only transactional email this app sends is the password-reset link.
Anything else is intentional product scope.

Failures are logged but never raised to the user — the auth flow tells the
user "if an account exists, we've sent a link" regardless of outcome to
prevent email enumeration.
"""
from __future__ import annotations

import logging

import resend
from flask import current_app, render_template, url_for

from app.models.user import PasswordResetToken, User

log = logging.getLogger(__name__)


def _configure_resend() -> bool:
    """Configure the Resend SDK with the app's API key.

    Returns False (and logs a warning) if no key is configured — useful in
    development where you may not want to send real emails.
    """
    key = current_app.config.get("RESEND_API_KEY")
    if not key:
        log.warning("RESEND_API_KEY not set — email will be skipped.")
        return False
    resend.api_key = key
    return True


def send_password_reset_email(user: User, token: PasswordResetToken) -> bool:
    """Send the password-reset email.

    Returns True if the send was attempted successfully, False otherwise.
    """
    if not _configure_resend():
        # In dev: log the reset link to the console so you can still complete
        # the flow without configured email.
        reset_url = url_for("auth.reset_password", token=token.token, _external=True)
        log.info("DEV password reset link for %s: %s", user.email, reset_url)
        return False

    reset_url = url_for("auth.reset_password", token=token.token, _external=True)
    ttl_hours = current_app.config["PASSWORD_RESET_TOKEN_TTL_HOURS"]

    html_body = render_template(
        "auth/email/reset_password.html",
        username=user.username,
        reset_url=reset_url,
        ttl_hours=ttl_hours,
    )
    text_body = render_template(
        "auth/email/reset_password.txt",
        username=user.username,
        reset_url=reset_url,
        ttl_hours=ttl_hours,
    )

    from_address = (
        f"{current_app.config['RESEND_FROM_NAME']} "
        f"<{current_app.config['RESEND_FROM_EMAIL']}>"
    )

    try:
        resend.Emails.send({
            "from": from_address,
            "to": [user.email],
            "subject": "Reset your F1 Predictions password",
            "html": html_body,
            "text": text_body,
        })
        return True
    except Exception:  # pragma: no cover  (network)
        log.exception("Failed to send password reset email to %s", user.email)
        return False
