"""Seat resolution — picks follow the seat, matched to results by team.

Driver ids: VER=1 HAD=6 LAW=30 LIN=41 BEA=87 OCO=31; stand-ins TSU=22,
NEW=99. Car numbers are the drivers' own, as in real F1.
"""
from __future__ import annotations

from app.config import Config
from app.models.driver import RoundDriver
from app.models.pitstop import PitStop
from app.models.result import SessionResult
from app.models.round import RoundScoringConfig
from app.scoring.engine import (
    score_fastest_lap,
    score_places_gained,
    score_quali_h2h,
    score_top10_slot,
)
from app.scoring.specials import compute_special_outcome
from app.seats import resolve_seats, result_for_pick, substitutions

RBR, RB, HAAS = "Red Bull", "RB F1 Team", "Haas F1 Team"


def seat(driver_id, team, car=None):
    return RoundDriver(round_id=1, car_number=car or driver_id,
                       expected_driver_id=driver_id, constructor_name=team)


def res(pos, driver_id, team, car=None, **kw):
    kw.setdefault("status", "Finished")
    kw.setdefault("is_classified", True)
    kw.setdefault("is_fastest_lap", False)
    return SessionResult(session_id=1, position=pos, car_number=car or driver_id,
                         actual_driver_id=driver_id, constructor_name=team, **kw)


def cfg():
    return RoundScoringConfig.from_defaults(round_id=1, defaults=Config.SCORING_DEFAULTS)


def lineup():
    return [seat(1, RBR), seat(6, RBR), seat(30, RB), seat(41, RB),
            seat(87, HAAS), seat(31, HAAS)]


class TestResolve:
    def test_regulars_take_their_own_rows(self):
        seats = lineup()
        rows = [res(i + 1, s.expected_driver_id, s.constructor_name) for i, s in enumerate(seats)]
        resolved = resolve_seats(seats, rows)
        assert all(resolved[s].actual_driver_id == s.expected_driver_id for s in seats)
        assert substitutions(seats, rows) == []

    def test_reserve_with_own_number_fills_ill_drivers_seat(self):
        seats = lineup()
        rows = [res(1, 1, RBR), res(2, 6, RBR), res(3, 30, RB), res(4, 41, RB),
                res(5, 87, HAAS), res(6, 99, HAAS)]          # NEW in #99 for OCO
        assert result_for_pick(31, seats, rows).actual_driver_id == 99

    def test_promotion_and_reserve_resolve_within_teams(self):
        # HAD out; LAW promoted RB → Red Bull; TSU takes LAW's RB seat.
        seats = lineup()
        rows = [res(1, 1, RBR), res(2, 30, RBR), res(3, 22, RB), res(4, 41, RB),
                res(5, 87, HAAS), res(6, 31, HAAS)]
        assert result_for_pick(6, seats, rows).actual_driver_id == 30    # HAD seat → LAW
        assert result_for_pick(30, seats, rows).actual_driver_id == 22   # LAW seat → TSU
        assert result_for_pick(41, seats, rows).actual_driver_id == 41
        subs = {(s.expected_driver_id, r.actual_driver_id) for s, r in substitutions(seats, rows)}
        assert subs == {(6, 30), (30, 22)}

    def test_seat_without_result_is_absent(self):
        seats = lineup()
        rows = [res(1, 1, RBR)]
        assert result_for_pick(6, seats, rows) is None
        assert result_for_pick(999, seats, rows) is None   # not in lineup

    def test_rows_without_team_fall_back_to_car_number(self):
        seats = [seat(44, "Ferrari", car=44)]
        rows = [res(5, 77, None, car=44)]
        assert result_for_pick(44, seats, rows).actual_driver_id == 77

    def test_rows_without_team_never_match_by_driver(self):
        # Legacy rows behave exactly as before: car number only.
        seats = [seat(30, RB, car=30), seat(6, RBR, car=6)]
        rows = [res(2, 30, None, car=30)]
        assert result_for_pick(30, seats, rows).actual_driver_id == 30
        assert result_for_pick(6, seats, rows) is None

    def test_team_name_compare_ignores_case_and_space(self):
        seats = [seat(31, "Haas F1 Team")]
        rows = [res(6, 99, " haas f1 team ")]
        assert result_for_pick(31, seats, rows).actual_driver_id == 99


class TestEngine:
    def test_top10_scores_the_stand_in(self):
        seats = lineup()
        rows = [res(1, 1, RBR), res(3, 30, RBR), res(2, 22, RB)]
        assert score_top10_slot(3, 6, rows, seats, cfg()) == cfg().race_top10_correct   # HAD seat = LAW P3
        assert score_top10_slot(2, 30, rows, seats, cfg()) == cfg().race_top10_correct  # LAW seat = TSU P2

    def test_fastest_lap_follows_seat(self):
        seats = lineup()
        rows = [res(1, 1, RBR), res(6, 99, HAAS, is_fastest_lap=True)]
        assert score_fastest_lap(31, rows, seats, cfg()) == cfg().fastest_lap_correct
        assert score_fastest_lap(87, rows, seats, cfg()) == 0

    def test_places_gained_follows_seat(self):
        seats = lineup()
        rows = [res(4, 99, HAAS, grid_position=10), res(5, 87, HAAS, grid_position=5)]
        assert score_places_gained(31, rows, seats) == 6

    def test_h2h_with_stand_in(self):
        a, b = seat(1, RBR), seat(6, RBR)
        rows = [res(4, 1, RBR), res(2, 30, RBR)]   # LAW in HAD's seat outqualifies VER
        assert score_quali_h2h(6, a, b, rows, cfg()) == cfg().qh2h_correct
        assert score_quali_h2h(1, a, b, rows, cfg()) == 0


class TestSpecials:
    def test_first_retirement_credits_the_seat(self):
        seats = lineup()
        rows = [res(1, 1, RBR, laps_completed=57),
                res(20, 99, HAAS, is_classified=False, status="Engine", laps_completed=3)]
        out = compute_special_outcome(1, "first_retirement", rows, [], seats)
        assert out.actual_driver_id == 31

    def test_biggest_team_gap_counts_stand_in(self):
        seats = lineup()
        rows = [res(1, 87, HAAS), res(18, 99, HAAS), res(2, 1, RBR), res(3, 6, RBR)]
        out = compute_special_outcome(1, "biggest_team_gap", rows, [], seats)
        assert (out.actual_team_name, out.actual_int) == (HAAS, 17)

    def test_most_pitstops_counts_stops_against_seat(self):
        seats = lineup()
        rows = [res(1, 87, HAAS), res(2, 99, HAAS)]
        stops = [PitStop(round_id=1, driver_id=99, lap=l, stop_number=n)
                 for n, l in enumerate((10, 20, 30), 1)]
        stops.append(PitStop(round_id=1, driver_id=87, lap=15, stop_number=1))
        out = compute_special_outcome(1, "most_pitstops", rows, stops, seats)
        assert (out.actual_driver_id, out.actual_int) == (31, 3)

    def test_longest_stint_reads_stops_of_who_drove(self):
        seats = [seat(87, HAAS), seat(31, HAAS)]
        rows = [res(1, 87, HAAS, laps_completed=50), res(2, 99, HAAS, laps_completed=50)]
        stops = [PitStop(round_id=1, driver_id=87, lap=25, stop_number=1),
                 PitStop(round_id=1, driver_id=99, lap=5, stop_number=1)]
        out = compute_special_outcome(1, "longest_stint", rows, stops, seats)
        assert (out.actual_driver_id, out.actual_int) == (31, 45)
