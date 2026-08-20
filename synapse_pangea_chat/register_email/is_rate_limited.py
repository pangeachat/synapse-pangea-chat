from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

import time
from typing import Dict, List

request_log: Dict[str, List[float]] = {}

_last_sweep: float = 0.0


def is_rate_limited(ip: str, config: PangeaChatConfig) -> bool:
    current_time = time.time()
    window = config.register_email_burst_duration_seconds

    _sweep_quiet_ips(current_time, window)

    timestamps = [
        timestamp
        for timestamp in request_log.get(ip, [])
        if current_time - timestamp <= window
    ]
    request_log[ip] = timestamps

    if len(timestamps) >= config.register_email_requests_per_burst:
        return True

    timestamps.append(current_time)

    return False


def _sweep_quiet_ips(current_time: float, window: float) -> None:
    """Drop IPs with nothing left inside the window.

    The route is unauthenticated, so without this the log keeps a key for every
    IP that ever reached it, for the lifetime of the process. Sweeping once per
    window rather than per request keeps the cost off the hot path.
    """
    global _last_sweep

    if current_time - _last_sweep < window:
        return
    _last_sweep = current_time

    for logged_ip, timestamps in list(request_log.items()):
        if all(current_time - timestamp > window for timestamp in timestamps):
            del request_log[logged_ip]
