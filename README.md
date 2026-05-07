# F1 Predictions

A small Flask app for a friend-group F1 predictions league. You pick the top 10, top 3 in qualifying, pole time, fastest lap, and number of DNFs. The app pulls results from the Jolpica F1 API, scores everyone, and runs leaderboards.

Built for Railway + Postgres in production, runs the same on a Raspberry Pi locally.

## What it does

- Predict each round before the deadline (1 hour before the first scoring session)
- Sprint weekends get extra prediction items (sprint pole, sprint top 3)
- Results fetch automatically from Jolpica after each session ends
- Points reveal in phases — sprint, qualifying, race
- League leaderboards: total points or head-to-head (1 point per round won)
- Substitutions handled by seat: pick "Hamilton", points go to whoever drives car 44

## Architecture

Two services. The web (Flask + gunicorn) serves the UI. The worker (APScheduler) runs scheduled jobs — schedule sync, results polling, deadline lock, scoring trigger. Both share one Postgres database.

```
[ web service ]   [ worker service ]
       \                /
        \              /
         [ Postgres ]
              |
       (read-only)
              |
       [ Jolpica F1 API ]
```

## Local development (Raspberry Pi or any machine with Postgres)

Requires Python 3.12 and a running Postgres instance.

```bash
# Clone, create a venv, install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create a local database
sudo -u postgres createuser f1user --pwprompt        # password: f1pass
sudo -u postgres createdb -O f1user f1predictions
sudo -u postgres createdb -O f1user f1predictions_test  # for tests

# Configure
cp .env.example .env
# Edit .env: at minimum, set SECRET_KEY to something long and random.
# RESEND_API_KEY can stay blank — without it, password reset links print to the console.

# First-time DB setup (creates migrations/ and applies the initial schema)
flask --app wsgi db init
flask --app wsgi db migrate -m "initial"
flask --app wsgi db upgrade

# Run web + worker in two terminals
flask --app wsgi run --port 5000
python -m worker.scheduler
```

App at http://localhost:5000.

## Tests

```bash
pytest
```

Tests use a separate database (`f1predictions_test` by default; override with `TEST_DATABASE_URL`). Each test truncates rows for isolation, so the suite is safe to run against a real Postgres instance.

## Railway deployment

1. Push the repo to GitHub.
2. Create a Railway project. Add the **Postgres** plugin — Railway will inject `DATABASE_URL` automatically.
3. Create a **web** service from the repo. Start command:
   ```
   gunicorn wsgi:app --workers 2 --timeout 60 --bind 0.0.0.0:$PORT
   ```
4. Create a **worker** service from the same repo. Start command:
   ```
   python -m worker.scheduler
   ```
5. Set environment variables on **both** services (they share config):
   - `SECRET_KEY` — long random string
   - `FLASK_ENV=production`
   - `RESEND_API_KEY`, `RESEND_FROM_EMAIL`, `RESEND_FROM_NAME` — for password reset email
   - `APP_BASE_URL` — your production URL (e.g. `https://predictions.example.com`)
   - `F1_SEASON` — defaults to 2026; set explicitly if you want
6. The `release: flask db upgrade` line in the Procfile applies migrations on every deploy. The first deploy will create the schema.

## Bootstrap an admin user

The first user has to be promoted to admin manually. Two options:

**Option A — set bootstrap env vars and they're applied on next migration.** Not implemented yet; for now use option B.

**Option B — register through the UI, then promote yourself with one SQL statement.** From the Railway Postgres shell:
```sql
UPDATE users SET is_admin = true WHERE email = 'you@example.com';
```

Now `/admin/` is reachable. From there you can trigger the schedule sync, force results re-fetches, lock or unlock predictions, and issue password reset links.

## Customising

Almost everything tweakable lives in `app/config.py` under `Config`:

| Setting | What it does |
|---|---|
| `SCORING_DEFAULTS` | Points for every prediction type. Snapshotted per-round, so changing values affects future rounds only. |
| `DEADLINE_OFFSET_MINUTES` | How long before the first session predictions lock. |
| `RESULTS_POLL_INTERVAL_MINUTES` | How often the worker checks Jolpica for pending results. |
| `SCHEDULE_SYNC_INTERVAL_HOURS` | How often the worker re-pulls the season schedule. |
| `SESSION_DURATION_MINUTES` | Estimated session lengths used for state transitions. |
| `PALETTE` | Hex colours for backgrounds, text, points pills, status indicators. |
| `INVITE_CODE_LENGTH`, `INVITE_CODE_ALPHABET` | League invite codes. |

Anything coloured: edit `PALETTE` in `config.py`. The variables flow through to the templates via CSS custom properties — no other file needs updating.

## Troubleshooting

**Worker isn't picking up results.** Check the worker logs in Railway. The most common cause is Jolpica not having published results yet (you'll see `JolpicaNotFoundError` in the logs); it'll retry on the next poll. If the session has been pending more than `RESULTS_PENDING_TIMEOUT_HOURS`, the worker logs a warning so you can investigate.

**Predictions deadline is wrong.** The deadline is computed from the first scoring session's `scheduled_start` minus `DEADLINE_OFFSET_MINUTES`. If Jolpica's schedule was wrong at the time of the last sync, run the schedule sync again from `/admin/`. If that still doesn't fix it, edit the session's `scheduled_start` in the DB directly — admin overrides survive subsequent syncs as long as the session has progressed past `upcoming`.

**Driver lineup is wrong.** RoundDriver mappings get refreshed automatically once the round's first session produces results. Before that they're seeded from the previous round, which can be stale. Edit the rows directly if you need to fix them before the round starts.

**Lost password.** Use the reset flow if Resend is configured. If not, ask an admin — `/admin/` has an "issue reset" button per user that prints the URL to the admin's flash message.

**Local Postgres connection refused.** On a Pi, Postgres binds to `localhost` only by default. Make sure you're connecting via `localhost:5432` and that the user has a password set (`ALTER USER f1user WITH PASSWORD 'f1pass'`).

## Files worth knowing about

```
app/
├── config.py           # All tweakable values
├── models/             # SQLAlchemy models — the data shape
├── api/jolpica.py      # API client + typed dataclasses
├── scoring/engine.py   # Pure-function scoring (well-tested)
├── auth/               # Sign in, register, password reset
├── predictions/        # The form
├── rounds/             # Read-only round views (own + friend)
├── leagues/            # Create, join, manage
├── leaderboard/        # Total points + head-to-head
├── admin/              # Override surface for the deployer
├── templates/          # Jinja templates
└── static/css/main.css # Design system
worker/
├── scheduler.py        # APScheduler entry point
├── jobs.py             # Scheduled task functions
└── ingest.py           # Idempotent DB-write helpers
```

## License

Use it however you like. No warranty.
