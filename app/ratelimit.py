"""In-memory, per-process fixed-window rate limiting.

The deployment is a single process by contract (the container runs one worker),
so an in-process map is the whole story; there is no shared store to consult.
"""

from __future__ import annotations

import threading
import time

_WINDOWS: dict[str, tuple[int, int]] = {}
_LOCK = threading.Lock()


def allow(bucket: str, limit: int, window: int = 60) -> int:
    """Count one request for ``bucket``; return seconds to wait, or 0 when allowed.

    Each bucket is a fixed window of ``window`` seconds. Buckets are dropped as
    their window rolls over, so the map stays small.
    """
    now = int(time.monotonic())
    with _LOCK:
        start, count = _WINDOWS.get(bucket, (now, 0))
        if now - start >= window:
            _WINDOWS[bucket] = (now, 1)
            return 0
        if count >= limit:
            return window - (now - start)
        _WINDOWS[bucket] = (start, count + 1)
        return 0


def reset(bucket: str) -> None:
    """Forget a bucket's count.

    A successful login clears its failure counter, so a person who mistypes a
    few times and then gets it right is not locked out for the rest of the
    window. An attacker never reaches this path.
    """
    with _LOCK:
        _WINDOWS.pop(bucket, None)


def reset_all() -> None:
    """Forget every bucket.

    The window map is process-global, so a test suite that logs in on every case
    would otherwise exhaust the shared IP bucket and start failing on 429 for
    reasons unrelated to what it asserts.
    """
    with _LOCK:
        _WINDOWS.clear()
