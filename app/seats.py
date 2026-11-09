"""Seat resolution — which session result belongs to which seat.

A pick is a bet on a seat: a `RoundDriver` row, labelled with the driver
expected to drive it. F1 car numbers belong to drivers, not cars (a reserve
races under his own number), so seats are matched to results by team:

  1. A seat's expected driver who drove for the seat's team takes their
     own result.
  2. Within each team, results left unclaimed fill that team's unclaimed
     seats, in car-number order. Normally one stand-in, one empty seat.
  3. Fallback for result rows without team data (ingested before
     `SessionResult.constructor_name` existed): match by car number,
     exactly the old behaviour. Steps 1 and 2 only consider rows that
     carry a team, so legacy data scores as it always did.

Matching within a team, never across the grid, is what makes a reshuffle
work: if the RB driver is promoted to Red Bull and a reserve takes his RB
seat, the RB seat scores the reserve and the vacant Red Bull seat scores
the promoted driver.

Pure functions over model instances; nothing here touches the session.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from app.models.driver import RoundDriver
from app.models.result import SessionResult


def _team(name: str | None) -> str | None:
    return (name or "").strip().casefold() or None


def _compatible(a: str | None, b: str | None) -> bool:
    return a is None or b is None or a == b


def resolve_seats(
    round_drivers: Iterable[RoundDriver],
    results: Iterable[SessionResult],
) -> dict[RoundDriver, SessionResult]:
    """Map each seat to the result row of whoever drove it.

    Seats with no matching result (car didn't take part) are absent.
    """
    seats = list(round_drivers)
    rows = list(results)
    out: dict[RoundDriver, SessionResult] = {}
    claimed: set[int] = set()

    def claim(seat: RoundDriver, row: SessionResult) -> None:
        out[seat] = row
        claimed.add(id(row))

    # 1. Expected driver drove for this seat's team.
    by_driver: dict[int, list[SessionResult]] = defaultdict(list)
    for r in rows:
        if _team(r.constructor_name) is not None:
            by_driver[r.actual_driver_id].append(r)
    for seat in seats:
        st = _team(seat.constructor_name)
        for r in by_driver.get(seat.expected_driver_id, ()):
            if id(r) not in claimed and _compatible(st, _team(r.constructor_name)):
                claim(seat, r)
                break

    # 2. Stand-ins fill vacant seats within the same team.
    open_by_team: dict[str, list[RoundDriver]] = defaultdict(list)
    for seat in seats:
        t = _team(seat.constructor_name)
        if seat not in out and t is not None:
            open_by_team[t].append(seat)
    free_by_team: dict[str, list[SessionResult]] = defaultdict(list)
    for r in rows:
        t = _team(r.constructor_name)
        if id(r) not in claimed and t is not None:
            free_by_team[t].append(r)
    for t, open_seats in open_by_team.items():
        candidates = sorted(free_by_team.get(t, ()), key=lambda r: r.car_number)
        for seat, r in zip(sorted(open_seats, key=lambda s: s.car_number), candidates):
            claim(seat, r)

    # 3. Car number, where team data can't contradict it.
    by_car = {r.car_number: r for r in rows}
    for seat in seats:
        if seat in out:
            continue
        r = by_car.get(seat.car_number)
        if (
            r is not None and id(r) not in claimed
            and _compatible(_team(seat.constructor_name), _team(r.constructor_name))
        ):
            claim(seat, r)

    return out


def seat_by_result(
    round_drivers: Iterable[RoundDriver],
    results: Iterable[SessionResult],
) -> dict[SessionResult, RoundDriver]:
    """Inverse of `resolve_seats`: result row → the seat it scores for."""
    return {r: seat for seat, r in resolve_seats(round_drivers, results).items()}


def seat_for_driver(
    driver_id: int, round_drivers: Iterable[RoundDriver],
) -> RoundDriver | None:
    """The seat a pick refers to (the seat the driver is listed in)."""
    for seat in round_drivers:
        if seat.expected_driver_id == driver_id:
            return seat
    return None


def result_for_pick(
    driver_id: int,
    round_drivers: Iterable[RoundDriver],
    results: Iterable[SessionResult],
) -> SessionResult | None:
    """The result row a pick scores against, or None if the seat didn't
    take part / the driver isn't in this round's lineup."""
    seats = list(round_drivers)
    seat = seat_for_driver(driver_id, seats)
    if seat is None:
        return None
    return resolve_seats(seats, results).get(seat)


def substitutions(
    round_drivers: Iterable[RoundDriver],
    results: Iterable[SessionResult],
) -> list[tuple[RoundDriver, SessionResult]]:
    """Seats driven by someone other than the listed driver."""
    return [
        (seat, r) for seat, r in resolve_seats(round_drivers, results).items()
        if r.actual_driver_id != seat.expected_driver_id
    ]
