"""Contributions (wildcards) blueprint.

A contributor authors one wildcard prediction per round — a free-form
question scored on a manually-entered actual. Routes:

  GET  /contribute                          dashboard (this contributor's rounds)
  GET|POST /contribute/round/<rid>          create/edit the definition
  POST /contribute/round/<rid>/delete       delete the definition
  GET|POST /contribute/round/<rid>/actual   enter the actual + score

The definition is editable until the day-before-deadline cutoff
(contribution_window_open). The actual is submittable once the round's
predictions are locked. Editing in a way that changes the answer's shape,
when users have already predicted, requires explicit confirmation and
blanks those predictions on save.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template, request, url_for,
)
from flask_login import current_user, login_required
from flask_wtf import FlaskForm

from app.api.jolpica import parse_lap_time
from app.contributions.forms import (
    apply_definition, is_material_change, parse_definition_form,
)
from app.extensions import db
from app.models.contribution import ContributionDefinition, ContributionPrediction
from app.models.driver import Driver
from app.models.prediction import PredictionScore, PredictionType
from app.models.round import Round
from app.scoring.contributions import (
    BOOL, CUSTOM_CHOICE, DECIMAL, DRIVER_PICK, INTEGER, INPUT_TYPE_DEFS,
    LAP_TIME, TEAM_PICK, score_contribution,
)
from app.utils import (
    contribution_edit_cutoff, contribution_prediction_count, contribution_window_open,
    contributor_required, round_driver_choices, round_team_choices,
)

contributions_bp = Blueprint("contributions", __name__, template_folder="../templates")

log = logging.getLogger(__name__)


def _utcnow():
    return datetime.now(timezone.utc)


def _season_rounds():
    return (
        db.session.query(Round)
        .filter(Round.season == current_app.config["F1_SEASON"])
        .order_by(Round.round_number.asc())
        .all()
    )


def _definition_for(round_id: int, contributor_id: int) -> ContributionDefinition | None:
    return (
        db.session.query(ContributionDefinition)
        .filter_by(round_id=round_id, contributor_id=contributor_id)
        .one_or_none()
    )


# =============================================================================
# Dashboard
# =============================================================================


@contributions_bp.route("/")
@login_required
@contributor_required
def dashboard():
    rounds = _season_rounds()
    defs = {
        d.round_id: d for d in db.session.query(ContributionDefinition)
        .filter_by(contributor_id=current_user.id).all()
    }
    now = _utcnow()
    rows = []
    for r in rounds:
        d = defs.get(r.id)
        rows.append({
            "round": r,
            "definition": d,
            "is_set": d is not None,
            "window_open": contribution_window_open(r, now),
            "is_locked": r.predictions_locked,
            "has_actual": d.has_actual if d else False,
            "pred_count": contribution_prediction_count(d.id) if d else 0,
        })
    return render_template(
        "contributions/dashboard.html",
        rows=rows, title="Contribute",
    )


# =============================================================================
# Definition create / edit
# =============================================================================


@contributions_bp.route("/round/<int:round_id>", methods=["GET", "POST"])
@login_required
@contributor_required
def edit(round_id: int):
    rd = db.session.get(Round, round_id)
    if rd is None:
        abort(404)
    definition = _definition_for(round_id, current_user.id)

    if definition is not None and definition.has_actual:
        flash("This wildcard has an actual result and can no longer be edited.", "error")
        return redirect(url_for("contributions.dashboard"))

    if not contribution_window_open(rd):
        flash("The editing window for this round has closed.", "error")
        return redirect(url_for("contributions.dashboard"))

    choices = round_driver_choices(rd)
    team_choices = round_team_choices(rd)
    valid_driver_ids = {c.driver_id for c in choices}
    valid_team_names = {t.name for t in team_choices}
    form = FlaskForm()  # CSRF only

    if request.method == "POST":
        if not form.validate_on_submit():
            flash("Form expired. Please try again.", "error")
            return redirect(url_for("contributions.edit", round_id=round_id))

        payload, errors, warnings = parse_definition_form(
            request.form, valid_driver_ids, valid_team_names,
        )
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "contributions/edit.html",
                round_obj=rd, definition=definition, choices=choices,
                team_choices=team_choices, input_type_defs=INPUT_TYPE_DEFS,
                form=form, existing=request.form, title="Contribute",
                pred_count=contribution_prediction_count(definition.id) if definition else 0,
                confirm_needed=False,
            )

        # Materiality / reset gate — only relevant when editing an existing
        # definition that users have already predicted on.
        is_edit = definition is not None
        count = contribution_prediction_count(definition.id) if is_edit else 0
        material = is_edit and is_material_change(definition, payload)
        confirmed = request.form.get("confirm_reset") == "yes"

        if material and count > 0 and not confirmed:
            # Re-render with the confirm gate. Carry the posted values so the
            # contributor's edits survive the round-trip.
            for wmsg in warnings:
                flash(wmsg, "info")
            return render_template(
                "contributions/edit.html",
                round_obj=rd, definition=definition, choices=choices,
                team_choices=team_choices, input_type_defs=INPUT_TYPE_DEFS,
                form=form, existing=request.form, title="Contribute",
                pred_count=count, confirm_needed=True,
            )

        # Apply.
        if definition is None:
            definition = ContributionDefinition(
                contributor_id=current_user.id, round_id=round_id,
            )
            db.session.add(definition)
        apply_definition(definition, payload)

        # If a confirmed material change, blank existing predictions (scores
        # don't exist yet — the race hasn't happened).
        if material and count > 0 and confirmed:
            db.session.query(ContributionPrediction).filter_by(
                contribution_id=definition.id,
            ).delete()
            flash(f"Saved. {count} existing prediction(s) were cleared.", "success")
        else:
            flash("Saved.", "success")

        for wmsg in warnings:
            flash(wmsg, "info")
        db.session.commit()
        return redirect(url_for("contributions.dashboard"))

    # GET
    return render_template(
        "contributions/edit.html",
        round_obj=rd, definition=definition, choices=choices,
        team_choices=team_choices, input_type_defs=INPUT_TYPE_DEFS,
        form=form, existing=None, title="Contribute",
        pred_count=contribution_prediction_count(definition.id) if definition else 0,
        confirm_needed=False,
    )


@contributions_bp.route("/round/<int:round_id>/delete", methods=["POST"])
@login_required
@contributor_required
def delete(round_id: int):
    rd = db.session.get(Round, round_id)
    if rd is None:
        abort(404)
    if not FlaskForm().validate_on_submit():
        abort(400)
    if not contribution_window_open(rd):
        flash("The editing window for this round has closed.", "error")
        return redirect(url_for("contributions.dashboard"))
    definition = _definition_for(round_id, current_user.id)

    if definition is not None and definition.has_actual:
        flash("This wildcard has an actual result and can no longer be deleted.", "error")
        return redirect(url_for("contributions.dashboard"))

    if definition is not None:
        db.session.delete(definition)  # cascades predictions + scores
        db.session.commit()
        flash("Wildcard deleted.", "info")
    return redirect(url_for("contributions.dashboard"))


# =============================================================================
# Actual entry + scoring
# =============================================================================


@contributions_bp.route("/round/<int:round_id>/actual", methods=["GET", "POST"])
@login_required
@contributor_required
def actual(round_id: int):
    rd = db.session.get(Round, round_id)
    if rd is None:
        abort(404)
    definition = _definition_for(round_id, current_user.id)
    if definition is None:
        flash("Set a wildcard for this round first.", "error")
        return redirect(url_for("contributions.dashboard"))
    if not rd.predictions_locked:
        flash("You can enter the actual once predictions lock.", "error")
        return redirect(url_for("contributions.dashboard"))

    choices = round_driver_choices(rd)
    team_choices = round_team_choices(rd)
    form = FlaskForm()

    if request.method == "POST":
        if not form.validate_on_submit():
            flash("Form expired. Please try again.", "error")
            return redirect(url_for("contributions.actual", round_id=round_id))
        errors = _apply_actual(definition, request.form, choices, team_choices)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "contributions/actual.html",
                round_obj=rd, definition=definition, choices=choices,
                team_choices=team_choices, form=form, title="Submit actual",
            )
        definition.actual_set_at = _utcnow()
        definition.actual_set_by_id = current_user.id
        db.session.flush()
        _score_contribution_now(definition)
        db.session.commit()
        flash("Actual saved and scored.", "success")
        return redirect(url_for("contributions.dashboard"))

    return render_template(
        "contributions/actual.html",
        round_obj=rd, definition=definition, choices=choices,
        team_choices=team_choices, form=form, title="Submit actual",
    )


def _apply_actual(definition, form, choices, team_choices) -> list[str]:
    """Validate + store the actual on the definition. Returns errors."""
    errors: list[str] = []
    it = definition.input_type
    valid_driver_ids = {c.driver_id for c in choices}
    valid_team_names = {t.name for t in team_choices}

    # Reset all actual columns first so a re-submit can change the value
    # cleanly without leaving a stale column populated.
    definition.actual_driver_id = None
    definition.actual_team_name = None
    definition.actual_int = None
    definition.actual_decimal = None
    definition.actual_lap_time_ms = None
    definition.actual_bool = None
    definition.actual_choice = None

    raw = (form.get("actual") or "").strip()
    if raw == "":
        return ["Enter the actual result."]

    if it == DRIVER_PICK:
        try:
            did = int(raw)
        except ValueError:
            return ["Invalid driver."]
        allowed = set(definition.allowed_driver_ids or valid_driver_ids)
        if did not in valid_driver_ids or did not in allowed:
            return ["Driver not valid for this wildcard."]
        definition.actual_driver_id = did
    elif it == TEAM_PICK:
        allowed = set(definition.allowed_team_names or valid_team_names)
        if raw not in valid_team_names or raw not in allowed:
            return ["Team not valid for this wildcard."]
        definition.actual_team_name = raw
    elif it == CUSTOM_CHOICE:
        if raw not in set(definition.custom_options or []):
            return ["Choose one of the configured options."]
        definition.actual_choice = raw
    elif it == BOOL:
        if raw.lower() in ("yes", "true", "1"):
            definition.actual_bool = True
        elif raw.lower() in ("no", "false", "0"):
            definition.actual_bool = False
        else:
            return ["Actual must be yes or no."]
    elif it == INTEGER:
        try:
            definition.actual_int = int(raw)
        except ValueError:
            return ["Actual must be a whole number."]
    elif it == DECIMAL:
        from decimal import Decimal, InvalidOperation
        try:
            definition.actual_decimal = Decimal(raw)
        except (InvalidOperation, ValueError):
            return ["Actual must be a number."]
    elif it == LAP_TIME:
        ms = parse_lap_time(raw)
        if ms is None:
            return ["Actual must be a lap time (M:SS.mmm)."]
        definition.actual_lap_time_ms = ms
    else:
        return ["Unknown input type."]
    return errors


def _score_contribution_now(definition: ContributionDefinition) -> None:
    """Recompute CONTRIBUTION score rows for this one wildcard.

    Delete-and-reinsert, scoped to this contribution_id, mirroring the
    worker's replace_phase_scores but entirely outside the worker. Emits a
    row for every user who predicted (0-point rows included, matching the
    engine's complete-set convention)."""
    db.session.query(PredictionScore).filter_by(
        contribution_id=definition.id,
    ).delete()
    db.session.flush()

    preds = (
        db.session.query(ContributionPrediction)
        .filter_by(contribution_id=definition.id)
        .all()
    )
    for p in preds:
        pts = score_contribution(p, definition)
        db.session.add(PredictionScore(
            user_id=p.user_id,
            round_id=definition.round_id,
            kind=PredictionType.CONTRIBUTION,
            position=None,
            special_key=None,
            contribution_id=definition.id,
            points=pts,
        ))
