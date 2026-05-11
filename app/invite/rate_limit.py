"""In-memory IP-based rate limiting for invite-link lookups.

Per-process counters; sufficient for low-to-moderate scale. The
public surface is small enough that single-process tracking is
acceptable until we cross a multi-worker production scale threshold,
at which point swap for a shared store (Redis).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

WINDOW_SECONDS = 60
MAX_REQUESTS = 30

_lock = threading.Lock()
_requests: dict[str, deque[float]] = defaultdict(deque)
_log = logging.getLogger(__name__)


def is_rate_limited(ip: str) -> bool:
    """Record a request for this IP and return True if it should be blocked."""
    now = time.monotonic()
    with _lock:
        dq = _requests[ip]
        cutoff = now - WINDOW_SECONDS
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= MAX_REQUESTS:
            _log.warning(
                "Invite lookup rate-limited for IP %s (%d in window)",
                ip, len(dq),
            )
            return True
        dq.append(now)
        return False
