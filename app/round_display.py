"""Round-display helpers — produce 'actual outcome' strings from a user's pick.

The round view shows, for each prediction, what actually happened to the
driver/car the user picked. Centralised here so the template stays dumb
and we don't repeat substitution-aware lookup logic.

Two public helpers:
  - actual_position_for_pick: race / sprint race / quali / quali Nth
  - actual_places_gained_for_pick: places gained (grid → finish format)

Plus qh2h_winner_driver_id for the head-to-head row (returns a driver
id; template renders the label from drivers_by_id).

Returned text codes:
  P{n}      classified finish at position n
  PL → ...  pit lane start (places gained only)
  DNS       did not start
  DSQ       disqualified
  DNF       unclassified for any other reason
  Suffix *  car was driven by a substitute driver
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models.driver import RoundDriver
from app.models.round import Session


@dataclass(frozen=True)
class ActualDisplay:
    text: str
    substituted: bool


# Status-string detection — mirrors the scoring engine's DNS rules and
# extends them with DSQ. Anything not_classified that doesn't match
# either pattern is treated as DNF.

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


def actual_position_for_pick(
    predicted_driver_id: int,
    session: Session | None,
    round_drivers: list[RoundDriver],
) -> ActualDisplay | None:
    """Describe the outcome of the user's picked driver for a position
    prediction (race top 10, sprint top 3, quali top 3, quali Nth).

    Returns None when there's nothing to show yet (no session, no
    results, or the pick isn't in the round's lineup).
    """
    if session is None or not session.results:
        return None
    car = _car_for_predicted_driver(predicted_driver_id, round_drivers)
    if car is None:
        return None

    result = next((r for r in session.results if r.car_number == car), None)
    if result is None:
        # Car has no row in the session results — driver never appeared.
        return ActualDisplay(text="DNS", substituted=False)

    substituted = result.actual_driver_id != predicted_driver_id
    star = "*" if substituted else ""

    if result.is_classified:
        return ActualDisplay(text=f"P{result.position}{star}", substituted=substituted)

    label = _status_label_for_unclassified(result.status)
    return ActualDisplay(text=f"{label}{star}", substituted=substituted)


def actual_places_gained_for_pick(
    predicted_driver_id: int,
    race_session: Session | None,
    round_drivers: list[RoundDriver],
) -> ActualDisplay | None:
    """Describe the picked driver's grid → finish for places gained.

    Classified: "P{grid} → P{finish}", with grid 0 rendered as "PL".
    Not classified: "P{grid} → DNF" (or DNS/DSQ standalone).
    Returns None when there's nothing meaningful to show.
    """
    if race_session is None or not race_session.results:
        return None
    car = _car_for_predicted_driver(predicted_driver_id, round_drivers)
    if car is None:
        return None

    result = next((r for r in race_session.results if r.car_number == car), None)
    if result is None:
        return ActualDisplay(text="DNS", substituted=False)

    substituted = result.actual_driver_id != predicted_driver_id
    star = "*" if substituted else ""

    if _is_did_not_start(result.status):
        return ActualDisplay(text=f"DNS{star}", substituted=substituted)
    if _is_disqualified(result.status):
        return ActualDisplay(text=f"DSQ{star}", substituted=substituted)

    # Grid prefix — omit entirely if grid_position wasn't recorded
    # (defensive; shouldn't normally happen).
    if result.grid_position is None:
        grid_prefix = ""
    elif result.grid_position == 0:
        grid_prefix = "PL → "
    else:
        grid_prefix = f"P{result.grid_position} → "

    if result.is_classified:
        return ActualDisplay(
            text=f"{grid_prefix}P{result.position}{star}",
            substituted=substituted,
        )
    return ActualDisplay(text=f"{grid_prefix}DNF{star}", substituted=substituted)


def qh2h_winner_driver_id(
    rd_a: RoundDriver | None,
    rd_b: RoundDriver | None,
    quali_session: Session | None,
) -> int | None:
    """The expected_driver_id of whichever H2H car qualified higher.

    Mirrors the scoring engine's tie/DNQ rules: a car with no result
    loses against one that set a time; both missing → None.
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
        return rd_b.expected_driver_id
    if res_b is None:
        return rd_a.expected_driver_id
    if res_a.position < res_b.position:
        return rd_a.expected_driver_id
    return rd_b.expected_driver_id
