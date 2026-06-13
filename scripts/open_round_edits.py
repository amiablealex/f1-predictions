"""Temporarily open a past-deadline round for editing, then relock it.

A deliberate, supervised admin action — not a feature. Used for the rare
case of letting a player submit/edit predictions after a round's deadline.

It works by pushing the round's predictions_deadline forward (which makes
get_current_round serve it on /predictions, and stops the deadline-lock
worker relocking it) and unlocking it. On --close it restores the exact
original deadline and relocks. The original is remembered in a local state
file so you never have to copy timestamps by hand.

ALWAYS prints the round's current deadline + lock state before writing, so
you can confirm which database you're pointed at. --open additionally
requires --confirm so the DB check is a deliberate two-step.

Usage (run from repo root):

  # See a round's state (no change):
  DATABASE_URL="<url>" python scripts/open_round_edits.py --round 7

  # Open R7 for 60 minutes (run once to inspect, again with --confirm):
  DATABASE_URL="<url>" python scripts/open_round_edits.py --round 7 --minutes 60 --open
  DATABASE_URL="<url>" python scripts/open_round_edits.py --round 7 --minutes 60 --open --confirm

  # Relock R7 and restore its real deadline when the user's done:
  DATABASE_URL="<url>" python scripts/open_round_edits.py --round 7 --close
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure repo root is importable when run as `python scripts/...`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.extensions import db
from app.models.round import Round

STATE_FILE = Path(__file__).resolve().parent / ".edit_state.json"


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _fmt(dt) -> str:
    return dt.isoformat() if dt else "(none)"


def _db_host() -> str:
    """Best-effort host extraction from DATABASE_URL for the confirmation
    line. Never prints credentials — just the host[:port]/dbname tail."""
    url = os.environ.get("DATABASE_URL", "(from .env / default)")
    if "@" in url:
        return url.split("@", 1)[1]
    return url


def main() -> int:
    ap = argparse.ArgumentParser(description="Open/close a round for post-deadline edits.")
    ap.add_argument("--round", type=int, required=True, help="Round number.")
    ap.add_argument("--minutes", type=int, default=60, help="Grace window length for --open (default 60).")
    ap.add_argument("--open", action="store_true", help="Open the round for editing.")
    ap.add_argument("--close", action="store_true", help="Relock and restore the round's real deadline.")
    ap.add_argument("--confirm", action="store_true", help="Required to actually apply --open.")
    args = ap.parse_args()

    if args.open and args.close:
        print("ERROR: pass only one of --open / --close.")
        return 2

    app = create_app()
    with app.app_context():
        season = app.config["F1_SEASON"]
        rd = (
            db.session.query(Round)
            .filter_by(season=season, round_number=args.round)
            .one_or_none()
        )
        if rd is None:
            print(f"ERROR: no round {args.round} in season {season}.")
            return 1

        now = datetime.now(timezone.utc)
        # ---- Always show current state first (DB-target confirmation). ----
        print("=" * 60)
        print(f"Database URL host : {_db_host()}")
        print(f"Season / round    : {season} / R{rd.round_number}  ({rd.gp_name})")
        print(f"Current deadline  : {_fmt(rd.predictions_deadline)}")
        print(f"Currently locked  : {rd.predictions_locked}")
        print(f"Server time (UTC) : {now.isoformat()}")
        print("=" * 60)

        if not args.open and not args.close:
            print("Inspection only — no flag given. Nothing changed.")
            return 0

        if args.open:
            if not args.confirm:
                print(f"\nWould OPEN R{rd.round_number} for {args.minutes} min "
                      f"(deadline -> {_fmt(now + timedelta(minutes=args.minutes))}, unlock).")
                print("Re-run with --confirm to apply. Check the DB host above is correct first.")
                return 0
            state = _load_state()
            key = f"{season}:{rd.round_number}"
            # Only store the original the FIRST time we open, so running
            # --open twice doesn't overwrite it with an already-pushed value.
            if key not in state:
                state[key] = {
                    "deadline": _fmt(rd.predictions_deadline),
                    "locked": rd.predictions_locked,
                }
                _save_state(state)
            rd.predictions_deadline = now + timedelta(minutes=args.minutes)
            rd.predictions_locked = False
            db.session.commit()
            print(f"\nOPENED. Deadline pushed to {_fmt(rd.predictions_deadline)}, unlocked.")
            print("Tell the player to go to the Predictions tab and submit.")
            print(f"When done: python scripts/open_round_edits.py --round {rd.round_number} --close")
            return 0

        if args.close:
            state = _load_state()
            key = f"{season}:{rd.round_number}"
            saved = state.get(key)
            if saved is None:
                print("\nWARNING: no saved original deadline for this round.")
                print("Relocking, but NOT changing the deadline (it may still be pushed forward).")
                rd.predictions_locked = True
                db.session.commit()
                print("Relocked. Verify the deadline above is the real one; fix by hand if not.")
                return 0
            original = saved["deadline"]
            rd.predictions_deadline = (
                datetime.fromisoformat(original) if original != "(none)" else None
            )
            rd.predictions_locked = True
            db.session.commit()
            del state[key]
            _save_state(state)
            print(f"\nCLOSED. Deadline restored to {_fmt(rd.predictions_deadline)}, relocked.")
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
