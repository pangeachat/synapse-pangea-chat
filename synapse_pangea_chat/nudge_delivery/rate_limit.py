"""Per-key sliding-window rate limiter for the unauthenticated nudge links."""

from __future__ import annotations

import time
from typing import Dict, List


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
