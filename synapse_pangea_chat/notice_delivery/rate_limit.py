"""Per-key sliding-window rate limiter for the unauthenticated notice links."""

from __future__ import annotations

import time
from typing import Dict, List


class AdminRateLimiter:
    """Token bucket independent of direct push and public link traffic."""

    def __init__(self, per_minute: int, burst: int):
        self._rate = per_minute / 60
        self._burst = burst
        self._buckets: Dict[str, tuple[float, float]] = {}

    def is_rate_limited(self, key: str) -> bool:
        now = time.monotonic()
        # Idle callers regain a full bucket, so their records can be evicted.
        self._buckets = {
            k: v
            for k, v in self._buckets.items()
            if now - v[1] < self._burst / self._rate
        }
        tokens, last = self._buckets.get(key, (float(self._burst), now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        limited = tokens < 1
        self._buckets[key] = (tokens if limited else tokens - 1, now)
        return limited


class SlidingWindowRateLimiter:
    def __init__(self, *, requests_per_burst: int, burst_duration_seconds: int):
        self._requests_per_burst = requests_per_burst
        self._window = burst_duration_seconds
        self._log: Dict[str, List[float]] = {}
        self._last_sweep = 0.0

    def is_rate_limited(self, key: str) -> bool:
        now = time.time()
        self._sweep(now)
        timestamps = [t for t in self._log.get(key, []) if now - t <= self._window]
        self._log[key] = timestamps
        if len(timestamps) >= self._requests_per_burst:
            return True
        timestamps.append(now)
        return False

    def _sweep(self, now: float) -> None:
        # Unauthenticated routes see every IP that ever reached them; drop the
        # quiet ones once per window so the log cannot grow for the process
        # lifetime.
        if now - self._last_sweep < self._window:
            return
        self._last_sweep = now
        for key, timestamps in list(self._log.items()):
            if all(now - t > self._window for t in timestamps):
                del self._log[key]

    def clear(self) -> None:
        self._log.clear()
        self._last_sweep = 0.0
