# Changelog

## [Unreleased]

### Added
- Provisional results. Jolpica publishes in stages (finishing order first, grid, fastest lap, pit stops and quali times later), so any non-empty result set is now ingested and scored straight away. A session missing inputs goes `provisional`: picks that depend on the missing data are withheld (shown unscored, not 0) and the worker keeps polling, re-scoring only when the data changes. It completes once the data is whole, or 48 h after session start (`RESULTS_FINALISE_AFTER_HOURS`). `PROVISIONAL_RESULTS_ENABLED=false` restores the old gate.
- Qualifying is checked for a pole time, and an admin can enter one manually when Jolpica has no quali times. A published time replaces it on re-fetch.
- Admin round page (`/admin/round/<id>`) with session status, missing inputs, Refresh, Finalise and the pole-time form. The dashboard lists rounds with a "Needs attention" section; worker triggers are collapsed.
- Leaderboard note when totals include provisional points.
- Admin lineup editor on the round page: relist a seat under a different driver for this round or for the rest of the season. Picks follow the seat. An "Add driver" form creates a debutant Jolpica doesn't know yet; their Jolpica record claims it on first appearance (matched on family name). Locked rounds show who drove each seat.
- `flask repair-seats`: one-off repair for lineups rewritten from results. Restores each played round's lineup from the first unplayed round, re-fetches results, recomputes specials and re-scores. Reports changed result rows, pit stops and special outcomes per round, and point changes by prediction type and by user. `--rounds` limits it to given rounds. Dry run unless `--apply`.
- Round page footnote lists the round's stand-ins ("TSU for LAW (RB F1 Team)").

### Fixed
- Substitutions. Picks are seats, and a seat is now matched to whoever drove it by team (`app/seats.py`) instead of by car number, which follows the driver in F1. A pick of an ill or replaced driver scores their stand-in; a promotion between teams (RB driver to Red Bull, reserve into RB) resolves within each team.
- The worker no longer rewrites a round's lineup from its first results. It added stand-ins as extra seats after the deadline and overwrote a promoted driver's team.
- A debutant's results are no longer skipped: drivers missing from the master list are created from the result rows.
- A Jolpica result set with a repeated position (served mid-edit, as 2026 R19 quali was) is treated as transient instead of crashing the results poll on the position constraint. Stored rows are kept.
- Longest-stint special read a stand-in's pit stops under the regular's id; biggest-team-gap ignored stand-ins.

### Changed
- `session_results.constructor_name` added; `uq_round_driver` is deferrable so seats can swap drivers in one save.
- Rules page: substitution rule reworded (no "car 44").
- Round page status banners shortened and reworded for players: "Session live · points follow the result", "Awaiting official results", "Provisional points · may change".
- Session pill reads "awaiting results" instead of "results pending"; new "provisional" pill and round-list status.
- Race specials are computed from freshly queried results and pit stops rather than relationship collections.
- Worker runs as two Railway cron services (`worker.run_once frequent` every 5 min, `worker.run_once sync` every 12 h) instead of a long-running APScheduler process. Deadline lock and results polling can lag by a few minutes.
- Web service runs one gunicorn worker with four threads and sleeps when idle.
- Migrations run as a Railway pre-deploy command instead of in the web start command.
- Builds use Railpack; Python pinned via `.python-version`.

### Added
- `worker/run_once.py`: single-pass worker entry point for cron.

### Removed
- `Procfile` and `railway.toml`. Railway Config as Code is deprecated; service settings live in the dashboard.

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
