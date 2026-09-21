"""Run-once worker entry point for Railway cron services.

Usage:
    python -m worker.run_once frequent   # session state, deadline lock, results poll
    python -m worker.run_once sync       # schedule + driver master sync

Railway cron runs the start command on a schedule and expects the process to
exit when done; an execution still running blocks the next one. So: run the
group once, release DB connections, exit. Non-zero exit if any job failed so
failed runs are visible in the Railway dashboard.

`worker.scheduler` remains the long-running entry point for local use.
"""
from __future__ import annotations

import logging
import sys

from app import create_app
from app.api.jolpica import build_default_client
from app.extensions import db
from worker.jobs import (
    deadline_lock_job,
    driver_master_sync_job,
    results_poll_job,
    schedule_sync_job,
    session_state_transitions_job,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("worker")

# Order matters: state transitions first so results_poll sees fresh states.
GROUPS = {
    "frequent": [
        ("session_state", session_state_transitions_job),
        ("deadline_lock", deadline_lock_job),
        ("results_poll", results_poll_job),
    ],
    "sync": [
        ("schedule_sync", schedule_sync_job),
        ("driver_master_sync", driver_master_sync_job),
    ],
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in GROUPS:
        log.error("usage: python -m worker.run_once {%s}", "|".join(GROUPS))
        return 2

    group = argv[1]
    app = create_app()
    client = build_default_client(app.config)

    failures = 0
    for name, job in GROUPS[group]:
        try:
            job(app, client)
            log.info("job %s ok", name)
        except Exception:
            failures += 1
            log.exception("job %s failed", name)

    # Leave no open connections behind (Railway cron requirement).
    with app.app_context():
        db.session.remove()
        db.engine.dispose()

    log.info("run_once %s finished: %d failure(s)", group, failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
