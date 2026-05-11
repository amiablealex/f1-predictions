"""Specials catalogue.

Defines the bank of 8 specials used by the RACE-phase rotation. Each
entry has:

  - key: stable identifier (matches Round.special_a_key / special_b_key,
    SpecialPrediction.special_key, SpecialOutcome.special_key,
    PredictionScore.special_key)
  - label: short human-readable name shown on the prediction form
  - description: brief explanation shown alongside the input
  - input_type: 'driver_pick' | 'team_pick' | 'int' | 'bool' — which
    column on SpecialPrediction holds the user's value
  - scoring_config_attr: attribute on RoundScoringConfig holding the
    points value (read via getattr by the engine)
  - scoring_tolerance: for numeric specials, the ± tolerance window
    (None for exact match or non-numeric specials)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpecialDef:
    key: str
    label: str
    description: str
    input_type: str
    scoring_config_attr: str
    scoring_tolerance: int | None = None


SPECIALS: list[SpecialDef] = [
    SpecialDef(
        key="first_retirement",
        label="First driver to retire",
        description="No score if no DNFs. Ties on retirement lap: all picks score.",
        input_type="driver_pick",
        scoring_config_attr="special_first_retirement",
    ),
    SpecialDef(
        key="most_pitstops",
        label="Most pit stops by any driver",
        description="Exact number. Ties: all picks of any tied driver score.",
        input_type="int",
        scoring_config_attr="special_most_pitstops",
    ),
    SpecialDef(
        key="last_classified",
        label="Last classified finisher",
        description="Lowest position with a classified status.",
        input_type="driver_pick",
        scoring_config_attr="special_last_classified",
    ),
    SpecialDef(
        key="margin_of_victory",
        label="Margin of victory (seconds)",
        description="Whole seconds. Within 5s of actual scores.",
        input_type="int",
        scoring_config_attr="special_margin_of_victory",
        scoring_tolerance=5,
    ),
    SpecialDef(
        key="lap_of_first_pitstop",
        label="Lap of the first pit stop of the race",
        description="Whole lap. Within 2 laps of actual scores.",
        input_type="int",
        scoring_config_attr="special_lap_of_first_pitstop",
        scoring_tolerance=2,
    ),
    SpecialDef(
        key="pole_sitter_wins",
        label="Will the pole sitter win the race?",
        description="Yes or no.",
        input_type="bool",
        scoring_config_attr="special_pole_sitter_wins",
    ),
    SpecialDef(
        key="longest_stint",
        label="Longest stint by any driver (laps)",
        description="Within 5 laps of actual scores.",
        input_type="int",
        scoring_config_attr="special_longest_stint",
        scoring_tolerance=5,
    ),
    SpecialDef(
        key="biggest_team_gap",
        label="Team with biggest finishing-position gap between their drivers",
        description="Only counts teams where both drivers finish (classified).",
        input_type="team_pick",
        scoring_config_attr="special_biggest_team_gap",
    ),
]


SPECIALS_BY_KEY: dict[str, SpecialDef] = {s.key: s for s in SPECIALS}
SPECIAL_KEYS: list[str] = [s.key for s in SPECIALS]
