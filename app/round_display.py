"""Round-display helpers — produce 'actual outcome' strings from a user's pick.

The round view shows, for each prediction, what actually happened to the
driver/car/value the user picked. Centralised here so the template stays
dumb and we don't repeat substitution-aware lookup logic.

Public helpers (all return ActualDisplay | None):
  - actual_position_for_pick: race / sprint top N / quali top N /
    quali Nth / quali random driver
  - actual_places_gained_for_pick: places gained (grid → finish format)
  - actual_for_h2h: quali head-to-head winner
  - actual_for_fastest_lap
  - actual_for_dnf_count
  - actual_for_special: all special types

ActualDisplay.is_exact fires when the user's pick matched exactly (not
within a tolerance bucket). Drives the tick pill in the Result column.
ActualDisplay.substituted is a separate flag; the template renders a
trailing * either way.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models.driver import Driver, RoundDriver
from app.models.prediction import SpecialPrediction
from app.models.round import Session
from app.models.special import SpecialOutcome
from app.specials import SpecialDef


@dataclass(frozen=True)
class ActualDisplay:
    text: str
    is_exact: bool = False
    substituted: bool = False
    # Additional lines for specials with tie-set winners. Rendered on
    # their own <br>-separated lines by the template.
    extra_lines: tuple[str, ...] = ()


# Status-string detection — mirrors the engine's DNS rules and extends
# with DSQ. Anything not_classified that doesn't match either pattern
# is treated as DNF.
_DNS_PATTERNS = ("did not start", "withdrew", "withdrawn", "dns")
_DSQ_PATTERNS = ("disqualified", "excluded")


def _is_did_not_start(status: str | None) -> bool:
    s = (status or "").strip().lower()
    return any(p in s for p in _DNS_PATTERNS)


def _is_disqualified(status: str | None) -> bool:
    s = (status or "").strip().lower()
    return any(p in s for p in _DSQ_PATTERNS)


def _status_label_for_unclassified(status: str | None) -> str:
    if _is_did_not_start(status):
        return "DNS"
    if _is_disqualified(status):
        return "DSQ"
    return "DNF"


def _car_for_predicted_driver(
    predicted_driver_id: int, round_drivers: list[RoundDriver],
) -> int | None:
    for rd in round_drivers:
        if rd.expected_driver_id == predicted_driver_id:
            return rd.car_number
    return None


def _driver_label(d: Driver | None) -> str:
    if d is None:
        return "—"
    code = d.code or d.driver_ref[:3].upper()
    return f"{code} · {d.family_name}"


def actual_position_for_pick(
    predicted_driver_id: int,
    predicted_position: int,
    session: Session | None,
    round_drivers: list[RoundDriver],
) -> ActualDisplay | None:
    """Outcome of the user's picked driver for a position prediction.

    `predicted_position` is the slot/N the pick was staked on; used to
    compute is_exact (delta = 0 AND classified). Returns None when
    there's nothing to show (no session, no results, pick not in the
    round's lineup).
    """
    if session is None or not session.results:
        return None
    car = _car_for_predicted_driver(predicted_driver_id, round_drivers)
    if car is None:
        return None

    result = next((r for r in session.results if r.car_number == car), None)
    if result is None:
        return ActualDisplay(text="DNS")

    substituted = result.actual_driver_id != predicted_driver_id

    if result.is_classified:
        is_exact = result.position == predicted_position
        return ActualDisplay(
            text=f"P{result.position}",
            substituted=substituted,
            is_exact=is_exact,
        )
    return ActualDisplay(
        text=_status_label_for_unclassified(result.status),
        substituted=substituted,
    )


def actual_places_gained_for_pick(
    predicted_driver_id: int,
    race_session: Session | None,
    round_drivers: list[RoundDriver],
) -> ActualDisplay | None:
    """Picked driver's grid → finish for places gained.

    Never sets is_exact — places gained is continuous.
    """
    if race_session is None or not race_session.results:
        return None
    car = _car_for_predicted_driver(predicted_driver_id, round_drivers)
    if car is None:
        return None

    result = next((r for r in race_session.results if r.car_number == car), None)
    if result is None:
        return ActualDisplay(text="DNS")

    substituted = result.actual_driver_id != predicted_driver_id

    if _is_did_not_start(result.status):
        return ActualDisplay(text="DNS", substituted=substituted)
    if _is_disqualified(result.status):
        return ActualDisplay(text="DSQ", substituted=substituted)

    if result.grid_position is None:
        grid_prefix = ""
    elif result.grid_position == 0:
        grid_prefix = "PL → "
    else:
        grid_prefix = f"P{result.grid_position} → "

    if result.is_classified:
        return ActualDisplay(
            text=f"{grid_prefix}P{result.position}",
            substituted=substituted,
        )
    return ActualDisplay(text=f"{grid_prefix}DNF", substituted=substituted)


def actual_for_h2h(
    predicted_driver_id: int | None,
    rd_a: RoundDriver | None,
    rd_b: RoundDriver | None,
    quali_session: Session | None,
    drivers_by_id: dict[int, Driver],
) -> ActualDisplay | None:
    """H2H winner from the user's perspective.

    text = winning seat's expected driver label.
    is_exact = user picked the winning seat.
    substituted = winning car was driven by a substitute (regardless of
    whether the user picked correctly — informative either way).
    """
    if (
        rd_a is None or rd_b is None
        or quali_session is None or not quali_session.results
    ):
        return None
    res_a = next(
        (r for r in quali_session.results if r.car_number == rd_a.car_number),
        None,
    )
    res_b = next(
        (r for r in quali_session.results if r.car_number == rd_b.car_number),
        None,
    )
    if res_a is None and res_b is None:
        return None

    if res_a is None:
        winner_rd, winner_res = rd_b, res_b
    elif res_b is None:
        winner_rd, winner_res = rd_a, res_a
    elif res_a.position < res_b.position:
        winner_rd, winner_res = rd_a, res_a
    else:
        winner_rd, winner_res = rd_b, res_b

    text = _driver_label(drivers_by_id.get(winner_rd.expected_driver_id))
    is_exact = (
        predicted_driver_id is not None
        and predicted_driver_id == winner_rd.expected_driver_id
    )
    substituted = (
        winner_res is not None
        and winner_res.actual_driver_id != winner_rd.expected_driver_id
    )
    return ActualDisplay(text=text, is_exact=is_exact, substituted=substituted)


def actual_for_fastest_lap(
    predicted_driver_id: int | None,
    race_session: Session | None,
    round_drivers: list[RoundDriver],
    drivers_by_id: dict[int, Driver],
) -> ActualDisplay | None:
    """Actual fastest-lap setter.

    text = the human who set the lap (which may be a substitute).
    is_exact = the user's pick was expected for the car that set FL.
    substituted = is_exact AND the car had a sub driving (so the user
    won despite a sub being at the wheel).
    """
    if race_session is None or not race_session.results:
        return None
    flap_row = next(
        (r for r in race_session.results if r.is_fastest_lap),
        None,
    )
    if flap_row is None:
        return None
    text = _driver_label(drivers_by_id.get(flap_row.actual_driver_id))

    is_exact = False
    substituted = False
    if predicted_driver_id is not None:
        car = _car_for_predicted_driver(predicted_driver_id, round_drivers)
        if car is not None and car == flap_row.car_number:
            is_exact = True
            substituted = flap_row.actual_driver_id != predicted_driver_id
    return ActualDisplay(text=text, is_exact=is_exact, substituted=substituted)


def actual_for_dnf_count(
    predicted_count: int | None,
    actual_count: int | None,
) -> ActualDisplay | None:
    if actual_count is None:
        return None
    is_exact = predicted_count is not None and predicted_count == actual_count
    return ActualDisplay(text=str(actual_count), is_exact=is_exact)


def actual_for_special(
    sp: SpecialDef,
    prediction: SpecialPrediction | None,
    outcome: SpecialOutcome | None,
    drivers_by_id: dict[int, Driver],
) -> ActualDisplay | None:
    """Outcome for a single special.

    Returns None if no outcome row exists. For outcomes flagged
    no_result, returns text="no result", is_exact=False. Driver-pick
    specials may have a tie-set; the first winner's label sits in
    text, remaining winners in extra_lines. is_exact fires when the
    user picked any tied winner. substitution flag is always False
    for specials.
    """
    if outcome is None:
        return None
    if outcome.no_result:
        return ActualDisplay(text="no result")
    if sp.input_type == "driver_pick":
        return _special_driver_pick(prediction, outcome, drivers_by_id)
    if sp.input_type == "int":
        return _special_int(prediction, outcome)
    if sp.input_type == "bool":
        return _special_bool(prediction, outcome)
    if sp.input_type == "team_pick":
        return _special_team_pick(prediction, outcome)
    return None


def _special_driver_pick(
    prediction: SpecialPrediction | None,
    outcome: SpecialOutcome,
    drivers_by_id: dict[int, Driver],
) -> ActualDisplay:
    if outcome.actual_team_name:
        winner_ids = [int(s) for s in outcome.actual_team_name.split(",") if s]
    elif outcome.actual_driver_id is not None:
        winner_ids = [outcome.actual_driver_id]
    else:
        winner_ids = []
    labels = [_driver_label(drivers_by_id.get(wid)) for wid in winner_ids]
    text = labels[0] if labels else ""
    extra = tuple(labels[1:])
    is_exact = (
        prediction is not None
        and prediction.predicted_driver_id is not None
        and prediction.predicted_driver_id in winner_ids
    )
    return ActualDisplay(text=text, extra_lines=extra, is_exact=is_exact)


def _special_int(
    prediction: SpecialPrediction | None,
    outcome: SpecialOutcome,
) -> ActualDisplay:
    text = str(outcome.actual_int) if outcome.actual_int is not None else ""
    is_exact = (
        prediction is not None
        and prediction.predicted_int is not None
        and prediction.predicted_int == outcome.actual_int
    )
    return ActualDisplay(text=text, is_exact=is_exact)


def _special_bool(
    prediction: SpecialPrediction | None,
    outcome: SpecialOutcome,
) -> ActualDisplay:
    text = "Yes" if outcome.actual_bool else "No"
    is_exact = (
        prediction is not None
        and prediction.predicted_bool is not None
        and prediction.predicted_bool == outcome.actual_bool
    )
    return ActualDisplay(text=text, is_exact=is_exact)


def _special_team_pick(
    prediction: SpecialPrediction | None,
    outcome: SpecialOutcome,
) -> ActualDisplay:
    text = outcome.actual_team_name or ""
    is_exact = (
        prediction is not None
        and bool(prediction.predicted_team_name)
        and prediction.predicted_team_name == outcome.actual_team_name
    )
    return ActualDisplay(text=text, is_exact=is_exact)
