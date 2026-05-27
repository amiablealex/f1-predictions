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
    PlacesGainedPrediction,
    PoleTimePrediction,
    QualiHeadToHeadPrediction,
    QualiNthPrediction,
    QualiRandomDriverPrediction,
    SpecialPrediction,
    Top3QualiPrediction,
    Top3SprintPrediction,
    Top10Prediction,
)
from app.specials import SPECIALS_BY_KEY
from app.models.round import Round, WeekendType
from app.utils import (
    get_active_round,
    get_current_round,
    get_round_by_number,
    round_driver_choices,
    round_team_choices,
)

predictions_bp = Blueprint("predictions", __name__, template_folder="../templates")

log = logging.getLogger(__name__)


def _utcnow():
    return datetime.now(timezone.utc)


def _resolve_active_round() -> Round | None:
    from flask import current_app
    return get_current_round(current_app.config["F1_SEASON"])


def _displayed_active_round() -> Round | None:
    """Locked-but-not-completed round, for the cross-banner on the form."""
    from flask import current_app
    return get_active_round(current_app.config["F1_SEASON"])


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

    existing = _load_draft(rd.id, current_user.id)
    form = FlaskForm()  # CSRF only

    return render_template(
        "predictions/edit.html",
        round_obj=rd,
        choices=choices,
        team_choices=round_team_choices(rd),
        existing=existing,
        is_sprint=(rd.weekend_type == WeekendType.SPRINT),
        random_driver=rd.random_quali_driver,  # may be None if worker hasn't picked yet
        qh2h_choices=[
            c for c in choices
            if rd.qh2h_driver_a and rd.qh2h_driver_b
            and c.driver_id in (
                rd.qh2h_driver_a.expected_driver_id,
                rd.qh2h_driver_b.expected_driver_id,
            )
        ],
        qh2h_a=rd.qh2h_driver_a,
        qh2h_b=rd.qh2h_driver_b,
        quali_nth_position=rd.quali_nth_position,
        active_specials=[
            SPECIALS_BY_KEY[k] for k in (rd.special_a_key, rd.special_b_key)
            if k and k in SPECIALS_BY_KEY
        ],
        form=form,
        title="Predictions",
        active_round=_displayed_active_round(),
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
        return render_template(
            "predictions/edit.html",
            round_obj=rd,
            choices=choices,
            team_choices=round_team_choices(rd),
            existing=request.form.to_dict(),
            is_sprint=is_sprint,
            random_driver=rd.random_quali_driver,
            qh2h_choices=[
                c for c in choices
                if rd.qh2h_driver_a and rd.qh2h_driver_b
                and c.driver_id in (
                    rd.qh2h_driver_a.expected_driver_id,
                    rd.qh2h_driver_b.expected_driver_id,
                )
            ],
            qh2h_a=rd.qh2h_driver_a,
            qh2h_b=rd.qh2h_driver_b,
            quali_nth_position=rd.quali_nth_position,
            active_specials=[
                SPECIALS_BY_KEY[k] for k in (rd.special_a_key, rd.special_b_key)
                if k and k in SPECIALS_BY_KEY
            ],
            form=form,
            title="Predictions",
            active_round=_displayed_active_round(),
        )

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
        "places_gained": None,
        "quali_random_driver": None,
        "qh2h": None,
        "qnth": None,
        "specials": {},   # {special_key: {"driver": int|None, "int": int|None, "bool": bool|None, "team": str|None}}
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

    # ---- random-driver quali position ----
    rqd_raw = form.get("quali_random_driver", "").strip()
    if rqd_raw:
        n = _int_or_none(rqd_raw)
        if n is None or n < 1 or n > 30:
            errors.append("Random driver position must be between 1 and 30.")
        else:
            payload["quali_random_driver"] = n

    # ---- quali head-to-head ----
    qh2h_raw = form.get("qh2h", "").strip()
    if qh2h_raw:
        d_id = _int_or_none(qh2h_raw)
        if d_id is None or d_id not in valid_driver_ids:
            errors.append("Head-to-head: invalid driver.")
        else:
            payload["qh2h"] = d_id

    # ---- quali Nth ----
    qnth_raw = form.get("qnth", "").strip()
    if qnth_raw:
        d_id = _int_or_none(qnth_raw)
        if d_id is None or d_id not in valid_driver_ids:
            errors.append("Who will qualify Nth: invalid driver.")
        else:
            payload["qnth"] = d_id

    # ---- specials (zero, one, or two entries) ----
    # Form fields:
    #   special_<key>_driver  → int driver_id
    #   special_<key>_int     → int
    #   special_<key>_bool    → "yes"/"no"
    #   special_<key>_team    → team name string
    for field_name in form.keys():
        if not field_name.startswith("special_"):
            continue
        # Strip the leading "special_" and the trailing "_<suffix>".
        for suffix in ("_driver", "_int", "_bool", "_team"):
            if field_name.endswith(suffix):
                key = field_name[len("special_"):-len(suffix)]
                payload["specials"].setdefault(
                    key, {"driver": None, "int": None, "bool": None, "team": None},
                )
                raw = form.get(field_name, "").strip()
                if raw == "":
                    break
                if suffix == "_driver":
                    d_id = _int_or_none(raw)
                    if d_id is None or d_id not in valid_driver_ids:
                        errors.append(f"Special '{key}': invalid driver.")
                    else:
                        payload["specials"][key]["driver"] = d_id
                elif suffix == "_int":
                    n = _int_or_none(raw)
                    if n is None or n < 0 or n > 200:
                        errors.append(f"Special '{key}': value must be 0–200.")
                    else:
                        payload["specials"][key]["int"] = n
                elif suffix == "_bool":
                    if raw.lower() in ("yes", "true", "1"):
                        payload["specials"][key]["bool"] = True
                    elif raw.lower() in ("no", "false", "0"):
                        payload["specials"][key]["bool"] = False
                    else:
                        errors.append(f"Special '{key}': must be yes or no.")
                elif suffix == "_team":
                    payload["specials"][key]["team"] = raw
                break

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

    # ---- places gained ----
    pg_raw = form.get("places_gained", "").strip()
    if pg_raw:
        d_id = _int_or_none(pg_raw)
        if d_id is None or d_id not in valid_driver_ids:
            errors.append("Places gained: invalid driver.")
        else:
            payload["places_gained"] = d_id

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

    pg = db.session.query(PlacesGainedPrediction).filter_by(user_id=user_id, round_id=round_id).first()
    if pg is not None:
        out["places_gained"] = pg.predicted_driver_id

    rqd = db.session.query(QualiRandomDriverPrediction).filter_by(user_id=user_id, round_id=round_id).first()
    if rqd is not None:
        out["quali_random_driver"] = rqd.predicted_position

    h2h = db.session.query(QualiHeadToHeadPrediction).filter_by(user_id=user_id, round_id=round_id).first()
    if h2h is not None:
        out["qh2h"] = h2h.predicted_driver_id

    qnth = db.session.query(QualiNthPrediction).filter_by(user_id=user_id, round_id=round_id).first()
    if qnth is not None:
        out["qnth"] = qnth.predicted_driver_id

    for sp in db.session.query(SpecialPrediction).filter_by(user_id=user_id, round_id=round_id):
        if sp.predicted_driver_id is not None:
            out[f"special_{sp.special_key}_driver"] = sp.predicted_driver_id
        if sp.predicted_int is not None:
            out[f"special_{sp.special_key}_int"] = sp.predicted_int
        if sp.predicted_bool is not None:
            out[f"special_{sp.special_key}_bool"] = "yes" if sp.predicted_bool else "no"
        if sp.predicted_team_name is not None:
            out[f"special_{sp.special_key}_team"] = sp.predicted_team_name

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
    db.session.query(PlacesGainedPrediction).filter_by(user_id=user_id, round_id=round_id).delete()
    db.session.query(QualiRandomDriverPrediction).filter_by(user_id=user_id, round_id=round_id).delete()
    db.session.query(QualiHeadToHeadPrediction).filter_by(user_id=user_id, round_id=round_id).delete()
    db.session.query(QualiNthPrediction).filter_by(user_id=user_id, round_id=round_id).delete()
    db.session.query(SpecialPrediction).filter_by(user_id=user_id, round_id=round_id).delete()
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
    if payload["places_gained"] is not None:
        db.session.add(PlacesGainedPrediction(
            user_id=user_id, round_id=round_id, predicted_driver_id=payload["places_gained"],
        ))
    if payload["quali_random_driver"] is not None:
        db.session.add(QualiRandomDriverPrediction(
            user_id=user_id, round_id=round_id, predicted_position=payload["quali_random_driver"],
        ))
    if payload["qh2h"] is not None:
        db.session.add(QualiHeadToHeadPrediction(
            user_id=user_id, round_id=round_id, predicted_driver_id=payload["qh2h"],
        ))
    if payload["qnth"] is not None:
        db.session.add(QualiNthPrediction(
            user_id=user_id, round_id=round_id, predicted_driver_id=payload["qnth"],
        ))
    for key, values in payload["specials"].items():
        if all(v is None for v in values.values()):
            continue
        db.session.add(SpecialPrediction(
            user_id=user_id, round_id=round_id, special_key=key,
            predicted_driver_id=values["driver"],
            predicted_int=values["int"],
            predicted_bool=values["bool"],
            predicted_team_name=values["team"],
        ))
