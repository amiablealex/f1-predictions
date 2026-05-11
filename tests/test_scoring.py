"""Tests for the scoring engine.

Tests are pure — they construct unsaved ORM instances in-memory and call
engine functions directly. No DB roundtrip required.
"""
from __future__ import annotations

import pytest

from app.config import Config
from app.models.driver import RoundDriver
from app.models.prediction import (
    DnfCountPrediction,
    FastestLapPrediction,
    PlacesGainedPrediction,
    PoleTimePrediction,
    PredictionType,
    QualiRandomDriverPrediction,
    Top3QualiPrediction,
    Top3SprintPrediction,
    Top10Prediction,
)
from app.models.result import SessionResult
from app.models.round import (
    Round,
    RoundScoringConfig,
    ScoringPhase,
    Session,
    SessionType,
)
from app.scoring.engine import (
    UserPredictions,
    build_phase_scores,
    build_quali_phase_scores,
    build_race_phase_scores,
    build_sprint_phase_scores,
    score_dnf_count,
    score_fastest_lap,
    score_places_gained,
    score_pole_time,
    score_quali_random_driver,
    score_quali_top3_slot,
    score_sprint_top3_slot,
    score_top10_slot,
)


# =============================================================================
# Test fixtures (plain functions returning in-memory model instances)
# =============================================================================


def make_config(**overrides) -> RoundScoringConfig:
    """Build a RoundScoringConfig from defaults, with optional overrides.

    Delegates to the production `from_defaults` classmethod so the test
    helper picks up new scoring fields automatically.
    """
    cfg = RoundScoringConfig.from_defaults(
        round_id=1, defaults=Config.SCORING_DEFAULTS,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def make_round_drivers(mapping: dict[int, int]) -> list[RoundDriver]:
    """`mapping` is {expected_driver_id: car_number}."""
    return [
        RoundDriver(round_id=1, car_number=car, expected_driver_id=drv)
        for drv, car in mapping.items()
    ]


def make_result(
    position: int,
    car_number: int,
    actual_driver_id: int = 999,
    status: str = "Finished",
    is_classified: bool = True,
    is_fastest_lap: bool = False,
    best_qualifying_time_ms: int | None = None,
    grid_position: int | None = None,
    laps_completed: int | None = None,
    race_time_ms: int | None = None,
) -> SessionResult:
    return SessionResult(
        session_id=1,
        position=position,
        car_number=car_number,
        actual_driver_id=actual_driver_id,
        status=status,
        is_classified=is_classified,
        is_fastest_lap=is_fastest_lap,
        best_qualifying_time_ms=best_qualifying_time_ms,
        grid_position=grid_position,
        laps_completed=laps_completed,
        race_time_ms=race_time_ms,
    )


def _baseline_field(n: int = 20) -> list[SessionResult]:
    """Build an n-car race field where every car finishes at its grid spot.

    Useful as a backdrop for places_gained tests — override one row to
    exercise the case under test.
    """
    return [
        make_result(
            position=p,
            car_number=p + 1000,
            actual_driver_id=p + 1000,
            grid_position=p,
        )
        for p in range(1, n + 1)
    ]


# =============================================================================
# score_top10_slot — race top 10 (unchanged behaviour)
# =============================================================================


class TestScoreTop10Slot:
    def setup_method(self):
        self.round_drivers = make_round_drivers({1: 44, 2: 1, 3: 16})
        self.results = [
            make_result(1, 44),
            make_result(2, 1),
            make_result(3, 16),
        ]
        self.cfg = make_config()

    def test_correct_position_awards_correct_points(self):
        pts = score_top10_slot(1, 1, self.results, self.round_drivers, self.cfg)
        assert pts == 10

    def test_one_off_awards_one_off_points(self):
        pts = score_top10_slot(2, 1, self.results, self.round_drivers, self.cfg)
        assert pts == 5

    def test_two_off_awards_two_off_points(self):
        pts = score_top10_slot(3, 1, self.results, self.round_drivers, self.cfg)
        assert pts == 2

    def test_more_than_two_off_zero(self):
        pts = score_top10_slot(5, 1, self.results, self.round_drivers, self.cfg)
        assert pts == 0

    def test_dnf_scores_zero_even_if_position_matches(self):
        results = [
            make_result(20, 44, status="Engine", is_classified=False),
            make_result(1, 1),
        ]
        pts = score_top10_slot(20, 1, results, self.round_drivers, self.cfg)
        assert pts == 0

    def test_unknown_predicted_driver_zero(self):
        pts = score_top10_slot(1, 99, self.results, self.round_drivers, self.cfg)
        assert pts == 0


def test_substitution_awards_points_to_predicted_seat():
    round_drivers = make_round_drivers({1: 44})
    results = [make_result(position=1, car_number=44, actual_driver_id=50)]
    cfg = make_config()
    pts = score_top10_slot(1, 1, results, round_drivers, cfg)
    assert pts == 10


# =============================================================================
# score_quali_top3_slot — bucketed scoring (CHANGED in Phase 3)
#
# Buckets default to:
#   delta == 0     → +5
#   delta == 1     → +2
#   delta == 2     → +1
#   delta in 3..5  →  0
#   delta in 6..8  → -2
#   delta >= 9     → -5
# =============================================================================


class TestScoreQualiTop3Bucketed:
    def setup_method(self):
        # Driver i is regular for car i for i in 1..20.
        self.round_drivers = make_round_drivers({i: i for i in range(1, 21)})
        # Quali results: car i finishes Pi for all 20 entries.
        self.results = [make_result(p, p, actual_driver_id=p) for p in range(1, 21)]
        self.cfg = make_config()

    def _score(self, predicted_pos: int, predicted_driver_id: int) -> int:
        return score_quali_top3_slot(
            predicted_pos, predicted_driver_id,
            self.results, self.round_drivers, self.cfg,
        )

    def test_exact(self):
        # Predicted driver 1 (car 1) at P1; actual P1 → delta 0 → +5
        assert self._score(1, 1) == 5

    def test_one_away(self):
        # Predicted driver 1 at P2; actual P1 → delta 1 → +2
        assert self._score(2, 1) == 2

    def test_two_away(self):
        # Predicted driver 1 at P3; actual P1 → delta 2 → +1
        assert self._score(3, 1) == 1

    def test_within_five_zero(self):
        # delta 3, 4, 5 → 0
        assert self._score(4, 1) == 0    # delta 3
        assert self._score(5, 1) == 0    # delta 4
        assert self._score(6, 1) == 0    # delta 5

    def test_six_to_eight_negative_two(self):
        assert self._score(7, 1) == -2   # delta 6
        assert self._score(8, 1) == -2   # delta 7
        assert self._score(9, 1) == -2   # delta 8

    def test_nine_or_more_negative_five(self):
        assert self._score(10, 1) == -5  # delta 9
        assert self._score(15, 1) == -5  # delta 14
        assert self._score(20, 1) == -5  # delta 19

    def test_predicted_driver_not_in_results_scores_zero(self):
        # Driver 99 not in lineup → no actual lookup → 0 (no penalty for
        # picking someone who didn't qualify; would be unfair).
        pts = self._score(1, 99)
        assert pts == 0


# =============================================================================
# score_quali_random_driver — same bucket scheme, different signature
# =============================================================================


class TestScoreQualiRandomDriver:
    def setup_method(self):
        self.round_drivers = make_round_drivers({1: 44, 2: 1, 3: 16})
        # Driver 1 (car 44) actually qualified P5
        self.results = [
            make_result(1, 1, actual_driver_id=2),
            make_result(2, 16, actual_driver_id=3),
            make_result(3, 7, actual_driver_id=7),
            make_result(4, 9, actual_driver_id=9),
            make_result(5, 44, actual_driver_id=1),
            make_result(6, 11, actual_driver_id=11),
        ]
        self.cfg = make_config()

    def test_exact_pick(self):
        # Predicted P5 for driver 1, actual P5 → +5
        pts = score_quali_random_driver(
            5, 1, self.results, self.round_drivers, self.cfg,
        )
        assert pts == 5

    def test_off_by_three(self):
        # Predicted P2 for driver 1, actual P5 → delta 3 → 0
        pts = score_quali_random_driver(
            2, 1, self.results, self.round_drivers, self.cfg,
        )
        assert pts == 0

    def test_far_off_negative(self):
        # Predicted P15 for driver 1, actual P5 → delta 10 → -5
        pts = score_quali_random_driver(
            15, 1, self.results, self.round_drivers, self.cfg,
        )
        assert pts == -5

    def test_random_driver_not_in_results_zero(self):
        # The random driver didn't appear in quali (extremely rare — DNS).
        # Score 0, no penalty.
        results = [make_result(1, 1, actual_driver_id=2)]
        pts = score_quali_random_driver(
            1, 99, results, self.round_drivers, self.cfg,
        )
        assert pts == 0


# =============================================================================
# score_pole_time (unchanged)
# =============================================================================


class TestScorePoleTime:
    def setup_method(self):
        self.cfg = make_config()
        self.actual_ms = 83_456

    def test_within_inner_bucket_awards_max(self):
        pts = score_pole_time(self.actual_ms + 100, self.actual_ms, self.cfg)
        assert pts == 10

    def test_at_inner_bucket_boundary_awards_max(self):
        pts = score_pole_time(self.actual_ms + 200, self.actual_ms, self.cfg)
        assert pts == 10

    def test_outside_inner_inside_outer_awards_outer(self):
        pts = score_pole_time(self.actual_ms - 500, self.actual_ms, self.cfg)
        assert pts == 5

    def test_at_outer_boundary_awards_outer(self):
        pts = score_pole_time(self.actual_ms - 1000, self.actual_ms, self.cfg)
        assert pts == 5

    def test_outside_outer_zero(self):
        pts = score_pole_time(self.actual_ms + 1500, self.actual_ms, self.cfg)
        assert pts == 0

    def test_no_actual_pole_zero(self):
        pts = score_pole_time(self.actual_ms, None, self.cfg)
        assert pts == 0


# =============================================================================
# score_fastest_lap (unchanged)
# =============================================================================


class TestScoreFastestLap:
    def setup_method(self):
        self.round_drivers = make_round_drivers({1: 44, 2: 1, 3: 16})
        self.cfg = make_config()

    def test_correct_picks_fastest_lap_setter(self):
        results = [
            make_result(1, 44),
            make_result(2, 1, is_fastest_lap=True),
            make_result(3, 16),
        ]
        pts = score_fastest_lap(2, results, self.round_drivers, self.cfg)
        assert pts == 10

    def test_wrong_pick_zero(self):
        results = [
            make_result(1, 44, is_fastest_lap=True),
            make_result(2, 1),
        ]
        pts = score_fastest_lap(2, results, self.round_drivers, self.cfg)
        assert pts == 0

    def test_substitution_awards_points_to_seat_picker(self):
        results = [make_result(1, 44, actual_driver_id=50, is_fastest_lap=True)]
        pts = score_fastest_lap(1, results, self.round_drivers, self.cfg)
        assert pts == 10


# =============================================================================
# score_dnf_count (unchanged)
# =============================================================================


class TestScoreDnfCount:
    def setup_method(self):
        self.cfg = make_config()

    def test_correct(self):
        assert score_dnf_count(5, 5, self.cfg) == 10

    def test_one_off(self):
        assert score_dnf_count(4, 5, self.cfg) == 5
        assert score_dnf_count(6, 5, self.cfg) == 5

    def test_two_off_zero(self):
        assert score_dnf_count(3, 5, self.cfg) == 0

    def test_actual_count_unknown_zero(self):
        assert score_dnf_count(5, None, self.cfg) == 0


# =============================================================================
# score_places_gained — NEW in Phase 3
# =============================================================================


class TestScorePlacesGained:
    def setup_method(self):
        # Driver 1 is regular for car 44.
        self.round_drivers = make_round_drivers({1: 44, 2: 1, 3: 16})

    def test_classified_gained_places(self):
        # Car 44 grid 5, finished P2 → +3
        results = _baseline_field(20)
        results[1] = make_result(2, 44, actual_driver_id=1, grid_position=5)
        pts = score_places_gained(1, results, self.round_drivers)
        assert pts == 3

    def test_classified_lost_places(self):
        # Car 44 grid 2, finished P10 → -8
        results = _baseline_field(20)
        results[9] = make_result(10, 44, actual_driver_id=1, grid_position=2)
        pts = score_places_gained(1, results, self.round_drivers)
        assert pts == -8

    def test_classified_no_change(self):
        results = _baseline_field(20)
        results[4] = make_result(5, 44, actual_driver_id=1, grid_position=5)
        pts = score_places_gained(1, results, self.round_drivers)
        assert pts == 0

    def test_dnf_treated_as_last_classified_plus_one(self):
        # 18 classified + 2 DNFs. Car 44 DNF'd from grid 5.
        # treated_finish = max_classified + 1 = 18 + 1 = 19.
        # 5 - 19 = -14
        results = []
        for p in range(1, 19):
            results.append(make_result(p, p + 1000, actual_driver_id=p + 1000, grid_position=p))
        results.append(make_result(
            19, 44, actual_driver_id=1,
            status="Engine", is_classified=False, grid_position=5,
        ))
        results.append(make_result(
            20, 17, actual_driver_id=99,
            status="Accident", is_classified=False, grid_position=15,
        ))
        pts = score_places_gained(1, results, self.round_drivers)
        assert pts == 5 - 19

    def test_dns_scores_zero(self):
        # Driver never started — predicted_driver_id pointed at a no-show.
        # No penalty for picking unluckily.
        results = _baseline_field(20)
        results[19] = make_result(
            20, 44, actual_driver_id=1,
            status="Did not start", is_classified=False, grid_position=5,
        )
        pts = score_places_gained(1, results, self.round_drivers)
        assert pts == 0

    def test_pit_lane_start_treated_as_last_grid_slot(self):
        # 20-car field. Car 44 grid 0 (pit lane), finished P5.
        # Pit lane → treated as 20. 20 - 5 = +15.
        results = _baseline_field(20)
        results[4] = make_result(5, 44, actual_driver_id=1, grid_position=0)
        pts = score_places_gained(1, results, self.round_drivers)
        assert pts == 15

    def test_no_grid_data_scores_zero(self):
        # Defensive — old session_results from before the grid_position
        # column existed have NULL.
        results = [make_result(2, 44, actual_driver_id=1, grid_position=None)]
        pts = score_places_gained(1, results, self.round_drivers)
        assert pts == 0

    def test_driver_not_in_lineup_zero(self):
        results = [make_result(1, 99, actual_driver_id=99, grid_position=5)]
        pts = score_places_gained(99, results, self.round_drivers)
        assert pts == 0

    def test_substitution_uses_seat(self):
        # Predicted Hamilton (driver 1, regular for car 44).
        # Bottas (driver 50) drove car 44 from grid 10 to finish P3 → +7.
        results = _baseline_field(20)
        results[2] = make_result(3, 44, actual_driver_id=50, grid_position=10)
        pts = score_places_gained(1, results, self.round_drivers)
        assert pts == 7


# =============================================================================
# Phase orchestrator: build_race_phase_scores
# =============================================================================


class TestRacePhaseOrchestration:
    def setup_method(self):
        self.round_drivers = make_round_drivers({i: i for i in range(1, 11)})
        self.cfg = make_config()

        # Race result: drivers 1..10 finish in order. Driver 5 has FL.
        # No DNFs. Grid matches finish, so places_gained == 0 for all.
        self.race_results = [
            make_result(
                p, p, actual_driver_id=p,
                is_fastest_lap=(p == 5), grid_position=p,
            )
            for p in range(1, 11)
        ]
        self.race_session = Session(
            id=1, round_id=1,
            session_type=SessionType.RACE,
            scheduled_start=None,
            status=None,
            pole_time_ms=None,
            fastest_lap_driver_id=5,
            dnf_count=0,
        )
        self.race_session.results = self.race_results

    def test_perfect_predictions_get_full_points(self):
        preds = UserPredictions(
            top10=[Top10Prediction(user_id=1, round_id=1, position=p, predicted_driver_id=p)
                   for p in range(1, 11)],
            fastest_lap=FastestLapPrediction(user_id=1, round_id=1, predicted_driver_id=5),
            dnf_count=DnfCountPrediction(user_id=1, round_id=1, predicted_count=0),
        )
        scores = build_race_phase_scores(
            user_id=1, round_id=1, user_preds=preds,
            race_session=self.race_session,
            round_drivers=self.round_drivers, config=self.cfg,
            round_obj=Round(id=1, season=2026, round_number=1, gp_name="T"),
            special_outcomes_by_key={},
        )
        # 10 top-10 + 1 FL + 1 DNF + 1 places_gained = 13 rows
        assert len(scores) == 13
        top10_total = sum(s.points for s in scores if s.kind == PredictionType.RACE_TOP10)
        assert top10_total == 100
        fl_total = sum(s.points for s in scores if s.kind == PredictionType.FASTEST_LAP)
        assert fl_total == 10
        dnf_total = sum(s.points for s in scores if s.kind == PredictionType.DNF_COUNT)
        assert dnf_total == 10
        # User didn't predict places_gained → 0 points but row exists.
        pg_rows = [s for s in scores if s.kind == PredictionType.PLACES_GAINED]
        assert len(pg_rows) == 1
        assert pg_rows[0].points == 0

    def test_no_predictions_yields_zero_points_with_full_row_set(self):
        preds = UserPredictions()
        scores = build_race_phase_scores(
            user_id=1, round_id=1, user_preds=preds,
            race_session=self.race_session,
            round_drivers=self.round_drivers, config=self.cfg,
            round_obj=Round(id=1, season=2026, round_number=1, gp_name="T"),
            special_outcomes_by_key={},
        )
        assert len(scores) == 13
        assert all(s.points == 0 for s in scores)

    def test_partial_predictions_score_what_was_submitted(self):
        preds = UserPredictions(
            top10=[Top10Prediction(user_id=1, round_id=1, position=1, predicted_driver_id=1)],
        )
        scores = build_race_phase_scores(
            user_id=1, round_id=1, user_preds=preds,
            race_session=self.race_session,
            round_drivers=self.round_drivers, config=self.cfg,
            round_obj=Round(id=1, season=2026, round_number=1, gp_name="T"),
            special_outcomes_by_key={},
        )
        assert len(scores) == 13
        p1_score = next(s for s in scores
                        if s.kind == PredictionType.RACE_TOP10 and s.position == 1)
        assert p1_score.points == 10
        p2_score = next(s for s in scores
                        if s.kind == PredictionType.RACE_TOP10 and s.position == 2)
        assert p2_score.points == 0

    def test_places_gained_prediction_scores(self):
        # User predicts driver 5 for places_gained. Driver 5 grid 5, finish 5
        # → 0 (in this test setup). Modify so driver 5 gains 3.
        self.race_results[4] = make_result(
            position=2, car_number=5, actual_driver_id=5,
            is_fastest_lap=True, grid_position=5,
        )
        # Recompute the session results list.
        self.race_session.results = self.race_results

        preds = UserPredictions(
            places_gained=PlacesGainedPrediction(
                user_id=1, round_id=1, predicted_driver_id=5,
            ),
        )
        scores = build_race_phase_scores(
            user_id=1, round_id=1, user_preds=preds,
            race_session=self.race_session,
            round_drivers=self.round_drivers, config=self.cfg,
            round_obj=Round(id=1, season=2026, round_number=1, gp_name="T"),
            special_outcomes_by_key={},
        )
        pg = next(s for s in scores if s.kind == PredictionType.PLACES_GAINED)
        assert pg.points == 3   # 5 - 2


# =============================================================================
# Phase orchestrator: build_quali_phase_scores
# =============================================================================


def _make_round_with_random_driver(random_round_driver: RoundDriver) -> Round:
    """Build an in-memory Round with the random_quali_driver relationship
    populated, without going through the DB."""
    r = Round(
        id=1, season=2026, round_number=1,
        gp_name="Test GP", country_code="IT",
    )
    r.random_quali_driver = random_round_driver
    return r


def test_quali_phase_perfect_top3_and_pole():
    round_drivers = make_round_drivers({1: 44, 2: 1, 3: 16})
    cfg = make_config()
    quali_results = [
        make_result(1, 1, actual_driver_id=2, best_qualifying_time_ms=83_456),
        make_result(2, 44, actual_driver_id=1),
        make_result(3, 16, actual_driver_id=3),
    ]
    quali_session = Session(
        id=1, round_id=1,
        session_type=SessionType.QUALIFYING,
        pole_time_ms=83_456,
    )
    quali_session.results = quali_results

    # Round has no random driver — orchestrator still emits a 0-point
    # row so the UI renders consistently.
    round_obj = Round(id=1, season=2026, round_number=1, gp_name="Test")

    preds = UserPredictions(
        quali_top3=[
            Top3QualiPrediction(user_id=1, round_id=1, position=1, predicted_driver_id=2),
            Top3QualiPrediction(user_id=1, round_id=1, position=2, predicted_driver_id=1),
            Top3QualiPrediction(user_id=1, round_id=1, position=3, predicted_driver_id=3),
        ],
        pole_time=PoleTimePrediction(user_id=1, round_id=1, predicted_time_ms=83_456),
    )
    scores = build_quali_phase_scores(
        user_id=1, round_id=1, user_preds=preds,
        quali_session=quali_session,
        round_drivers=round_drivers, config=cfg,
        round_obj=round_obj,
    )
    # 3 top3 + 1 pole + 1 random_driver + 1 h2h + 1 qnth = 7 rows
    assert len(scores) == 7
    top3_total = sum(s.points for s in scores if s.kind == PredictionType.QUALI_TOP3)
    assert top3_total == 5 + 5 + 5
    pole = next(s for s in scores if s.kind == PredictionType.POLE_TIME)
    assert pole.points == 10
    rqd = next(s for s in scores if s.kind == PredictionType.QUALI_RANDOM_DRIVER)
    assert rqd.points == 0   # neither prediction nor driver set
    h2h = next(s for s in scores if s.kind == PredictionType.QUALI_HEAD_TO_HEAD)
    assert h2h.points == 0   # neither prediction nor driver set
    qnth = next(s for s in scores if s.kind == PredictionType.QUALI_NTH)
    assert qnth.points == 0   # neither prediction nor N set


def test_quali_phase_random_driver_scores_when_assigned():
    round_drivers = make_round_drivers({1: 44, 2: 1, 3: 16})
    cfg = make_config()
    # Driver 1 (car 44) actually qualified P5
    quali_results = [
        make_result(1, 1, actual_driver_id=2),
        make_result(2, 16, actual_driver_id=3),
        make_result(3, 7, actual_driver_id=7),
        make_result(4, 9, actual_driver_id=9),
        make_result(5, 44, actual_driver_id=1),
    ]
    quali_session = Session(
        id=1, round_id=1, session_type=SessionType.QUALIFYING, pole_time_ms=None,
    )
    quali_session.results = quali_results

    # Round's random driver is driver 1 (the RoundDriver for car 44).
    random_rd = round_drivers[0]
    round_obj = _make_round_with_random_driver(random_rd)

    preds = UserPredictions(
        quali_random_driver=QualiRandomDriverPrediction(
            user_id=1, round_id=1, predicted_position=5,
        ),
    )
    scores = build_quali_phase_scores(
        user_id=1, round_id=1, user_preds=preds,
        quali_session=quali_session,
        round_drivers=round_drivers, config=cfg,
        round_obj=round_obj,
    )
    rqd = next(s for s in scores if s.kind == PredictionType.QUALI_RANDOM_DRIVER)
    assert rqd.points == 5   # exact prediction


# =============================================================================
# Phase orchestrator: build_sprint_phase_scores (unchanged)
# =============================================================================


def test_sprint_phase_perfect_score():
    round_drivers = make_round_drivers({1: 44, 2: 1, 3: 16})
    cfg = make_config()

    sr_results = [make_result(1, 44), make_result(2, 1), make_result(3, 16)]
    sr_session = Session(id=2, round_id=1, session_type=SessionType.SPRINT_RACE)
    sr_session.results = sr_results

    preds = UserPredictions(
        sprint_top3=[
            Top3SprintPrediction(user_id=1, round_id=1, position=1, predicted_driver_id=1),
            Top3SprintPrediction(user_id=1, round_id=1, position=2, predicted_driver_id=2),
            Top3SprintPrediction(user_id=1, round_id=1, position=3, predicted_driver_id=3),
        ],
    )
    scores = build_sprint_phase_scores(
        user_id=1, round_id=1, user_preds=preds,
        sprint_quali_session=None, sprint_race_session=sr_session,
        round_drivers=round_drivers, config=cfg,
    )
    assert len(scores) == 3
    total = sum(s.points for s in scores)
    assert total == 5 + 5 + 5


# =============================================================================
# Frozen config: changing defaults does NOT alter past-round scoring
# =============================================================================


def test_frozen_config_unaffected_by_default_changes(monkeypatch):
    """Mutating Config.SCORING_DEFAULTS must not change scores for a round
    whose RoundScoringConfig was already snapshotted with old values."""
    old_cfg = make_config()
    monkeypatch.setitem(Config.SCORING_DEFAULTS, "race_top10_correct", 99)

    round_drivers = make_round_drivers({1: 44})
    results = [make_result(1, 44)]
    pts = score_top10_slot(1, 1, results, round_drivers, old_cfg)
    assert pts == 10


# =============================================================================
# Phase dispatch
# =============================================================================


def test_dispatch_unknown_phase_raises():
    with pytest.raises(ValueError):
        build_phase_scores(
            user_id=1, round_id=1, phase="nonsense",
            user_preds=UserPredictions(), sessions_by_type={},
            round_drivers=[], config=make_config(),
            round_obj=Round(id=1, season=2026, round_number=1, gp_name="T"),
        )
# =============================================================================
# score_quali_h2h — NEW in Phase 4
# =============================================================================


class TestScoreQualiH2h:
    def setup_method(self):
        # Two teammates: driver 1 in car 44, driver 2 in car 1.
        self.round_drivers = make_round_drivers({1: 44, 2: 1})
        self.rd_a = self.round_drivers[0]   # driver 1, car 44
        self.rd_b = self.round_drivers[1]   # driver 2, car 1
        self.cfg = make_config()

    def _score(self, predicted_driver_id, results):
        from app.scoring.engine import score_quali_h2h
        return score_quali_h2h(
            predicted_driver_id, self.rd_a, self.rd_b, results, self.cfg,
        )

    def test_correct_pick_a_wins(self):
        # Car 44 P3, car 1 P8 → a wins. Picker chose driver 1 → +5.
        results = [
            make_result(3, 44, actual_driver_id=1),
            make_result(8, 1, actual_driver_id=2),
        ]
        assert self._score(1, results) == 5

    def test_correct_pick_b_wins(self):
        # Car 44 P10, car 1 P4 → b wins. Picker chose driver 2 → +5.
        results = [
            make_result(10, 44, actual_driver_id=1),
            make_result(4, 1, actual_driver_id=2),
        ]
        assert self._score(2, results) == 5

    def test_wrong_pick_zero(self):
        results = [
            make_result(3, 44, actual_driver_id=1),
            make_result(8, 1, actual_driver_id=2),
        ]
        # Picker chose driver 2 (loser) → 0.
        assert self._score(2, results) == 0

    def test_one_dnq_other_wins(self):
        # Car 44 set a time, car 1 didn't appear (DNQ).
        results = [make_result(15, 44, actual_driver_id=1)]
        # Picker chose driver 1 (the one who set a time) → +5.
        assert self._score(1, results) == 5
        # Picker chose driver 2 → 0.
        assert self._score(2, results) == 0

    def test_both_dnq_no_scoring(self):
        results = []
        assert self._score(1, results) == 0
        assert self._score(2, results) == 0

    def test_substitution_aware(self):
        # Substitute drove car 44 (actual_driver_id 50) and qualified P2.
        # Regular for car 44 is driver 1, so a "won" the h2h.
        results = [
            make_result(2, 44, actual_driver_id=50),
            make_result(7, 1, actual_driver_id=2),
        ]
        assert self._score(1, results) == 5
        assert self._score(2, results) == 0


# =============================================================================
# score_quali_nth — NEW in Phase 4
# =============================================================================


class TestScoreQualiNth:
    def setup_method(self):
        # 20-driver lineup, car i for driver i.
        self.round_drivers = make_round_drivers({i: i for i in range(1, 21)})
        # Each driver qualifies at position equal to their id.
        self.results = [make_result(p, p, actual_driver_id=p) for p in range(1, 21)]
        self.cfg = make_config()

    def _score(self, predicted_driver_id, n):
        from app.scoring.engine import score_quali_nth
        return score_quali_nth(
            predicted_driver_id, n, self.results, self.round_drivers, self.cfg,
        )

    def test_exact(self):
        # N=14, predicted driver 14 actually qualifies P14 → +5.
        assert self._score(14, 14) == 5

    def test_one_off(self):
        # N=14, predicted driver 13 (actual P13) → delta 1 → +2.
        assert self._score(13, 14) == 2

    def test_two_off(self):
        assert self._score(12, 14) == 1

    def test_within_five_zero(self):
        assert self._score(11, 14) == 0     # delta 3
        assert self._score(9, 14) == 0      # delta 5

    def test_six_to_eight_negative_two(self):
        assert self._score(8, 14) == -2     # delta 6
        assert self._score(6, 14) == -2     # delta 8

    def test_far_off_negative_five(self):
        assert self._score(1, 14) == -5     # delta 13

    def test_picked_driver_dns_zero(self):
        # Driver 99 not in results → 0.
        assert self._score(99, 14) == 0


# =============================================================================
# Phase orchestrator: build_quali_phase_scores with h2h + qnth populated
# =============================================================================


def _make_round_with_full_quali_selections(
    random_rd: RoundDriver | None,
    qh2h_a: RoundDriver | None,
    qh2h_b: RoundDriver | None,
    qnth_n: int | None,
) -> Round:
    """Build an in-memory Round with all quali-phase selections set."""
    r = Round(
        id=1, season=2026, round_number=1,
        gp_name="Test GP", country_code="IT",
    )
    r.random_quali_driver = random_rd
    r.qh2h_driver_a = qh2h_a
    r.qh2h_driver_b = qh2h_b
    r.quali_nth_position = qnth_n
    return r


def test_quali_phase_scores_h2h_and_qnth_when_set():
    # Two teammates (drivers 1 and 2). Driver 1 (car 44) qualifies P3,
    # driver 2 (car 1) qualifies P8. N=14, driver 14 qualifies P14.
    round_drivers = make_round_drivers({i: (44 if i == 1 else i) for i in range(1, 21)})
    cfg = make_config()
    quali_results = [
        make_result(3, 44, actual_driver_id=1),
        make_result(8, 1, actual_driver_id=2),
    ] + [
        make_result(p, p, actual_driver_id=p) for p in range(3, 21) if p not in (3, 8)
    ]
    quali_session = Session(
        id=1, round_id=1,
        session_type=SessionType.QUALIFYING, pole_time_ms=None,
    )
    quali_session.results = quali_results

    rd_a = round_drivers[0]   # driver 1, car 44
    rd_b = round_drivers[1]   # driver 2, car 1
    round_obj = _make_round_with_full_quali_selections(
        random_rd=None, qh2h_a=rd_a, qh2h_b=rd_b, qnth_n=14,
    )

    from app.models.prediction import QualiHeadToHeadPrediction, QualiNthPrediction
    preds = UserPredictions(
        qh2h=QualiHeadToHeadPrediction(
            user_id=1, round_id=1, predicted_driver_id=1,   # correct
        ),
        qnth=QualiNthPrediction(
            user_id=1, round_id=1, predicted_driver_id=14,  # exact
        ),
    )
    scores = build_quali_phase_scores(
        user_id=1, round_id=1, user_preds=preds,
        quali_session=quali_session,
        round_drivers=round_drivers, config=cfg,
        round_obj=round_obj,
    )
    assert len(scores) == 7
    h2h = next(s for s in scores if s.kind == PredictionType.QUALI_HEAD_TO_HEAD)
    assert h2h.points == 5
    qnth = next(s for s in scores if s.kind == PredictionType.QUALI_NTH)
    assert qnth.points == 5


def test_quali_phase_h2h_missing_team_selection_yields_zero():
    """If the round's h2h drivers aren't set yet (worker hasn't picked
    them), orchestrator still emits a 0-point row."""
    round_drivers = make_round_drivers({1: 44, 2: 1})
    cfg = make_config()
    quali_session = Session(
        id=1, round_id=1, session_type=SessionType.QUALIFYING, pole_time_ms=None,
    )
    quali_session.results = []

    round_obj = _make_round_with_full_quali_selections(
        random_rd=None, qh2h_a=None, qh2h_b=None, qnth_n=None,
    )

    from app.models.prediction import QualiHeadToHeadPrediction
    preds = UserPredictions(
        qh2h=QualiHeadToHeadPrediction(
            user_id=1, round_id=1, predicted_driver_id=1,
        ),
    )
    scores = build_quali_phase_scores(
        user_id=1, round_id=1, user_preds=preds,
        quali_session=quali_session,
        round_drivers=round_drivers, config=cfg,
        round_obj=round_obj,
    )
    h2h = next(s for s in scores if s.kind == PredictionType.QUALI_HEAD_TO_HEAD)
    assert h2h.points == 0
