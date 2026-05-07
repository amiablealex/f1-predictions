"""Worker entry point.

Run with `python -m worker.scheduler`. On Railway this is a separate
service from the web; locally on the Pi it can be run alongside Flask.

Architecture:
  - One Flask `app` is created (without serving HTTP) so that jobs can run
    inside `app.app_context()` and use SQLAlchemy via `from app.extensions
    import db`.
  - APScheduler's `BlockingScheduler` runs in the foreground; the
    process exits cleanly on SIGINT / SIGTERM.
  - Each scheduled job is wrapped in `_with_app_context` so failures in
    one job don't poison subsequent runs.
"""
from __future__ import annotations

import logging
import signal
import sys
from datetime import datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app import create_app
from app.api.jolpica import build_default_client
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


def _safe_run(job_name: str, func, *args, **kwargs):
    """Run a job, swallow exceptions so the scheduler keeps ticking."""
    try:
        func(*args, **kwargs)
    except Exception:  # pragma: no cover  (defensive)
        log.exception("Job %s raised; scheduler will continue", job_name)


def main() -> int:
    app = create_app()
    client = build_default_client(app.config)

    sched = BlockingScheduler(timezone=app.config["TIMEZONE"])

    # Quick first-pass on startup so the worker doesn't sit idle for hours
    # before its first run on a fresh deploy.
    soon = datetime.now() + timedelta(seconds=10)

    sched.add_job(
        lambda: _safe_run("schedule_sync", schedule_sync_job, app, client),
        trigger=IntervalTrigger(hours=app.config["SCHEDULE_SYNC_INTERVAL_HOURS"]),
        id="schedule_sync",
        next_run_time=soon,
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        lambda: _safe_run("driver_master_sync", driver_master_sync_job, app, client),
        trigger=IntervalTrigger(hours=24),
        id="driver_master_sync",
        next_run_time=soon + timedelta(seconds=20),
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        lambda: _safe_run("session_state", session_state_transitions_job, app, client),
        trigger=IntervalTrigger(minutes=1),
        id="session_state",
        next_run_time=soon,
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        lambda: _safe_run("results_poll", results_poll_job, app, client),
        trigger=IntervalTrigger(minutes=app.config["RESULTS_POLL_INTERVAL_MINUTES"]),
        id="results_poll",
        next_run_time=soon + timedelta(seconds=30),
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        lambda: _safe_run("deadline_lock", deadline_lock_job, app, client),
        trigger=IntervalTrigger(minutes=1),
        id="deadline_lock",
        next_run_time=soon,
        max_instances=1,
        coalesce=True,
    )

    def _shutdown(signum, frame):
        log.info("worker: signal %s received, shutting down", signum)
        sched.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info(
        "worker: scheduler starting (timezone=%s, results_poll=%dm, schedule_sync=%dh)",
        app.config["TIMEZONE"],
        app.config["RESULTS_POLL_INTERVAL_MINUTES"],
        app.config["SCHEDULE_SYNC_INTERVAL_HOURS"],
    )
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
