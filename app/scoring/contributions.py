"""Contributions (wildcards): catalogue, validation, scoring, blurb.

Pure functions — no DB writes. Mirrors app/scoring/specials.py: the
engine-side logic for the user-authored wildcard predictions. The input
types and the rules that constrain their scoring are the single source of
truth here, imported by the contributions blueprint (form validation) and
by the predictions form / load_round_state (rendering).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.models.contribution import ContributionDefinition, ContributionPrediction


# Input type identifiers.
DRIVER_PICK = "driver_pick"
TEAM_PICK = "team_pick"
INTEGER = "integer"
DECIMAL = "decimal"
LAP_TIME = "lap_time"
BOOL = "bool"
CUSTOM_CHOICE = "custom_choice"
POINTS_LIMIT = 25  # |points| ceiling for primary and secondary, per wildcard.

INPUT_TYPES = (DRIVER_PICK, TEAM_PICK, INTEGER, DECIMAL, LAP_TIME, BOOL, CUSTOM_CHOICE)

# Discrete answers: exact-only scoring, no secondary tier.
DISCRETE_TYPES = frozenset({DRIVER_PICK, TEAM_PICK, BOOL, CUSTOM_CHOICE})
# Numeric answers: exact-or-range, secondary tier available.
NUMERIC_TYPES = frozenset({INTEGER, DECIMAL, LAP_TIME})


@dataclass(frozen=True)
class InputTypeDef:
    key: str
    label: str        # shown in the contributor's input-type dropdown
    user_widget: str  # how the predictions form renders it (Phase 3)


INPUT_TYPE_DEFS: dict[str, InputTypeDef] = {
    DRIVER_PICK:   InputTypeDef(DRIVER_PICK, "Driver pick", "driver_select"),
    TEAM_PICK:     InputTypeDef(TEAM_PICK, "Team pick", "team_select"),
    INTEGER:       InputTypeDef(INTEGER, "Whole number", "int_input"),
    DECIMAL:       InputTypeDef(DECIMAL, "Decimal number", "decimal_input"),
    LAP_TIME:      InputTypeDef(LAP_TIME, "Lap time (M:SS.mmm)", "lap_time_input"),
    BOOL:          InputTypeDef(BOOL, "Yes / No", "bool_radio"),
    CUSTOM_CHOICE: InputTypeDef(CUSTOM_CHOICE, "Custom options", "choice_select"),
}


def _to_decimal(v) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


# =============================================================================
# Validation
# =============================================================================


def validate_definition(
    *,
    input_type: str,
    primary_points,
    primary_mode: str | None,
    primary_range,
    secondary_points,
    secondary_range,
    allowed_driver_ids: list | None,
    allowed_team_names: list | None,
    custom_options: list | None,
    valid_driver_ids: set[int],
    valid_team_names: set[str],
) -> tuple[list[str], list[str]]:
    """Validate a definition's configuration.

    Returns (errors, warnings). Errors block save; warnings allow save —
    dead configs are blocked, merely impractical ones are warned.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if input_type not in INPUT_TYPES:
        return ["Unknown input type."], warnings
    if primary_points is None:
        errors.append("Primary points are required.")
    elif abs(primary_points) > POINTS_LIMIT:
        errors.append(f"Primary points must be between -{POINTS_LIMIT} and {POINTS_LIMIT}.")

    if input_type in DISCRETE_TYPES:
        if primary_mode != "exact":
            errors.append("This input type can only be scored on an exact match.")
        if secondary_points is not None:
            errors.append("A secondary tier isn't available for this input type.")
    else:
        pr = _to_decimal(primary_range)
        if primary_mode == "range":
            if pr is None or pr <= 0:
                errors.append("A numerical range needs a value greater than 0.")
        elif primary_mode == "exact":
            if pr is not None:
                errors.append("Exact scoring doesn't take a range value.")
            if input_type in (DECIMAL, LAP_TIME):
                warnings.append(
                    "Exact scoring on a decimal/lap-time is very hard to hit — "
                    "consider a numerical range."
                )
        else:
            errors.append("Primary scoring must be 'exact' or 'numerical range'.")

        if secondary_points is not None:
            if abs(secondary_points) > POINTS_LIMIT:
                errors.append(f"Secondary points must be between -{POINTS_LIMIT} and {POINTS_LIMIT}.")
            sr = _to_decimal(secondary_range)
            if sr is None or sr <= 0:
                errors.append("A secondary tier needs a range value greater than 0.")
            else:
                primary_eff = pr if (primary_mode == "range" and pr is not None) else Decimal(0)
                if sr <= primary_eff:
                    errors.append(
                        "The secondary range must be wider than the primary range, "
                        "otherwise it can never apply."
                    )
            if primary_points is not None and secondary_points >= primary_points:
                errors.append("Secondary points must be lower than primary points.")
        elif secondary_range is not None:
            errors.append("A secondary range was set without secondary points.")

    if input_type == DRIVER_PICK and allowed_driver_ids:
        if len(set(allowed_driver_ids)) < 2:
            errors.append("Pick at least two drivers for a restricted driver list.")
        elif not set(allowed_driver_ids).issubset(valid_driver_ids):
            errors.append("Driver list contains a driver not in this round.")
    if input_type == TEAM_PICK and allowed_team_names:
        if len(set(allowed_team_names)) < 2:
            errors.append("Pick at least two teams for a restricted team list.")
        elif not set(allowed_team_names).issubset(valid_team_names):
            errors.append("Team list contains a team not in this round.")
    if input_type == CUSTOM_CHOICE:
        opts = [o.strip() for o in (custom_options or []) if o and o.strip()]
        if len(set(opts)) < 2:
            errors.append("Custom options need at least two distinct entries.")

    return errors, warnings


# =============================================================================
# Scoring
# =============================================================================


def _numeric_pair(
    prediction: ContributionPrediction, definition: ContributionDefinition,
) -> tuple[Decimal | None, Decimal | None]:
    """Return (predicted, actual) as Decimals in the unit the range is in.

    Lap times are stored in ms but ranges are entered in seconds, so we
    convert to seconds here for a direct comparison.
    """
    it = definition.input_type
    if it == INTEGER:
        return _to_decimal(prediction.predicted_int), _to_decimal(definition.actual_int)
    if it == DECIMAL:
        return _to_decimal(prediction.predicted_decimal), _to_decimal(definition.actual_decimal)
    if it == LAP_TIME:
        p, a = prediction.predicted_lap_time_ms, definition.actual_lap_time_ms
        if p is None or a is None:
            return None, None
        return _to_decimal(p) / 1000, _to_decimal(a) / 1000
    return None, None


def score_contribution(
    prediction: ContributionPrediction | None,
    definition: ContributionDefinition,
) -> int:
    """Points for one user's wildcard answer against the entered actual.

    0 if there's no prediction, or no actual has been entered yet.
    """
    if prediction is None or not definition.has_actual:
        return 0

    it = definition.input_type
    if it == DRIVER_PICK:
        if prediction.predicted_driver_id is None or definition.actual_driver_id is None:
            return 0
        return definition.primary_points if prediction.predicted_driver_id == definition.actual_driver_id else 0
    if it == TEAM_PICK:
        if not prediction.predicted_team_name or not definition.actual_team_name:
            return 0
        return definition.primary_points if prediction.predicted_team_name == definition.actual_team_name else 0
    if it == CUSTOM_CHOICE:
        if not prediction.predicted_choice or not definition.actual_choice:
            return 0
        return definition.primary_points if prediction.predicted_choice == definition.actual_choice else 0
    if it == BOOL:
        if prediction.predicted_bool is None or definition.actual_bool is None:
            return 0
        return definition.primary_points if prediction.predicted_bool == definition.actual_bool else 0

    # Numeric: integer / decimal / lap_time.
    pred_val, actual_val = _numeric_pair(prediction, definition)
    if pred_val is None or actual_val is None:
        return 0
    delta = abs(pred_val - actual_val)

    if definition.primary_mode == "exact":
        if delta == 0:
            return definition.primary_points
    else:
        pr = _to_decimal(definition.primary_range)
        if pr is not None and delta <= pr:
            return definition.primary_points

    if definition.secondary_points is not None:
        sr = _to_decimal(definition.secondary_range)
        if sr is not None and delta <= sr:
            return definition.secondary_points
    return 0


# =============================================================================
# Blurb auto-generation
# =============================================================================


def _fmt_points(p: int) -> str:
    return f"+{p}" if p > 0 else str(p)


def _trim(d: Decimal) -> str:
    return format(d.normalize(), "f")


def _fmt_range(input_type: str, value) -> str:
    d = _to_decimal(value)
    if d is None:
        return ""
    return f"{_trim(d)}s" if input_type == LAP_TIME else _trim(d)


def generate_blurb(
    *,
    input_type: str,
    primary_points: int,
    primary_mode: str,
    primary_range,
    secondary_points,
    secondary_range,
) -> str:
    """Default scoring subtitle from the structured config. Editable by the
    contributor afterwards; the structured config stays the points source."""
    if input_type in DISCRETE_TYPES:
        return f"Exact answer scores {_fmt_points(primary_points)}."
    if primary_mode == "exact":
        base = f"Exact scores {_fmt_points(primary_points)}."
    else:
        base = f"Within {_fmt_range(input_type, primary_range)} scores {_fmt_points(primary_points)}."
    if secondary_points is not None and secondary_range is not None:
        base = base.rstrip(".") + (
            f"; within {_fmt_range(input_type, secondary_range)} scores {_fmt_points(secondary_points)}."
        )
    return base
