"""In-memory IP-based rate limiting for login attempts.

Per-process counters. Fine for a single-worker friend-group app; swap
for Redis if we ever go multi-worker.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

WINDOW_SECONDS = 15 * 60
MAX_ATTEMPTS = 10

_lock = threading.Lock()
_attempts: dict[str, deque[float]] = defaultdict(deque)
_log = logging.getLogger(__name__)


def _prune(dq: deque[float], now: float) -> None:
    cutoff = now - WINDOW_SECONDS
    while dq and dq[0] < cutoff:
        dq.popleft()


def is_blocked(ip: str) -> bool:
    now = time.monotonic()
    with _lock:
        dq = _attempts[ip]
        _prune(dq, now)
        return len(dq) >= MAX_ATTEMPTS


def record_failure(ip: str) -> None:
    now = time.monotonic()
    with _lock:
        dq = _attempts[ip]
        _prune(dq, now)
        dq.append(now)
        if len(dq) >= MAX_ATTEMPTS:
            _log.warning("Login blocked for IP %s (%d attempts)", ip, len(dq))


def reset(ip: str) -> None:
    with _lock:
        _attempts.pop(ip, None)


def retry_after_seconds(ip: str) -> int:
    now = time.monotonic()
    with _lock:
        dq = _attempts[ip]
        _prune(dq, now)
        if not dq:
            return 0
        return max(0, int(WINDOW_SECONDS - (now - dq[0])))
