from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict, List, Tuple

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

request_log: Dict[str, List[float]] = {}


def _get_config_window(config: PangeaChatConfig) -> Tuple[int, int]:
    duration = getattr(config, "user_activity_burst_duration_seconds", 60)
    requests = getattr(config, "user_activity_requests_per_burst", 10)
    if duration < 1:
        duration = 1
    if requests < 1:
        requests = 1
    return duration, requests


def is_rate_limited(user_id: str, config: PangeaChatConfig) -> bool:
    current_time = time.time()
    window_seconds, max_requests = _get_config_window(config)

    if user_id not in request_log:
        request_log[user_id] = []

    request_log[user_id] = [
        timestamp
        for timestamp in request_log[user_id]
        if current_time - timestamp <= window_seconds
    ]

    if len(request_log[user_id]) >= max_requests:
        return True

    request_log[user_id].append(current_time)
    return False
