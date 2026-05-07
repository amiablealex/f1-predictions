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
    PoleTimePrediction,
    PredictionType,
    Top3QualiPrediction,
    Top3SprintPrediction,
    Top10Prediction,
)
from app.models.result import SessionResult
from app.models.round import (
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
    score_pole_time,
    score_quali_top3_slot,
    score_sprint_top3_slot,
    score_top10_slot,
)


# =============================================================================
# Test fixtures (plain functions returning in-memory model instances)
# =============================================================================


def make_config(**overrides) -> RoundScoringConfig:
    """Build a RoundScoringConfig from defaults, with optional overrides."""
    d = Config.SCORING_DEFAULTS
    cfg = RoundScoringConfig(
        round_id=1,
        race_top10_correct=d["race_top10_correct"],
        race_top10_one_off=d["race_top10_one_off"],
        race_top10_two_off=d["race_top10_two_off"],
        quali_top3_correct=d["quali_top3_correct"],
        quali_top3_one_off=d["quali_top3_one_off"],
        pole_time_buckets=list(d["pole_time_buckets"]),
        sprint_top3_correct=d["sprint_top3_correct"],
        sprint_top3_one_off=d["sprint_top3_one_off"],
        fastest_lap_correct=d["fastest_lap_correct"],
        dnf_count_correct=d["dnf_count_correct"],
        dnf_count_one_off=d["dnf_count_one_off"],
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
    )


# =============================================================================
# score_top10_slot — race top 10
# =============================================================================


class TestScoreTop10Slot:
    def setup_method(self):
        # Drivers 1, 2, 3 are in cars 44, 1, 16.
        self.round_drivers = make_round_drivers({1: 44, 2: 1, 3: 16})
        # Race result: car 44 finished P1, car 1 P2, car 16 P3.
        self.results = [
            make_result(1, 44),
            make_result(2, 1),
            make_result(3, 16),
        ]
        self.cfg = make_config()

    def test_correct_position_awards_correct_points(self):
        # Predicted driver 1 (car 44) at P1 — actual P1. Correct.
        pts = score_top10_slot(1, 1, self.results, self.round_drivers, self.cfg)
        assert pts == 10

    def test_one_off_awards_one_off_points(self):
        # Predicted driver 1 (car 44) at P2 — actual P1. Off by 1.
        pts = score_top10_slot(2, 1, self.results, self.round_drivers, self.cfg)
        assert pts == 5

    def test_two_off_awards_two_off_points(self):
        # Predicted driver 1 (car 44) at P3 — actual P1. Off by 2.
        pts = score_top10_slot(3, 1, self.results, self.round_drivers, self.cfg)
        assert pts == 2

    def test_more_than_two_off_zero(self):
        pts = score_top10_slot(5, 1, self.results, self.round_drivers, self.cfg)
        assert pts == 0

    def test_dnf_scores_zero_even_if_position_matches(self):
        # Car 44 DNF'd, returned at P20 unclassified.
        results = [
            make_result(20, 44, status="Engine", is_classified=False),
            make_result(1, 1),
        ]
        pts = score_top10_slot(20, 1, results, self.round_drivers, self.cfg)
        assert pts == 0

    def test_unknown_predicted_driver_zero(self):
        # Driver 99 not in this round's lineup
        pts = score_top10_slot(1, 99, self.results, self.round_drivers, self.cfg)
        assert pts == 0


# =============================================================================
# Substitution: predicted Hamilton, Bottas drove car 44, Bottas won
# =============================================================================


def test_substitution_awards_points_to_predicted_seat():
    # Driver 1 (Hamilton) is the regular for car 44. Bottas (driver 50)
    # drove it this round and won.
    round_drivers = make_round_drivers({1: 44})
    results = [
        make_result(position=1, car_number=44, actual_driver_id=50),
    ]
    cfg = make_config()
    # User predicted Hamilton (driver 1) at P1 — should score full points.
    pts = score_top10_slot(1, 1, results, round_drivers, cfg)
    assert pts == 10


# =============================================================================
# score_quali_top3_slot — qualifying top 3 (no two-off bucket)
# =============================================================================


class TestScoreQualiTop3:
    def setup_method(self):
        self.round_drivers = make_round_drivers({1: 44, 2: 1, 3: 16})
        self.results = [
            make_result(1, 1),    # car 1 (driver 2) on pole
            make_result(2, 44),   # car 44 (driver 1) P2
            make_result(3, 16),   # car 16 (driver 3) P3
        ]
        self.cfg = make_config()

    def test_correct(self):
        pts = score_quali_top3_slot(1, 2, self.results, self.round_drivers, self.cfg)
        assert pts == 5

    def test_one_off(self):
        pts = score_quali_top3_slot(1, 1, self.results, self.round_drivers, self.cfg)
        # Driver 1 actually P2, predicted P1 → off by 1
        assert pts == 2

    def test_two_off_returns_zero_for_top3(self):
        # Driver 3 actually P3, predicted P1 → off by 2 → 0 points
        pts = score_quali_top3_slot(1, 3, self.results, self.round_drivers, self.cfg)
        assert pts == 0


# =============================================================================
# score_pole_time — bucket-based proximity scoring
# =============================================================================


class TestScorePoleTime:
    def setup_method(self):
        self.cfg = make_config()
        # actual pole time = 1:23.456 = 83456 ms
        self.actual_ms = 83_456

    def test_within_inner_bucket_awards_max(self):
        # 0.1s off → in 0.2s bucket → 10 points
        pts = score_pole_time(self.actual_ms + 100, self.actual_ms, self.cfg)
        assert pts == 10

    def test_at_inner_bucket_boundary_awards_max(self):
        # exactly 0.2s off → in inner bucket → 10 points
        pts = score_pole_time(self.actual_ms + 200, self.actual_ms, self.cfg)
        assert pts == 10

    def test_outside_inner_inside_outer_awards_outer(self):
        # 0.5s off → outside 0.2s bucket, inside 1.0s bucket → 5 points
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
# score_fastest_lap
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
        # Predicted Hamilton (regular for car 44). Bottas drove car 44 and
        # set the fastest lap. User scores.
        results = [make_result(1, 44, actual_driver_id=50, is_fastest_lap=True)]
        pts = score_fastest_lap(1, results, self.round_drivers, self.cfg)
        assert pts == 10


# =============================================================================
# score_dnf_count
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
# Phase orchestrator: build_race_phase_scores
# =============================================================================


class TestRacePhaseOrchestration:
    def setup_method(self):
        # Map drivers 1..10 to car numbers 1..10 for simplicity
        self.round_drivers = make_round_drivers({i: i for i in range(1, 11)})
        self.cfg = make_config()

        # Race result: drivers 1..10 finish in order 1..10. Driver 5 has FL.
        # DNF count is 0 for clarity.
        self.race_results = [
            make_result(i, i, actual_driver_id=i, is_fastest_lap=(i == 5))
            for i in range(1, 11)
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
        # Attach results to the session manually since we're not committing.
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
        )
        # 10 top-10 + 1 fastest-lap + 1 DNF = 12 rows
        assert len(scores) == 12
        top10_total = sum(s.points for s in scores if s.kind == PredictionType.RACE_TOP10)
        assert top10_total == 100
        fl_total = sum(s.points for s in scores if s.kind == PredictionType.FASTEST_LAP)
        assert fl_total == 10
        dnf_total = sum(s.points for s in scores if s.kind == PredictionType.DNF_COUNT)
        assert dnf_total == 10

    def test_no_predictions_yields_zero_points_with_full_row_set(self):
        preds = UserPredictions()  # nothing submitted
        scores = build_race_phase_scores(
            user_id=1, round_id=1, user_preds=preds,
            race_session=self.race_session,
            round_drivers=self.round_drivers, config=self.cfg,
        )
        # Still emit 10 + 1 + 1 = 12 rows so the results UI renders cleanly.
        assert len(scores) == 12
        assert all(s.points == 0 for s in scores)

    def test_partial_predictions_score_what_was_submitted(self):
        preds = UserPredictions(
            top10=[Top10Prediction(user_id=1, round_id=1, position=1, predicted_driver_id=1)],
            # No fastest-lap or DNF prediction
        )
        scores = build_race_phase_scores(
            user_id=1, round_id=1, user_preds=preds,
            race_session=self.race_session,
            round_drivers=self.round_drivers, config=self.cfg,
        )
        assert len(scores) == 12
        # Position-1 prediction was correct → 10 points
        p1_score = next(s for s in scores
                        if s.kind == PredictionType.RACE_TOP10 and s.position == 1)
        assert p1_score.points == 10
        # Position-2 was not predicted → 0 points
        p2_score = next(s for s in scores
                        if s.kind == PredictionType.RACE_TOP10 and s.position == 2)
        assert p2_score.points == 0


# =============================================================================
# Phase orchestrator: build_quali_phase_scores
# =============================================================================


def test_quali_phase_perfect_score():
    round_drivers = make_round_drivers({1: 44, 2: 1, 3: 16})
    cfg = make_config()

    # Pole time 1:23.456
    quali_results = [
        make_result(1, 44, best_qualifying_time_ms=83_456),
        make_result(2, 1),
        make_result(3, 16),
    ]
    quali_session = Session(
        id=1, round_id=1,
        session_type=SessionType.QUALIFYING,
        pole_time_ms=83_456,
    )
    quali_session.results = quali_results

    preds = UserPredictions(
        quali_top3=[
            Top3QualiPrediction(user_id=1, round_id=1, position=1, predicted_driver_id=1),
            Top3QualiPrediction(user_id=1, round_id=1, position=2, predicted_driver_id=2),
            Top3QualiPrediction(user_id=1, round_id=1, position=3, predicted_driver_id=3),
        ],
        pole_time=PoleTimePrediction(user_id=1, round_id=1, predicted_time_ms=83_456),
    )
    scores = build_quali_phase_scores(
        user_id=1, round_id=1, user_preds=preds,
        quali_session=quali_session,
        round_drivers=round_drivers, config=cfg,
    )
    assert len(scores) == 4   # 3 top3 + 1 pole time
    total = sum(s.points for s in scores)
    assert total == 5 + 5 + 5 + 10   # all three slots correct + perfect pole time


# =============================================================================
# Phase orchestrator: build_sprint_phase_scores
# =============================================================================


def test_sprint_phase_perfect_score():
    round_drivers = make_round_drivers({1: 44, 2: 1, 3: 16})
    cfg = make_config()

    # Sprint race: car 44 wins, 1 second, 16 third.
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
    assert len(scores) == 3                     # 3 sprint top3 rows only
    total = sum(s.points for s in scores)
    assert total == 5 + 5 + 5


# =============================================================================
# Frozen config: changing defaults does NOT alter past-round scoring
# =============================================================================


def test_frozen_config_unaffected_by_default_changes(monkeypatch):
    """Mutating Config.SCORING_DEFAULTS must not change scores for a round
    whose RoundScoringConfig was already snapshotted with old values."""
    # Take a snapshot of the OLD defaults.
    old_cfg = make_config()

    # Mutate the global defaults to something different.
    monkeypatch.setitem(Config.SCORING_DEFAULTS, "race_top10_correct", 99)

    round_drivers = make_round_drivers({1: 44})
    results = [make_result(1, 44)]
    pts = score_top10_slot(1, 1, results, round_drivers, old_cfg)
    # The snapshot still uses the original value, not 99.
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
        )
