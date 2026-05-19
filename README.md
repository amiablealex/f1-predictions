# F1 Predictions

A small Flask app for a friend-group F1 predictions league. Predict the top 10, top 3 in qualifying, pole time, fastest lap, DNF count, plus rotating per-round bonuses that change every weekend. Results pulled from the [Jolpica F1 API](https://github.com/jolpica/jolpica-f1), scoring runs automatically.

**Live at [f1.kitsniff.com](https://f1.kitsniff.com)** · [About](https://f1.kitsniff.com/rules/about) · [Privacy](https://f1.kitsniff.com/rules/privacy) · [Changelog](./CHANGELOG.md)

<img width="2000" height="676" alt="image" src="https://github.com/user-attachments/assets/e89e98c1-514f-44ae-b991-d0bde031f38b" />

## What it does

- Predict each round before the deadline (1 hour before the first scoring session)
- Sprint weekends add sprint top 3
- Per-round rotating bonuses keep every weekend different: a random driver wager, a head-to-head between two teammates, "who qualifies P-N?", plus two from a rotating bank of eight specials (first retirement, most pit stops, margin of victory, longest stint, and more)
- Points reveal in phases — sprint, qualifying, race
- League leaderboards: total points or head-to-head
- Substitutions handled by seat: pick "Hamilton", points go to whoever drives car 44
- Installable as a PWA on mobile

## Stack

| Layer | Tool |
|---|---|
| Language | Python 3.11+ |
| Web framework | Flask + Flask-Login + Flask-WTF |
| ORM | SQLAlchemy 2.x + Flask-Migrate (Alembic) |
| Database | Postgres |
| Templating | Jinja2 |
| Frontend | Hand-written CSS, vanilla JS, HTMX where useful (no framework) |
| Server | gunicorn |
| Scheduler | APScheduler (separate worker process) |
| Data source | [Jolpica F1 API](https://github.com/jolpica/jolpica-f1) (Ergast successor) |
| Email | [Resend](https://resend.com) — password reset only |
| Hosting | [Railway](https://railway.app) (web + worker + Postgres) |
| Local dev | Raspberry Pi 4 with local Postgres |

Two services share one database. The **web** service serves the UI via gunicorn. The **worker** service runs scheduled jobs — schedule sync, results polling, deadline lock, scoring — via APScheduler. Both read from the same Postgres instance. Scoring is a pure-function engine with dense unit-test coverage.

## Run it locally

Requires Python 3.11+ and Postgres.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create databases
sudo -u postgres createuser f1user --pwprompt
sudo -u postgres createdb -O f1user f1predictions
sudo -u postgres createdb -O f1user f1predictions_test

# Configure
cp .env.example .env       # set SECRET_KEY at minimum

# Apply migrations
flask --app wsgi db upgrade

# Run web + worker in two terminals
flask --app wsgi run --port 5000
python -m worker.scheduler
```

Tests: `pytest`.

## Customising

Almost everything tweakable lives in `app/config.py` under `Config`:

| Setting | What it does |
|---|---|
| `SCORING_DEFAULTS` | Points for every prediction type, including all eight specials. Snapshotted into `RoundScoringConfig` when a round is created, so changing values affects future rounds only — past leaderboards stay frozen. |
| `DEADLINE_OFFSET_MINUTES` | How many minutes before the first scoring session predictions lock. Default 60. |
| `RESULTS_POLL_INTERVAL_MINUTES` | How often the worker checks Jolpica for pending results. Default 5. |
| `SCHEDULE_SYNC_INTERVAL_HOURS` | How often the worker re-pulls the season schedule. Default 12. |
| `RESULTS_PENDING_TIMEOUT_HOURS` | After how long a still-pending session logs a warning for admin attention. Default 6. |
| `SESSION_DURATION_MINUTES` | Estimated session lengths used to transition sessions from `in_progress` to `pending_results`. |
| `JOLPICA_BASE_URL` | Override the Jolpica endpoint if mirroring or testing against a different instance. |
| `JOLPICA_MIN_REQUEST_INTERVAL_SECONDS` | Client-side rate limit between API calls. Default 0.3s (well under Jolpica's 4 req/s ceiling). |
| `JOLPICA_REQUEST_TIMEOUT_SECONDS` | HTTP timeout per request. Default 15. |
| `F1_SEASON` | Year the app currently scores. Pulled from env var; defaults to 2026. |
| `TIMEZONE` | Display timezone for deadlines and session start times. Default `Europe/London`. |
| `PASSWORD_RESET_TOKEN_TTL_HOURS` | How long password reset links stay valid. Default 2. |
| `INVITE_CODE_LENGTH` | Length of league invite codes. Default 6. |
| `INVITE_CODE_ALPHABET` | Character set for invite codes. Excludes ambiguous characters (`0`, `O`, `1`, `I`). |
| `LEADERBOARD_PAGE_SIZE` | Hard cap on leaderboard entries per page. Default 100. |
| `PALETTE` | Hex colours for backgrounds, text, points pills, status indicators. Flows through to templates via CSS custom properties — edit here, no other file needs updating. |

The specials bank is defined in `app/specials.py`. To add or remove a special, edit that file plus the corresponding entry in `SCORING_DEFAULTS` and a column on `RoundScoringConfig`.

## License

[MIT](./LICENSE).
