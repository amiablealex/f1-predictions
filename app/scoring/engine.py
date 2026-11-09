"""Scoring engine.

The engine is a set of pure functions that compute `PredictionScore` rows
from predictions, session results, and a frozen `RoundScoringConfig`. It
never writes to the database — that is the worker's responsibility. Each
phase orchestrator emits a complete set of score rows for a single user,
including 0-point rows for predictions the user did not submit. This is
deliberate: the round-results view always renders a row per slot, even
when the user didn't fill it in.

Substitution model:
  A pick is a seat — the `RoundDriver` row the picked driver is listed
  in. app/seats.py resolves each seat to the result of whoever drove it,
  matching stand-ins by team (car numbers follow drivers, so they can't
  identify a seat). If a reserve drove Hamilton's Ferrari and finished
  5th, a user who picked Hamilton at position 5 scores the full points.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from app.models.driver import RoundDriver
from app.models.prediction import (
    DnfCountPrediction,
    FastestLapPrediction,
    PlacesGainedPrediction,
    PoleTimePrediction,
    PredictionScore,
    PredictionType,
    QualiHeadToHeadPrediction,
    QualiNthPrediction,
    QualiRandomDriverPrediction,
    SpecialPrediction,
    Top3QualiPrediction,
    Top3SprintPrediction,
    Top10Prediction,
)
from app.models.result import SessionResult
from app.models.round import Round, RoundScoringConfig, ScoringPhase, Session, SessionType
from app.scoring.provisional import Withheld
from app.seats import resolve_seats, result_for_pick


# =============================================================================
# Container for one user's predictions for a round
# =============================================================================


@dataclass
class UserPredictions:
    """All of a user's predictions for one round.

    Any of these can be None / empty if the user didn't submit them. The
    engine treats missing predictions as 0-point rows.
    """
    top10: list[Top10Prediction] = field(default_factory=list)
    quali_top3: list[Top3QualiPrediction] = field(default_factory=list)
    sprint_top3: list[Top3SprintPrediction] = field(default_factory=list)
    pole_time: PoleTimePrediction | None = None
    fastest_lap: FastestLapPrediction | None = None
    dnf_count: DnfCountPrediction | None = None
    places_gained: PlacesGainedPrediction | None = None
    quali_random_driver: QualiRandomDriverPrediction | None = None
    qh2h: QualiHeadToHeadPrediction | None = None
    qnth: QualiNthPrediction | None = None
    # Keyed by special_key — only contains entries for specials the user
    # actually submitted predictions for.
    specials: dict[str, "SpecialPrediction"] = field(default_factory=dict)


# =============================================================================
# Substitution-aware result lookup
# =============================================================================


def _resolve_actual_result(
    predicted_driver_id: int,
    round_drivers: Iterable[RoundDriver],
    session_results: Iterable[SessionResult],
) -> SessionResult | None:
    """The result row for the seat the picked driver is listed in."""
    return result_for_pick(predicted_driver_id, round_drivers, session_results)


# =============================================================================
# Atomic scoring functions (return integer points)
# =============================================================================


def _score_position_delta(
    predicted_position: int,
    actual: SessionResult | None,
    correct: int,
    one_off: int,
    two_off: int,
) -> int:
    """Award points based on |predicted - actual_position|.

    DNF / unclassified or missing result → 0 points.
    """
    if actual is None or not actual.is_classified:
        return 0
    delta = abs(predicted_position - actual.position)
    if delta == 0:
        return correct
    if delta == 1:
        return one_off
    if delta == 2:
        return two_off
    return 0


def _score_quali_position_bucketed(
    predicted_position: int,
    actual_position: int,
    config: RoundScoringConfig,
) -> int:
    """Bucketed scoring for qualifying-style position predictions.

    Walks the configured buckets in order and returns the points of the
    first one whose `max_delta` covers the absolute difference. The last
    bucket should have a large `max_delta` to act as a catch-all.
    """
    delta = abs(predicted_position - actual_position)
    for bucket in config.quali_position_buckets:
        if delta <= int(bucket["max_delta"]):
            return int(bucket["points"])
    # Defensive fallthrough — buckets misconfigured.
    return 0


def score_top10_slot(
    predicted_position: int,
    predicted_driver_id: int,
    race_results: list[SessionResult],
    round_drivers: list[RoundDriver],
    config: RoundScoringConfig,
) -> int:
    actual = _resolve_actual_result(predicted_driver_id, round_drivers, race_results)
    return _score_position_delta(
        predicted_position, actual,
        correct=config.race_top10_correct,
        one_off=config.race_top10_one_off,
        two_off=config.race_top10_two_off,
    )


def score_quali_top3_slot(
    predicted_position: int,
    predicted_driver_id: int,
    quali_results: list[SessionResult],
    round_drivers: list[RoundDriver],
    config: RoundScoringConfig,
) -> int:
    """Bucketed scoring for a quali top-3 slot.

    If the predicted driver did not appear in qualifying (no matching car
    in the results — e.g. DNS), score 0.
    """
    actual = _resolve_actual_result(predicted_driver_id, round_drivers, quali_results)
    if actual is None:
        return 0
    return _score_quali_position_bucketed(
        predicted_position, actual.position, config,
    )


def score_quali_random_driver(
    predicted_position: int,
    random_driver_id: int,
    quali_results: list[SessionResult],
    round_drivers: list[RoundDriver],
    config: RoundScoringConfig,
) -> int:
    """Bucketed scoring for the per-round random-driver quali wager."""
    actual = _resolve_actual_result(random_driver_id, round_drivers, quali_results)
    if actual is None:
        return 0
    return _score_quali_position_bucketed(
        predicted_position, actual.position, config,
    )


def score_quali_h2h(
    predicted_driver_id: int,
    qh2h_driver_a: RoundDriver,
    qh2h_driver_b: RoundDriver,
    quali_results: list[SessionResult],
    config: RoundScoringConfig,
) -> int:
    """Binary scoring for the head-to-head wager.

    Compares the two pre-selected teammates' seats (substitution-aware).
    The user wins if they picked the higher-qualifying seat. DNQ handling:
    a seat with no quali result loses against one that set a time; if
    both DNQ'd, no scoring. The two seats are the team's whole lineup, so
    resolving them on their own is equivalent to resolving the full grid.
    """
    resolved = resolve_seats([qh2h_driver_a, qh2h_driver_b], quali_results)
    res_a = resolved.get(qh2h_driver_a)
    res_b = resolved.get(qh2h_driver_b)
    if res_a is None and res_b is None:
        return 0
    if res_a is None:
        winner_driver_id = qh2h_driver_b.expected_driver_id
    elif res_b is None:
        winner_driver_id = qh2h_driver_a.expected_driver_id
    elif res_a.position < res_b.position:
        winner_driver_id = qh2h_driver_a.expected_driver_id
    else:
        winner_driver_id = qh2h_driver_b.expected_driver_id
    return config.qh2h_correct if predicted_driver_id == winner_driver_id else 0


def score_quali_nth(
    predicted_driver_id: int,
    n: int,
    quali_results: list[SessionResult],
    round_drivers: list[RoundDriver],
    config: RoundScoringConfig,
) -> int:
    """Bucketed scoring on |actual position of picked driver - N|.

    Driver not in quali results (DNS) → 0 points.
    """
    actual = _resolve_actual_result(predicted_driver_id, round_drivers, quali_results)
    if actual is None:
        return 0
    return _score_quali_position_bucketed(n, actual.position, config)


def score_sprint_top3_slot(
    predicted_position: int,
    predicted_driver_id: int,
    sprint_results: list[SessionResult],
    round_drivers: list[RoundDriver],
    config: RoundScoringConfig,
) -> int:
    actual = _resolve_actual_result(predicted_driver_id, round_drivers, sprint_results)
    return _score_position_delta(
        predicted_position, actual,
        correct=config.sprint_top3_correct,
        one_off=config.sprint_top3_one_off,
        two_off=0,
    )


def score_pole_time(
    predicted_ms: int,
    actual_pole_ms: int | None,
    config: RoundScoringConfig,
) -> int:
    """Bucket-based scoring on absolute distance from the actual pole time.

    Buckets are stored most-precise-first; the first bucket the prediction
    falls inside wins.
    """
    if actual_pole_ms is None:
        return 0
    delta_ms = abs(predicted_ms - actual_pole_ms)
    for bucket in config.pole_time_buckets:
        threshold_ms = float(bucket["within_seconds"]) * 1000.0
        if delta_ms <= threshold_ms:
            return int(bucket["points"])
    return 0


def score_fastest_lap(
    predicted_driver_id: int,
    race_results: list[SessionResult],
    round_drivers: list[RoundDriver],
    config: RoundScoringConfig,
) -> int:
    """Substitution-aware fastest-lap scoring: did the picked seat set the
    fastest lap, whoever drove it."""
    actual = _resolve_actual_result(predicted_driver_id, round_drivers, race_results)
    if actual is not None and actual.is_fastest_lap:
        return config.fastest_lap_correct
    return 0


def score_dnf_count(
    predicted_count: int,
    actual_count: int | None,
    config: RoundScoringConfig,
) -> int:
    if actual_count is None:
        return 0
    delta = abs(predicted_count - actual_count)
    if delta == 0:
        return config.dnf_count_correct
    if delta == 1:
        return config.dnf_count_one_off
    return 0


# -----------------------------------------------------------------------------
# Places gained — grid_position - finish_position
# -----------------------------------------------------------------------------

_DNS_PATTERNS = ("did not start", "withdrew", "withdrawn", "dns")


def _is_did_not_start(status: str | None) -> bool:
    s = (status or "").strip().lower()
    return any(p in s for p in _DNS_PATTERNS)


def score_places_gained(
    predicted_driver_id: int,
    race_results: list[SessionResult],
    round_drivers: list[RoundDriver],
) -> int:
    """Award (grid - finish_position), uncapped.

    Uses Jolpica's assigned position regardless of finish status:
      - Classified finish → straight grid - finish.
      - DNF / DSQ / unclassified → grid - assigned_position. F1 ranks
        DNFs by laps completed (late-DNF outranks early-DNF), so a
        driver who got further into the race scores better than one
        who retired sooner. Front-runners who retire early get hit
        hardest; back-markers who retire late may score slightly
        positive. Accepted as the price of capturing completion.

    Edge cases:
      - DNS or never raced → 0 (driver never had a chance to gain or
        lose places). Detected via status string.
      - Driver not in this round's lineup → 0.
      - No result row for the car → 0 (effectively a DNS).
      - Pit lane start (grid 0) → treated as the last grid slot
        (= total race entries).
      - No grid_position recorded → 0 (defensive; the ingestion gate
        ensures this doesn't happen in practice).
    """
    actual = _resolve_actual_result(predicted_driver_id, round_drivers, race_results)
    if actual is None:
        return 0
    if actual.grid_position is None:
        return 0
    if _is_did_not_start(actual.status):
        return 0

    total_starters = len(race_results)
    grid = actual.grid_position if actual.grid_position != 0 else total_starters
    return grid - actual.position


# =============================================================================
# Phase orchestrators — emit a complete set of PredictionScore rows for ONE
# user. Missing predictions are filled in with 0-point rows so the
# round-results view always renders a row per slot.
# =============================================================================


def build_sprint_phase_scores(
    user_id: int,
    round_id: int,
    user_preds: UserPredictions,
    sprint_quali_session: Session | None,
    sprint_race_session: Session | None,
    round_drivers: list[RoundDriver],
    config: RoundScoringConfig,
) -> list[PredictionScore]:
    """SPRINT phase reveal: SR top 3 only.

    Sprint qualifying is deadline-only — Jolpica has no SQ endpoint, so we
    don't score it. The SQ session still exists in the data model purely
    to anchor the predictions deadline (1 hour before SQ starts).
    """
    scores: list[PredictionScore] = []
    sr_results = list(sprint_race_session.results) if sprint_race_session else []

    sprint_top3_by_pos = {p.position: p for p in user_preds.sprint_top3}
    for pos in (1, 2, 3):
        pred = sprint_top3_by_pos.get(pos)
        if pred is not None:
            pts = score_sprint_top3_slot(
                pos, pred.predicted_driver_id, sr_results, round_drivers, config,
            )
        else:
            pts = 0
        scores.append(PredictionScore(
            user_id=user_id, round_id=round_id,
            kind=PredictionType.SPRINT_TOP3, position=pos, points=pts,
        ))
    return scores


def build_quali_phase_scores(
    user_id: int,
    round_id: int,
    user_preds: UserPredictions,
    quali_session: Session,
    round_drivers: list[RoundDriver],
    config: RoundScoringConfig,
    round_obj: Round,
) -> list[PredictionScore]:
    """QUALI phase reveal: main quali top 3 + pole time + random-driver wager."""
    scores: list[PredictionScore] = []
    quali_results = list(quali_session.results)

    # ---- Quali top 3 (3 rows, bucketed scoring) ----
    quali_top3_by_pos = {p.position: p for p in user_preds.quali_top3}
    for pos in (1, 2, 3):
        pred = quali_top3_by_pos.get(pos)
        if pred is not None:
            pts = score_quali_top3_slot(
                pos, pred.predicted_driver_id, quali_results, round_drivers, config,
            )
        else:
            pts = 0
        scores.append(PredictionScore(
            user_id=user_id, round_id=round_id,
            kind=PredictionType.QUALI_TOP3, position=pos, points=pts,
        ))

    # ---- Pole time (single row) ----
    if user_preds.pole_time is not None:
        pts = score_pole_time(
            user_preds.pole_time.predicted_time_ms,
            quali_session.pole_time_ms,
            config,
        )
    else:
        pts = 0
    scores.append(PredictionScore(
        user_id=user_id, round_id=round_id,
        kind=PredictionType.POLE_TIME, position=None, points=pts,
    ))

    # ---- Random driver wager (single row) ----
    # Round.random_quali_driver is a RoundDriver (FK); we want its
    # expected_driver_id to feed into the substitution-aware lookup.
    random_driver = round_obj.random_quali_driver
    if user_preds.quali_random_driver is not None and random_driver is not None:
        pts = score_quali_random_driver(
            user_preds.quali_random_driver.predicted_position,
            random_driver.expected_driver_id,
            quali_results, round_drivers, config,
        )
    else:
        pts = 0
    scores.append(PredictionScore(
        user_id=user_id, round_id=round_id,
        kind=PredictionType.QUALI_RANDOM_DRIVER, position=None, points=pts,
    ))

    # ---- Quali head-to-head (single row) ----
    if (
        user_preds.qh2h is not None
        and round_obj.qh2h_driver_a is not None
        and round_obj.qh2h_driver_b is not None
    ):
        pts = score_quali_h2h(
            user_preds.qh2h.predicted_driver_id,
            round_obj.qh2h_driver_a, round_obj.qh2h_driver_b,
            quali_results, config,
        )
    else:
        pts = 0
    scores.append(PredictionScore(
        user_id=user_id, round_id=round_id,
        kind=PredictionType.QUALI_HEAD_TO_HEAD, position=None, points=pts,
    ))

    # ---- Quali Nth (single row) ----
    if user_preds.qnth is not None and round_obj.quali_nth_position is not None:
        pts = score_quali_nth(
            user_preds.qnth.predicted_driver_id,
            round_obj.quali_nth_position,
            quali_results, round_drivers, config,
        )
    else:
        pts = 0
    scores.append(PredictionScore(
        user_id=user_id, round_id=round_id,
        kind=PredictionType.QUALI_NTH, position=None, points=pts,
    ))
    return scores


def build_race_phase_scores(
    user_id: int,
    round_id: int,
    user_preds: UserPredictions,
    race_session: Session,
    round_drivers: list[RoundDriver],
    config: RoundScoringConfig,
    round_obj: "Round",
    special_outcomes_by_key: dict[str, "SpecialOutcome"],
) -> list[PredictionScore]:
    """RACE phase reveal: race top 10, fastest lap, DNF count, places gained, specials."""
    scores: list[PredictionScore] = []
    race_results = list(race_session.results)

    # ---- Race top 10 (10 rows) ----
    top10_by_pos = {p.position: p for p in user_preds.top10}
    for pos in range(1, 11):
        pred = top10_by_pos.get(pos)
        if pred is not None:
            pts = score_top10_slot(
                pos, pred.predicted_driver_id, race_results, round_drivers, config,
            )
        else:
            pts = 0
        scores.append(PredictionScore(
            user_id=user_id, round_id=round_id,
            kind=PredictionType.RACE_TOP10, position=pos, points=pts,
        ))

    # ---- Fastest lap (single row) ----
    if user_preds.fastest_lap is not None:
        pts = score_fastest_lap(
            user_preds.fastest_lap.predicted_driver_id,
            race_results, round_drivers, config,
        )
    else:
        pts = 0
    scores.append(PredictionScore(
        user_id=user_id, round_id=round_id,
        kind=PredictionType.FASTEST_LAP, position=None, points=pts,
    ))

    # ---- DNF count (single row) ----
    if user_preds.dnf_count is not None:
        pts = score_dnf_count(
            user_preds.dnf_count.predicted_count,
            race_session.dnf_count,
            config,
        )
    else:
        pts = 0
    scores.append(PredictionScore(
        user_id=user_id, round_id=round_id,
        kind=PredictionType.DNF_COUNT, position=None, points=pts,
    ))

    # ---- Places gained (single row) ----
    if user_preds.places_gained is not None:
        pts = score_places_gained(
            user_preds.places_gained.predicted_driver_id,
            race_results, round_drivers,
        )
    else:
        pts = 0
    scores.append(PredictionScore(
        user_id=user_id, round_id=round_id,
        kind=PredictionType.PLACES_GAINED, position=None, points=pts,
    ))

    # ---- Specials (two rows — one per active special on this round) ----
    from app.scoring.specials import score_special   # local import to avoid cycle
    for special_key in (round_obj.special_a_key, round_obj.special_b_key):
        if not special_key:
            continue
        prediction = user_preds.specials.get(special_key)
        outcome = special_outcomes_by_key.get(special_key)
        pts = score_special(prediction, outcome, config)
        scores.append(PredictionScore(
            user_id=user_id, round_id=round_id,
            kind=PredictionType.SPECIAL,
            position=None, special_key=special_key, points=pts,
        ))
    return scores


# =============================================================================
# Phase dispatch
# =============================================================================


def build_phase_scores(
    user_id: int,
    round_id: int,
    phase: ScoringPhase,
    user_preds: UserPredictions,
    sessions_by_type: dict[SessionType, Session],
    round_drivers: list[RoundDriver],
    config: RoundScoringConfig,
    round_obj: Round,
    special_outcomes_by_key: dict[str, "SpecialOutcome"] | None = None,
    withheld: "Withheld | None" = None,
) -> list[PredictionScore]:
    """Top-level dispatch — call from the worker.

    `withheld` drops rows for picks whose inputs are still missing from a
    provisional session, so they show as unscored rather than 0.
    """
    rows = _build_phase_scores(
        user_id, round_id, phase, user_preds, sessions_by_type,
        round_drivers, config, round_obj, special_outcomes_by_key or {},
    )
    if withheld:
        rows = [r for r in rows if not withheld.covers_score(r)]
    return rows


def _build_phase_scores(
    user_id: int,
    round_id: int,
    phase: ScoringPhase,
    user_preds: UserPredictions,
    sessions_by_type: dict[SessionType, Session],
    round_drivers: list[RoundDriver],
    config: RoundScoringConfig,
    round_obj: Round,
    special_outcomes_by_key: dict[str, "SpecialOutcome"],
) -> list[PredictionScore]:
    if phase == ScoringPhase.SPRINT:
        return build_sprint_phase_scores(
            user_id, round_id, user_preds,
            sprint_quali_session=sessions_by_type.get(SessionType.SPRINT_QUALI),
            sprint_race_session=sessions_by_type.get(SessionType.SPRINT_RACE),
            round_drivers=round_drivers,
            config=config,
        )
    if phase == ScoringPhase.QUALI:
        quali = sessions_by_type[SessionType.QUALIFYING]
        return build_quali_phase_scores(
            user_id, round_id, user_preds, quali, round_drivers, config, round_obj,
        )
    if phase == ScoringPhase.RACE:
        race = sessions_by_type[SessionType.RACE]
        return build_race_phase_scores(
            user_id, round_id, user_preds, race, round_drivers, config,
            round_obj, special_outcomes_by_key,
        )
    raise ValueError(f"Unknown scoring phase: {phase}")
