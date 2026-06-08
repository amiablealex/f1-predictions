"""Contribution definition form parsing.

Turns the contributor's posted form into a validated payload and applies it
to a ContributionDefinition. Mirrors the parse/save split in
app/predictions/routes.py. Validation rules live in
app/scoring/contributions.py (single source of truth).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.api.jolpica import parse_lap_time
from app.models.contribution import ContributionDefinition
from app.scoring.contributions import (
    CUSTOM_CHOICE, DECIMAL, DISCRETE_TYPES, DRIVER_PICK, INTEGER, INPUT_TYPES,
    LAP_TIME, TEAM_PICK, generate_blurb, validate_definition,
)


def _int_or_none(s):
    if s is None:
        return None
    s = str(s).strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _decimal_or_none(s):
    if s is None:
        return None
    s = str(s).strip()
    if s == "":
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def parse_definition_form(form, valid_driver_ids, valid_team_names):
    """Parse the posted definition form.

    Returns (payload, errors, warnings). `payload` is a dict of normalised
    values ready for apply_definition; only meaningful when errors is empty.
    """
    errors: list[str] = []

    question_text = (form.get("question_text") or "").strip()
    if not question_text:
        errors.append("A question is required.")
    if len(question_text) > 200:
        errors.append("Question must be 200 characters or fewer.")

    input_type = (form.get("input_type") or "").strip()
    if input_type not in INPUT_TYPES:
        errors.append("Choose an input type.")
        return {}, errors, []

    primary_points = _int_or_none(form.get("primary_points"))
    primary_mode = (form.get("primary_mode") or "").strip()
    primary_range = _decimal_or_none(form.get("primary_range"))

    has_secondary = bool((form.get("secondary_points") or "").strip())
    secondary_points = _int_or_none(form.get("secondary_points")) if has_secondary else None
    secondary_range = _decimal_or_none(form.get("secondary_range")) if has_secondary else None

    # Discrete types are always exact, single-tier — normalise before validate
    # so a stale posted mode/secondary (from switching type in the UI) can't
    # leak through.
    if input_type in DISCRETE_TYPES:
        primary_mode = "exact"
        primary_range = None
        secondary_points = None
        secondary_range = None

    # Option sets — only parse the one relevant to the chosen type.
    allowed_driver_ids = None
    allowed_team_names = None
    custom_options = None
    if input_type == DRIVER_PICK:
        ids = [
            _int_or_none(v) for v in form.getlist("allowed_driver_ids")
        ]
        ids = [i for i in ids if i is not None]
        allowed_driver_ids = ids or None  # empty = unrestricted
    elif input_type == TEAM_PICK:
        names = [v.strip() for v in form.getlist("allowed_team_names") if v.strip()]
        allowed_team_names = names or None
    elif input_type == CUSTOM_CHOICE:
        # Free-text textarea, one option per line.
        raw = form.get("custom_options") or ""
        opts, seen = [], set()
        for line in raw.splitlines():
            line = line.strip()
            if line and line not in seen:
                seen.add(line)
                opts.append(line)
        custom_options = opts or None

    errors_v, warnings = validate_definition(
        input_type=input_type,
        primary_points=primary_points,
        primary_mode=primary_mode,
        primary_range=primary_range,
        secondary_points=secondary_points,
        secondary_range=secondary_range,
        allowed_driver_ids=allowed_driver_ids,
        allowed_team_names=allowed_team_names,
        custom_options=custom_options,
        valid_driver_ids=valid_driver_ids,
        valid_team_names=valid_team_names,
    )
    errors.extend(errors_v)

    if errors:
        return {}, errors, warnings

    # Blurb: use the contributor's text if they edited it, else autogenerate.
    blurb = (form.get("scoring_blurb") or "").strip()
    if not blurb:
        blurb = generate_blurb(
            input_type=input_type,
            primary_points=primary_points,
            primary_mode=primary_mode,
            primary_range=primary_range,
            secondary_points=secondary_points,
            secondary_range=secondary_range,
        )

    payload = {
        "question_text": question_text,
        "scoring_blurb": blurb[:200],
        "input_type": input_type,
        "allowed_driver_ids": allowed_driver_ids,
        "allowed_team_names": allowed_team_names,
        "custom_options": custom_options,
        "primary_points": primary_points,
        "primary_mode": primary_mode,
        "primary_range": primary_range,
        "secondary_points": secondary_points,
        "secondary_range": secondary_range,
    }
    return payload, errors, warnings


# Fields that, when changed, invalidate existing user predictions.
_MATERIAL_FIELDS = ("input_type", "allowed_driver_ids", "allowed_team_names", "custom_options")


def is_material_change(definition: ContributionDefinition, payload: dict) -> bool:
    """True if applying `payload` would invalidate stored predictions.

    Material = the answer's shape changes: input type, or the allowed
    option set. Wording/points/range changes are NOT material — stored
    values stay structurally valid.
    """
    def _norm(v):
        # Normalise lists for order-insensitive comparison; driver-id lists
        # are sets of ints, others sets of strings.
        if isinstance(v, list):
            return set(v)
        return v

    for f in _MATERIAL_FIELDS:
        if _norm(getattr(definition, f)) != _norm(payload.get(f)):
            return True
    return False


def apply_definition(definition: ContributionDefinition, payload: dict) -> None:
    """Copy a parsed payload onto a definition (create or edit)."""
    for k, v in payload.items():
        setattr(definition, k, v)
