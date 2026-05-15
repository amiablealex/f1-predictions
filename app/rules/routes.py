"""Rules / how-to-play.

Renders dynamically from the live scoring config so the page is always
consistent with what's actually being scored.
"""
from flask import Blueprint, current_app, render_template

rules_bp = Blueprint("rules", __name__, template_folder="../templates")


@rules_bp.route("/")
def index():
    cfg = current_app.config["SCORING_DEFAULTS"]
    deadline_offset = current_app.config["DEADLINE_OFFSET_MINUTES"]
    return render_template(
        "rules/rules.html",
        cfg=cfg,
        deadline_offset_minutes=deadline_offset,
        title="How to play",
    )

@rules_bp.route("/about")
def about():
    return render_template("static_pages/about.html", title="About")


@rules_bp.route("/privacy")
def privacy():
    return render_template("static_pages/privacy.html", title="Privacy")
