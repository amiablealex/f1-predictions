"""Tests for the specials outcome + scoring module."""
from __future__ import annotations

import pytest

from app.config import Config
from app.models.driver import RoundDriver
from app.models.pitstop import PitStop
from app.models.prediction import SpecialPrediction
from app.models.result import SessionResult
from app.models.round import RoundScoringConfig
from app.models.special import SpecialOutcome
from app.scoring.specials import (
    compute_special_outcome,
    score_special,
)


# =============================================================================
# Helpers
# =============================================================================


def make_config(**overrides) -> RoundScoringConfig:
    cfg = RoundScoringConfig.from_defaults(round_id=1, defaults=Config.SCORING_DEFAULTS)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def make_round_drivers(triples: list[tuple[int, int, str | None]]) -> list[RoundDriver]:
    """`triples` is [(expected_driver_id, car_number, team_name)]."""
    return [
        RoundDriver(round_id=1, car_number=car, expected_driver_id=drv,
                    constructor_name=team)
        for drv, car, team in triples
    ]


def make_result(position, car_number, **kwargs) -> SessionResult:
    defaults = dict(
        session_id=1,
        actual_driver_id=kwargs.pop("actual_driver_id", car_number),
        status="Finished",
        is_classified=True,
        is_fastest_lap=False,
        best_qualifying_time_ms=None,
        grid_position=None,
        laps_completed=None,
        race_time_ms=None,
    )
    defaults.update(kwargs)
    return SessionResult(position=position, car_number=car_number, **defaults)


def make_pit_stop(driver_id, lap, stop_number, duration_ms=23_000) -> PitStop:
    return PitStop(
        round_id=1, driver_id=driver_id,
        lap=lap, stop_number=stop_number, duration_ms=duration_ms,
    )


# =============================================================================
# first_retirement
# =============================================================================


class TestFirstRetirement:
    def test_single_retiree(self):
        rds = make_round_drivers([(1, 44, "Mercedes"), (2, 1, "Red Bull")])
        results = [
            make_result(1, 1, laps_completed=57),
            make_result(20, 44, is_classified=False, status="Engine", laps_completed=12),
        ]
        outcome = compute_special_outcome(1, "first_retirement", results, [], rds)
        assert outcome.no_result is False
        assert outcome.actual_driver_id == 1   # driver_id of car 44

    def test_no_dnfs_yields_no_result(self):
        rds = make_round_drivers([(1, 44, "Mercedes")])
        results = [make_result(1, 44, laps_completed=57)]
        outcome = compute_special_outcome(1, "first_retirement", results, [], rds)
        assert outcome.no_result is True

    def test_tie_set_stored(self):
        rds = make_round_drivers([
            (1, 44, "M"), (2, 1, "R"), (3, 16, "F"),
        ])
        results = [
            make_result(1, 11, laps_completed=57),
            make_result(19, 44, is_classified=False, status="Engine", laps_completed=12),
            make_result(20, 1, is_classified=False, status="Collision", laps_completed=12),
            make_result(18, 16, is_classified=False, status="Hydraulics", laps_completed=30),
        ]
        outcome = compute_special_outcome(1, "first_retirement", results, [], rds)
        assert outcome.no_result is False
        # Tied drivers are 1 and 2 (cars 44 and 1).
        assert set(outcome.actual_team_name.split(",")) == {"1", "2"}

    def test_scoring_picker_of_either_tied_driver_wins(self):
        cfg = make_config()
        outcome = SpecialOutcome(
            round_id=1, special_key="first_retirement",
            actual_driver_id=1, actual_team_name="1,2", no_result=False,
        )
        pred1 = SpecialPrediction(
            user_id=1, round_id=1, special_key="first_retirement",
            predicted_driver_id=1,
        )
        pred2 = SpecialPrediction(
            user_id=1, round_id=1, special_key="first_retirement",
            predicted_driver_id=2,
        )
        pred_wrong = SpecialPrediction(
            user_id=1, round_id=1, special_key="first_retirement",
            predicted_driver_id=3,
        )
        assert score_special(pred1, outcome, cfg) == 10
        assert score_special(pred2, outcome, cfg) == 10
        assert score_special(pred_wrong, outcome, cfg) == 0


# =============================================================================
# most_pitstops
# =============================================================================


class TestMostPitstops:
    def test_clear_winner(self):
        rds = make_round_drivers([(1, 44, "M"), (2, 1, "R")])
        stops = [
            make_pit_stop(1, 12, 1),
            make_pit_stop(1, 30, 2),
            make_pit_stop(1, 45, 3),
            make_pit_stop(2, 15, 1),
        ]
        outcome = compute_special_outcome(1, "most_pitstops", [], stops, rds)
        assert outcome.actual_int == 3
        assert outcome.actual_driver_id == 1

    def test_ties_share(self):
        rds = make_round_drivers([(1, 44, "M"), (2, 1, "R")])
        stops = [
            make_pit_stop(1, 10, 1),
            make_pit_stop(1, 30, 2),
            make_pit_stop(2, 12, 1),
            make_pit_stop(2, 35, 2),
        ]
        outcome = compute_special_outcome(1, "most_pitstops", [], stops, rds)
        assert outcome.actual_int == 2
        assert set(outcome.actual_team_name.split(",")) == {"1", "2"}

    def test_no_stops_no_result(self):
        outcome = compute_special_outcome(1, "most_pitstops", [], [], [])
        assert outcome.no_result is True


# =============================================================================
# last_classified
# =============================================================================


class TestLastClassified:
    def test_lowest_classified_position(self):
        rds = make_round_drivers([
            (1, 44, "M"), (2, 1, "R"), (3, 16, "F"),
        ])
        results = [
            make_result(1, 1),
            make_result(15, 44),   # lapped but classified
            make_result(20, 16, is_classified=False, status="Engine"),
        ]
        outcome = compute_special_outcome(1, "last_classified", results, [], rds)
        assert outcome.actual_driver_id == 1   # car 44

    def test_no_classified_no_result(self):
        results = [
            make_result(20, 44, is_classified=False, status="Engine"),
        ]
        outcome = compute_special_outcome(1, "last_classified", results, [], [])
        assert outcome.no_result is True


# =============================================================================
# margin_of_victory
# =============================================================================


class TestMarginOfVictory:
    def test_close_finish(self):
        results = [
            make_result(1, 44, race_time_ms=5_500_000),
            make_result(2, 1, race_time_ms=5_501_234),
        ]
        outcome = compute_special_outcome(1, "margin_of_victory", results, [], [])
        assert outcome.actual_int == 1   # round(1.234) = 1

    def test_p2_lapped_no_result(self):
        results = [
            make_result(1, 44, race_time_ms=5_500_000),
            make_result(2, 1, race_time_ms=None, status="+1 Lap"),
        ]
        outcome = compute_special_outcome(1, "margin_of_victory", results, [], [])
        assert outcome.no_result is True

    def test_scoring_within_tolerance(self):
        cfg = make_config()
        outcome = SpecialOutcome(
            round_id=1, special_key="margin_of_victory",
            actual_int=12, no_result=False,
        )
        # Tolerance is 5s
        for predicted, expected in [
            (12, 10),   # exact
            (7, 10),    # delta 5 — boundary
            (17, 10),   # delta 5 — boundary
            (6, 0),     # delta 6 — out
            (18, 0),    # delta 6 — out
        ]:
            pred = SpecialPrediction(
                user_id=1, round_id=1, special_key="margin_of_victory",
                predicted_int=predicted,
            )
            assert score_special(pred, outcome, cfg) == expected


# =============================================================================
# lap_of_first_pitstop
# =============================================================================


class TestLapOfFirstPitstop:
    def test_earliest_lap(self):
        stops = [
            make_pit_stop(1, 18, 1),
            make_pit_stop(2, 12, 1),
            make_pit_stop(3, 22, 1),
        ]
        outcome = compute_special_outcome(1, "lap_of_first_pitstop", [], stops, [])
        assert outcome.actual_int == 12

    def test_no_pitstops_no_result(self):
        outcome = compute_special_outcome(1, "lap_of_first_pitstop", [], [], [])
        assert outcome.no_result is True

    def test_scoring_within_tolerance(self):
        cfg = make_config()
        outcome = SpecialOutcome(
            round_id=1, special_key="lap_of_first_pitstop",
            actual_int=15, no_result=False,
        )
        # Tolerance is 2 laps
        for predicted, expected in [
            (15, 10), (13, 10), (17, 10),
            (12, 0), (18, 0),
        ]:
            pred = SpecialPrediction(
                user_id=1, round_id=1, special_key="lap_of_first_pitstop",
                predicted_int=predicted,
            )
            assert score_special(pred, outcome, cfg) == expected


# =============================================================================
# pole_sitter_wins
# =============================================================================


class TestPoleSitterWins:
    def test_yes(self):
        results = [make_result(1, 44, grid_position=1)]
        outcome = compute_special_outcome(1, "pole_sitter_wins", results, [], [])
        assert outcome.actual_bool is True

    def test_no(self):
        results = [make_result(1, 44, grid_position=4)]
        outcome = compute_special_outcome(1, "pole_sitter_wins", results, [], [])
        assert outcome.actual_bool is False

    def test_scoring(self):
        cfg = make_config()
        outcome_yes = SpecialOutcome(
            round_id=1, special_key="pole_sitter_wins",
            actual_bool=True, no_result=False,
        )
        pred_yes = SpecialPrediction(
            user_id=1, round_id=1, special_key="pole_sitter_wins",
            predicted_bool=True,
        )
        pred_no = SpecialPrediction(
            user_id=1, round_id=1, special_key="pole_sitter_wins",
            predicted_bool=False,
        )
        assert score_special(pred_yes, outcome_yes, cfg) == 10
        assert score_special(pred_no, outcome_yes, cfg) == 0


# =============================================================================
# longest_stint
# =============================================================================


class TestLongestStint:
    def test_basic(self):
        # Driver 1 (car 44): no stops, runs 57 laps → stint 57.
        # Driver 2 (car 1): stops at lap 20, runs to 57 → stints 20 / 37.
        rds = make_round_drivers([(1, 44, "M"), (2, 1, "R")])
        results = [
            make_result(1, 44, laps_completed=57),
            make_result(2, 1, laps_completed=57),
        ]
        stops = [make_pit_stop(2, 20, 1)]
        outcome = compute_special_outcome(1, "longest_stint", results, stops, rds)
        assert outcome.actual_int == 57
        assert outcome.actual_driver_id == 1

    def test_dnf_stints_counted(self):
        # Driver 1 runs 30 laps no stop → stint 30.
        # Driver 2 stops at lap 5 then DNFs lap 50 → stints 5 / 45 → 45 wins.
        rds = make_round_drivers([(1, 44, "M"), (2, 1, "R")])
        results = [
            make_result(1, 44, laps_completed=30, is_classified=False, status="Engine"),
            make_result(2, 1, laps_completed=50, is_classified=False, status="Hydraulics"),
        ]
        stops = [make_pit_stop(2, 5, 1)]
        outcome = compute_special_outcome(1, "longest_stint", results, stops, rds)
        assert outcome.actual_int == 45
        assert outcome.actual_driver_id == 2


# =============================================================================
# biggest_team_gap
# =============================================================================


class TestBiggestTeamGap:
    def test_clear_gap(self):
        # Mercedes: P1 + P16 (gap 15). Red Bull: P2 + P3 (gap 1).
        rds = make_round_drivers([
            (1, 44, "Mercedes"), (2, 63, "Mercedes"),
            (3, 1, "Red Bull"), (4, 11, "Red Bull"),
        ])
        results = [
            make_result(1, 44, actual_driver_id=1),
            make_result(2, 1, actual_driver_id=3),
            make_result(3, 11, actual_driver_id=4),
            make_result(16, 63, actual_driver_id=2),
        ]
        outcome = compute_special_outcome(1, "biggest_team_gap", results, [], rds)
        assert outcome.actual_team_name == "Mercedes"
        assert outcome.actual_int == 15

    def test_team_with_dnf_excluded(self):
        # Mercedes: P1 + DNF → excluded.
        # Red Bull: P2 + P10 (gap 8) → wins.
        rds = make_round_drivers([
            (1, 44, "Mercedes"), (2, 63, "Mercedes"),
            (3, 1, "Red Bull"), (4, 11, "Red Bull"),
        ])
        results = [
            make_result(1, 44, actual_driver_id=1),
            make_result(2, 1, actual_driver_id=3),
            make_result(10, 11, actual_driver_id=4),
            make_result(20, 63, actual_driver_id=2, is_classified=False, status="Engine"),
        ]
        outcome = compute_special_outcome(1, "biggest_team_gap", results, [], rds)
        assert outcome.actual_team_name == "Red Bull"
        assert outcome.actual_int == 8

    def test_all_teams_have_dnfs_no_result(self):
        rds = make_round_drivers([(1, 44, "M"), (2, 63, "M")])
        results = [
            make_result(1, 44, is_classified=False, status="Engine"),
            make_result(2, 63, is_classified=False, status="Engine"),
        ]
        outcome = compute_special_outcome(1, "biggest_team_gap", results, [], rds)
        assert outcome.no_result is True

    def test_scoring(self):
        cfg = make_config()
        outcome = SpecialOutcome(
            round_id=1, special_key="biggest_team_gap",
            actual_team_name="Mercedes", actual_int=15, no_result=False,
        )
        pred_right = SpecialPrediction(
            user_id=1, round_id=1, special_key="biggest_team_gap",
            predicted_team_name="Mercedes",
        )
        pred_wrong = SpecialPrediction(
            user_id=1, round_id=1, special_key="biggest_team_gap",
            predicted_team_name="Red Bull",
        )
        assert score_special(pred_right, outcome, cfg) == 10
        assert score_special(pred_wrong, outcome, cfg) == 0


# =============================================================================
# Scoring edge cases
# =============================================================================


def test_score_no_result_is_zero():
    cfg = make_config()
    outcome = SpecialOutcome(
        round_id=1, special_key="first_retirement", no_result=True,
    )
    pred = SpecialPrediction(
        user_id=1, round_id=1, special_key="first_retirement",
        predicted_driver_id=1,
    )
    assert score_special(pred, outcome, cfg) == 0


def test_score_no_prediction_is_zero():
    cfg = make_config()
    outcome = SpecialOutcome(
        round_id=1, special_key="first_retirement",
        actual_driver_id=1, no_result=False,
    )
    assert score_special(None, outcome, cfg) == 0


def test_unknown_special_key_raises():
    with pytest.raises(ValueError):
        compute_special_outcome(1, "made_up_key", [], [], [])
