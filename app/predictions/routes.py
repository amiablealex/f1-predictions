"""Predictions blueprint.

Shows the editable form for the active round and handles submission.
Once the round is locked, redirects the user to the round-view page.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from flask_wtf import FlaskForm

from app.api.jolpica import parse_lap_time
from app.extensions import db, csrf
from app.models.prediction import (
    DnfCountPrediction,
    FastestLapPrediction,
    PoleTimePrediction,
    Top3QualiPrediction,
    Top3SprintPrediction,
    Top10Prediction,
)
from app.models.round import Round, WeekendType
from app.utils import (
    get_current_round,
    get_round_by_number,
    round_driver_choices,
)

predictions_bp = Blueprint("predictions", __name__, template_folder="../templates")

log = logging.getLogger(__name__)


def _utcnow():
    return datetime.now(timezone.utc)


def _resolve_active_round() -> Round | None:
    from flask import current_app
    return get_current_round(current_app.config["F1_SEASON"])


@predictions_bp.route("/predictions", methods=["GET"])
@login_required
def edit():
    """Show the prediction form for the current round (or the round view
    if it's already locked)."""
    rd = _resolve_active_round()
    if rd is None:
        return render_template("predictions/no_round.html", title="Predictions")

    if rd.predictions_locked or (rd.predictions_deadline and rd.predictions_deadline <= _utcnow()):
        return redirect(url_for("rounds.view", season=rd.season, round_number=rd.round_number))

    choices = round_driver_choices(rd)
    if not choices:
        return render_template(
            "predictions/no_drivers.html",
            round_obj=rd, title="Predictions",
        )

    # Load any existing draft
    existing = _load_draft(rd.id, current_user.id)
    form = FlaskForm()  # CSRF only

    return render_template(
        "predictions/edit.html",
        round_obj=rd,
        choices=choices,
        existing=existing,
        is_sprint=(rd.weekend_type == WeekendType.SPRINT),
        form=form,
        title="Predictions",
    )


@predictions_bp.route("/predictions", methods=["POST"])
@login_required
def submit():
    """Validate and save predictions. Re-renders the form on validation
    error; redirects to the round view on success."""
    form = FlaskForm()
    if not form.validate_on_submit():
        flash("Form expired. Please try again.", "error")
        return redirect(url_for("predictions.edit"))

    rd = _resolve_active_round()
    if rd is None:
        flash("There's no active round to submit for.", "error")
        return redirect(url_for("rounds.current"))
    if rd.predictions_locked or (rd.predictions_deadline and rd.predictions_deadline <= _utcnow()):
        flash("Predictions are locked for this round.", "error")
        return redirect(url_for("rounds.view", season=rd.season, round_number=rd.round_number))

    choices = round_driver_choices(rd)
    valid_driver_ids = {c.driver_id for c in choices}
    is_sprint = (rd.weekend_type == WeekendType.SPRINT)

    errors: list[str] = []
    payload = _parse_form(request.form, valid_driver_ids, is_sprint, errors)

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("predictions.edit"))

    _save_predictions(rd.id, current_user.id, payload, is_sprint)
    db.session.commit()
    flash("Predictions saved.", "success")
    return redirect(url_for("predictions.edit"))


# =============================================================================
# Form parsing
# =============================================================================


def _int_or_none(s: str | None) -> int | None:
    if s is None:
        return None
    s = s.strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_form(form, valid_driver_ids, is_sprint, errors) -> dict:
    """Pull all prediction values out of a posted form. Records errors
    (in-place) for display rather than raising."""
    payload: dict = {
        "top10": {},
        "quali_top3": {},
        "sprint_top3": {},
        "pole_time_ms": None,
        "fastest_lap": None,
        "dnf_count": None,
    }

    # ---- race top 10 ----
    seen_top10: set[int] = set()
    for pos in range(1, 11):
        raw = form.get(f"top10_{pos}", "").strip()
        if raw == "":
            continue
        d_id = _int_or_none(raw)
        if d_id is None or d_id not in valid_driver_ids:
            errors.append(f"Race position {pos}: invalid driver.")
            continue
        if d_id in seen_top10:
            errors.append(f"Race position {pos}: each driver can only appear once.")
            continue
        seen_top10.add(d_id)
        payload["top10"][pos] = d_id

    # ---- quali top 3 ----
    seen_q: set[int] = set()
    for pos in range(1, 4):
        raw = form.get(f"quali_top3_{pos}", "").strip()
        if raw == "":
            continue
        d_id = _int_or_none(raw)
        if d_id is None or d_id not in valid_driver_ids:
            errors.append(f"Quali position {pos}: invalid driver.")
            continue
        if d_id in seen_q:
            errors.append(f"Quali position {pos}: each driver can only appear once.")
            continue
        seen_q.add(d_id)
        payload["quali_top3"][pos] = d_id

    # ---- pole time (M:SS.mmm) ----
    pole_raw = form.get("pole_time", "").strip()
    if pole_raw:
        ms = parse_lap_time(pole_raw)
        if ms is None:
            errors.append("Pole time must be in the format 'M:SS.mmm' (e.g. 1:23.456).")
        else:
            payload["pole_time_ms"] = ms

    # ---- sprint-only fields ----
    if is_sprint:
        seen_sp: set[int] = set()
        for pos in range(1, 4):
            raw = form.get(f"sprint_top3_{pos}", "").strip()
            if raw == "":
                continue
            d_id = _int_or_none(raw)
            if d_id is None or d_id not in valid_driver_ids:
                errors.append(f"Sprint race position {pos}: invalid driver.")
                continue
            if d_id in seen_sp:
                errors.append(f"Sprint race position {pos}: each driver can only appear once.")
                continue
            seen_sp.add(d_id)
            payload["sprint_top3"][pos] = d_id

    # ---- fastest lap ----
    fl_raw = form.get("fastest_lap", "").strip()
    if fl_raw:
        d_id = _int_or_none(fl_raw)
        if d_id is None or d_id not in valid_driver_ids:
            errors.append("Fastest lap: invalid driver.")
        else:
            payload["fastest_lap"] = d_id

    # ---- DNF count ----
    dnf_raw = form.get("dnf_count", "").strip()
    if dnf_raw:
        n = _int_or_none(dnf_raw)
        if n is None or n < 0 or n > 20:
            errors.append("DNF count must be between 0 and 20.")
        else:
            payload["dnf_count"] = n

    return payload


# =============================================================================
# Save
# =============================================================================


def _load_draft(round_id: int, user_id: int) -> dict:
    """Pull current predictions into a flat dict keyed by form field name,
    so the template can pre-fill fields."""
    out: dict = {}

    for p in db.session.query(Top10Prediction).filter_by(user_id=user_id, round_id=round_id):
        out[f"top10_{p.position}"] = p.predicted_driver_id
    for p in db.session.query(Top3QualiPrediction).filter_by(user_id=user_id, round_id=round_id):
        out[f"quali_top3_{p.position}"] = p.predicted_driver_id
    for p in db.session.query(Top3SprintPrediction).filter_by(user_id=user_id, round_id=round_id):
        out[f"sprint_top3_{p.position}"] = p.predicted_driver_id

    pt = db.session.query(PoleTimePrediction).filter_by(user_id=user_id, round_id=round_id).first()
    if pt is not None:
        from app.api.jolpica import format_lap_time
        out["pole_time"] = format_lap_time(pt.predicted_time_ms)

    fl = db.session.query(FastestLapPrediction).filter_by(user_id=user_id, round_id=round_id).first()
    if fl is not None:
        out["fastest_lap"] = fl.predicted_driver_id

    dn = db.session.query(DnfCountPrediction).filter_by(user_id=user_id, round_id=round_id).first()
    if dn is not None:
        out["dnf_count"] = dn.predicted_count

    return out


def _save_predictions(round_id: int, user_id: int, payload: dict, is_sprint: bool) -> None:
    """Replace prediction rows for this user/round with the new payload."""

    # Wholesale-replace each section so removing a slot wipes its row.
    db.session.query(Top10Prediction).filter_by(user_id=user_id, round_id=round_id).delete()
    db.session.query(Top3QualiPrediction).filter_by(user_id=user_id, round_id=round_id).delete()
    db.session.query(Top3SprintPrediction).filter_by(user_id=user_id, round_id=round_id).delete()
    db.session.query(PoleTimePrediction).filter_by(user_id=user_id, round_id=round_id).delete()
    db.session.query(FastestLapPrediction).filter_by(user_id=user_id, round_id=round_id).delete()
    db.session.query(DnfCountPrediction).filter_by(user_id=user_id, round_id=round_id).delete()
    db.session.flush()

    for pos, d_id in payload["top10"].items():
        db.session.add(Top10Prediction(
            user_id=user_id, round_id=round_id, position=pos, predicted_driver_id=d_id,
        ))
    for pos, d_id in payload["quali_top3"].items():
        db.session.add(Top3QualiPrediction(
            user_id=user_id, round_id=round_id, position=pos, predicted_driver_id=d_id,
        ))
    if is_sprint:
        for pos, d_id in payload["sprint_top3"].items():
            db.session.add(Top3SprintPrediction(
                user_id=user_id, round_id=round_id, position=pos, predicted_driver_id=d_id,
            ))
    if payload["pole_time_ms"] is not None:
        db.session.add(PoleTimePrediction(
            user_id=user_id, round_id=round_id, predicted_time_ms=payload["pole_time_ms"],
        ))
    if payload["fastest_lap"] is not None:
        db.session.add(FastestLapPrediction(
            user_id=user_id, round_id=round_id, predicted_driver_id=payload["fastest_lap"],
        ))
    if payload["dnf_count"] is not None:
        db.session.add(DnfCountPrediction(
            user_id=user_id, round_id=round_id, predicted_count=payload["dnf_count"],
        ))
