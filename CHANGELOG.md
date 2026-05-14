# Changelog

## [0.10.0] — 2026-05-14

### Added
- Quali head-to-head: each round picks two teammates; predict who qualifies higher.
- Qualify-Nth: each round picks a grid position; predict the driver who qualifies there.
- Specials rotation: two of eight specials drawn per round. Bank covers first retirement, most pit stops, last classified finisher, margin of victory, lap of first pit stop, pole sitter wins, longest stint, biggest team finishing-position gap.
- Pit-stop ingestion from Jolpica.
- `session_results.laps_completed` and `session_results.race_time_ms` columns supporting the new specials.
- Backfill CLI command (`flask backfill-phase4`) for populating round-level selections and outcomes on past rounds.

### Fixed
- Enum case mismatch in `prediction_type` for types added by Phase 3 and Phase 4 migrations.
- Jinja scope bug in round-view template's head-to-head actual rendering.

### Changed
- `RoundScoringConfig` extended with per-special configurable point values.
- Round-view and predictions form updated with new fields and result rows.
- Rules page extended with new prediction type explanations.


## 0.9.0 — 2026-05-09 (pre-release)

### Added
- "Places gained" race prediction: pick a driver, score = grid − finish.
- Per-round random driver quali wager.
- `/results` season list (your view + per-friend view).
- Friend-context navigation through round detail.
- Relative deadline phrasing ("Locks in 3 hours").
- Submitted/dirty-state badge on predictions form.
- `/health` endpoint with DB ping.
- Login rate limiting.
- Password complexity (digit required).

### Changed
- Qualifying scoring is now bucketed (exact +5 → 9+ off −5),
  applied to top-3 slots and the random driver wager.
- Results page lists locked rounds only, newest first.
- Round-detail prev/next arrows clamp to locked rounds.

### Removed
- `quali_top3_correct` and `quali_top3_one_off` config fields
  (replaced by `quali_position_buckets`).
