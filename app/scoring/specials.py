"""Specials: outcome computation + scoring.

This module covers the eight specials in the rotation bank. Each special
has two parts:

  1. An outcome-computation function: takes race results + pit stops,
     returns a SpecialOutcome row populated with the appropriate
     payload (driver id, integer value, bool, or team name). May
     return a `no_result=True` outcome if the special has no winner
     this round (e.g. nobody DNF'd → first-retirement has no result).

  2. A scoring function: takes the user's SpecialPrediction and a
     SpecialOutcome, returns an integer points value.

Both halves are dispatched by `special_key`. The worker calls
`compute_special_outcome` once per active special per round; the engine
calls `score_special` for each user × active special.

Tie handling:
  - First retirement / most pit stops: ties on the underlying metric
    are resolved by giving any picker of any tied driver full points.
    These specials store the tie-set as a comma-separated string in
    `actual_team_name` (overloaded — see _store_driver_set / parse).
  - Longest stint / biggest team gap: stored as the canonical winner
    only (single driver / single team). Picking any other answer is
    wrong even if it was tied.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from app.models.driver import RoundDriver
from app.models.pitstop import PitStop
from app.models.prediction import SpecialPrediction
from app.models.result import SessionResult
from app.models.round import RoundScoringConfig
from app.models.special import SpecialOutcome
from app.specials import SPECIALS_BY_KEY


# =============================================================================
# Helpers for the multi-driver tie-set overload
#
# Some specials can legitimately have multiple winners (any picker of a
# tied driver scores). We use SpecialOutcome.actual_team_name as a
# comma-separated tie-set string: e.g. "12,44" means drivers 12 and 44
# are jointly the answer. actual_driver_id holds the canonical (first) id
# for display; scoring uses the full set.
# =============================================================================


def _store_driver_set(outcome: SpecialOutcome, driver_ids: list[int]) -> None:
    """Populate `outcome` with a driver tie-set."""
    if not driver_ids:
        outcome.no_result = True
        return
    canonical = sorted(driver_ids)
    outcome.actual_driver_id = canonical[0]
    outcome.actual_team_name = ",".join(str(i) for i in canonical)


def _parse_driver_set(outcome: SpecialOutcome) -> set[int]:
    """Pull the driver tie-set back out of `actual_team_name`."""
    if not outcome.actual_team_name:
        if outcome.actual_driver_id is not None:
            return {outcome.actual_driver_id}
        return set()
    return {int(s) for s in outcome.actual_team_name.split(",") if s}


# =============================================================================
# Driver / car resolution helpers
# =============================================================================


def _driver_id_for_car(car_number: int, round_drivers: list[RoundDriver]) -> int | None:
    """The expected driver for a given car number."""
    for rd in round_drivers:
        if rd.car_number == car_number:
            return rd.expected_driver_id
    return None


def _car_for_predicted_driver(
    predicted_driver_id: int, round_drivers: list[RoundDriver],
) -> int | None:
    """Inverse of the above — used for substitution-aware lookup."""
    for rd in round_drivers:
        if rd.expected_driver_id == predicted_driver_id:
            return rd.car_number
    return None


# =============================================================================
# Outcome computation — one function per special
# =============================================================================


def _outcome_for_round(round_id: int, special_key: str) -> SpecialOutcome:
    """Construct an empty outcome row to be populated by computers.

    Explicitly sets `no_result=False` so transient (unsaved) instances
    read sensibly — the column default only applies on flush.
    """
    return SpecialOutcome(
        round_id=round_id, special_key=special_key, no_result=False,
    )


def _compute_first_retirement(
    round_id: int,
    race_results: list[SessionResult],
    round_drivers: list[RoundDriver],
) -> SpecialOutcome:
    """Driver(s) who retired earliest.

    "Retired earliest" = unclassified row with the lowest laps_completed.
    Ties on lap count: all tied drivers form the answer set.
    """
    outcome = _outcome_for_round(round_id, "first_retirement")
    retirees = [
        r for r in race_results
        if not r.is_classified and r.laps_completed is not None
    ]
    if not retirees:
        outcome.no_result = True
        return outcome
    min_laps = min(r.laps_completed for r in retirees)
    tied_cars = [r.car_number for r in retirees if r.laps_completed == min_laps]
    driver_ids = [
        d for d in (_driver_id_for_car(c, round_drivers) for c in tied_cars)
        if d is not None
    ]
    _store_driver_set(outcome, driver_ids)
    return outcome


def _compute_most_pitstops(
    round_id: int,
    pit_stops: list[PitStop],
    round_drivers: list[RoundDriver],
) -> SpecialOutcome:
    """Driver(s) with the highest pit-stop count. Ties form the answer set.

    actual_int holds the count. actual_driver_id + actual_team_name
    overload-store the winner tie-set.
    """
    outcome = _outcome_for_round(round_id, "most_pitstops")
    if not pit_stops:
        outcome.no_result = True
        return outcome
    counts: dict[int, int] = defaultdict(int)
    for stop in pit_stops:
        counts[stop.driver_id] += 1
    max_count = max(counts.values())
    tied = [d for d, c in counts.items() if c == max_count]
    # Validate against the round lineup — exclude any driver_id not in
    # this round's regulars (extremely unlikely but defensive).
    valid_regulars = {rd.expected_driver_id for rd in round_drivers}
    tied = [d for d in tied if d in valid_regulars] or tied
    outcome.actual_int = max_count
    _store_driver_set(outcome, tied)
    return outcome


def _compute_last_classified(
    round_id: int,
    race_results: list[SessionResult],
    round_drivers: list[RoundDriver],
) -> SpecialOutcome:
    """Driver with the lowest position whose status is classified."""
    outcome = _outcome_for_round(round_id, "last_classified")
    classified = [r for r in race_results if r.is_classified]
    if not classified:
        outcome.no_result = True
        return outcome
    last = max(classified, key=lambda r: r.position)
    driver_id = _driver_id_for_car(last.car_number, round_drivers)
    if driver_id is None:
        outcome.no_result = True
        return outcome
    outcome.actual_driver_id = driver_id
    return outcome


def _compute_margin_of_victory(
    round_id: int,
    race_results: list[SessionResult],
) -> SpecialOutcome:
    """Gap between P1 and P2 in whole seconds.

    If P2 is not classified, or finished on a different lead lap, the
    margin can't be expressed in seconds — record no_result.
    Specifically: if P2's race_time_ms is None (which is the case for
    P2 down-a-lap from P1), no margin.
    """
    outcome = _outcome_for_round(round_id, "margin_of_victory")
    p1 = next((r for r in race_results if r.position == 1), None)
    p2 = next((r for r in race_results if r.position == 2), None)
    if (
        p1 is None or p2 is None
        or p1.race_time_ms is None or p2.race_time_ms is None
    ):
        outcome.no_result = True
        return outcome
    gap_ms = p2.race_time_ms - p1.race_time_ms
    if gap_ms < 0:
        outcome.no_result = True
        return outcome
    outcome.actual_int = round(gap_ms / 1000)
    return outcome


def _compute_lap_of_first_pitstop(
    round_id: int,
    pit_stops: list[PitStop],
) -> SpecialOutcome:
    """Earliest lap on which any driver pitted."""
    outcome = _outcome_for_round(round_id, "lap_of_first_pitstop")
    if not pit_stops:
        outcome.no_result = True
        return outcome
    outcome.actual_int = min(s.lap for s in pit_stops)
    return outcome


def _compute_pole_sitter_wins(
    round_id: int,
    race_results: list[SessionResult],
) -> SpecialOutcome:
    """True iff the pole sitter (grid 1) won the race (finished P1).

    Grid 0 (pit lane start) is excluded — that's never "the pole sitter".
    """
    outcome = _outcome_for_round(round_id, "pole_sitter_wins")
    p1 = next((r for r in race_results if r.position == 1), None)
    if p1 is None or p1.grid_position is None:
        outcome.no_result = True
        return outcome
    outcome.actual_bool = (p1.grid_position == 1)
    return outcome


def _compute_longest_stint(
    round_id: int,
    pit_stops: list[PitStop],
    race_results: list[SessionResult],
    round_drivers: list[RoundDriver],
) -> SpecialOutcome:
    """The driver whose longest single stint covered the most laps.

    A "stint" runs from race start (or last pit stop) to the next pit
    stop (or the driver's final lap). Include DNFs — their stints up to
    retirement still count. Ties on stint length: pick the canonically-
    lowest driver_id (single-winner; ties don't share for this special).
    """
    outcome = _outcome_for_round(round_id, "longest_stint")
    if not race_results:
        outcome.no_result = True
        return outcome

    # Group pit-stop laps per driver_id, sorted by stop_number.
    stops_by_driver: dict[int, list[int]] = defaultdict(list)
    for s in sorted(pit_stops, key=lambda x: (x.driver_id, x.stop_number)):
        stops_by_driver[s.driver_id].append(s.lap)

    longest_per_driver: dict[int, int] = {}
    for result in race_results:
        if result.laps_completed is None:
            continue
        driver_id = _driver_id_for_car(result.car_number, round_drivers)
        if driver_id is None:
            continue
        stop_laps = stops_by_driver.get(driver_id, [])
        # Stint boundaries: race start (lap 0 → in pit on lap N1 → ...
        # → in pit on lap Nk → final lap). Stint k runs from lap
        # boundary[k] to lap boundary[k+1].
        boundaries = [0] + sorted(stop_laps) + [result.laps_completed]
        if len(boundaries) < 2:
            continue
        max_stint = max(
            boundaries[i + 1] - boundaries[i]
            for i in range(len(boundaries) - 1)
        )
        if max_stint > 0:
            longest_per_driver[driver_id] = max_stint

    if not longest_per_driver:
        outcome.no_result = True
        return outcome

    max_stint = max(longest_per_driver.values())
    tied = sorted(d for d, s in longest_per_driver.items() if s == max_stint)
    outcome.actual_int = max_stint
    outcome.actual_driver_id = tied[0]   # canonical winner; no tie share
    return outcome


def _compute_biggest_team_gap(
    round_id: int,
    race_results: list[SessionResult],
    round_drivers: list[RoundDriver],
) -> SpecialOutcome:
    """Team whose two drivers had the biggest finishing-position gap.

    Only counts teams where BOTH drivers finished (classified). Teams
    with a DNF are excluded entirely. Ties: pick alphabetically-first
    team name (single winner).
    """
    outcome = _outcome_for_round(round_id, "biggest_team_gap")
    # Build team → list of classified finishing positions.
    car_to_team = {rd.car_number: rd.constructor_name for rd in round_drivers}
    team_positions: dict[str, list[int]] = defaultdict(list)
    for r in race_results:
        if not r.is_classified:
            continue
        team = car_to_team.get(r.car_number)
        if team is None:
            continue
        team_positions[team].append(r.position)
    eligible = {
        team: positions for team, positions in team_positions.items()
        if len(positions) == 2
    }
    if not eligible:
        outcome.no_result = True
        return outcome
    gaps = {team: abs(p[0] - p[1]) for team, p in eligible.items()}
    max_gap = max(gaps.values())
    winners = sorted(team for team, g in gaps.items() if g == max_gap)
    outcome.actual_team_name = winners[0]
    outcome.actual_int = max_gap
    return outcome


# =============================================================================
# Dispatch
# =============================================================================


def compute_special_outcome(
    round_id: int,
    special_key: str,
    race_results: list[SessionResult],
    pit_stops: list[PitStop],
    round_drivers: list[RoundDriver],
) -> SpecialOutcome:
    """Compute the actual outcome for a single special.

    Returns a SpecialOutcome (transient — caller persists it).
    """
    if special_key == "first_retirement":
        return _compute_first_retirement(round_id, race_results, round_drivers)
    if special_key == "most_pitstops":
        return _compute_most_pitstops(round_id, pit_stops, round_drivers)
    if special_key == "last_classified":
        return _compute_last_classified(round_id, race_results, round_drivers)
    if special_key == "margin_of_victory":
        return _compute_margin_of_victory(round_id, race_results)
    if special_key == "lap_of_first_pitstop":
        return _compute_lap_of_first_pitstop(round_id, pit_stops)
    if special_key == "pole_sitter_wins":
        return _compute_pole_sitter_wins(round_id, race_results)
    if special_key == "longest_stint":
        return _compute_longest_stint(
            round_id, pit_stops, race_results, round_drivers,
        )
    if special_key == "biggest_team_gap":
        return _compute_biggest_team_gap(round_id, race_results, round_drivers)
    raise ValueError(f"Unknown special_key: {special_key}")


# =============================================================================
# Scoring — one function dispatches to inner logic per input_type
# =============================================================================


def score_special(
    prediction: SpecialPrediction | None,
    outcome: SpecialOutcome | None,
    config: RoundScoringConfig,
) -> int:
    """Score a single user's prediction for one special.

    Returns 0 if either the prediction or the outcome is missing, or if
    the outcome's no_result is True.
    """
    if prediction is None or outcome is None or outcome.no_result:
        return 0
    special = SPECIALS_BY_KEY.get(outcome.special_key)
    if special is None:
        return 0
    points = getattr(config, special.scoring_config_attr, 0) or 0

    if special.input_type == "driver_pick":
        return _score_driver_pick(prediction, outcome, points)
    if special.input_type == "int":
        return _score_int(prediction, outcome, points, special.scoring_tolerance)
    if special.input_type == "bool":
        return _score_bool(prediction, outcome, points)
    if special.input_type == "team_pick":
        return _score_team_pick(prediction, outcome, points)
    return 0


def _score_driver_pick(
    prediction: SpecialPrediction, outcome: SpecialOutcome, points: int,
) -> int:
    if prediction.predicted_driver_id is None:
        return 0
    winners = _parse_driver_set(outcome)
    return points if prediction.predicted_driver_id in winners else 0


def _score_int(
    prediction: SpecialPrediction,
    outcome: SpecialOutcome,
    points: int,
    tolerance: int | None,
) -> int:
    if prediction.predicted_int is None or outcome.actual_int is None:
        return 0
    delta = abs(prediction.predicted_int - outcome.actual_int)
    if tolerance is None:
        return points if delta == 0 else 0
    return points if delta <= tolerance else 0


def _score_bool(
    prediction: SpecialPrediction, outcome: SpecialOutcome, points: int,
) -> int:
    if prediction.predicted_bool is None or outcome.actual_bool is None:
        return 0
    return points if prediction.predicted_bool == outcome.actual_bool else 0


def _score_team_pick(
    prediction: SpecialPrediction, outcome: SpecialOutcome, points: int,
) -> int:
    if not prediction.predicted_team_name or not outcome.actual_team_name:
        return 0
    return points if prediction.predicted_team_name == outcome.actual_team_name else 0
