# Changelog

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
