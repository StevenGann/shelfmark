"""In-memory, per-process fixed-window rate limiting.

The deployment is a single process by contract (the container runs one worker),
so an in-process map is the whole story; there is no shared store to consult.
"""

from __future__ import annotations

import threading
import time

_WINDOWS: dict[str, tuple[int, int]] = {}
_LOCK = threading.Lock()


def allow(bucket: str, limit: int) -> int:
    """Count one request for ``bucket``; return seconds to wait, or 0 when allowed.

    Each bucket is a 60-second fixed window. Buckets are dropped as their window
    rolls over, so the map stays small.
    """
    now = int(time.monotonic())
    with _LOCK:
        start, count = _WINDOWS.get(bucket, (now, 0))
        if now - start >= 60:
            _WINDOWS[bucket] = (now, 1)
            return 0
        if count >= limit:
            return 60 - (now - start)
        _WINDOWS[bucket] = (start, count + 1)
        return 0
