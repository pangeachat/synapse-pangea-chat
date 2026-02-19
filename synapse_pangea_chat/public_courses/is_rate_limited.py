from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict, List, Tuple

if TYPE_CHECKING:
    from synapse_pangea_chat import PangeaChatConfig

request_log: Dict[str, List[float]] = {}


class RateLimitError(Exception):
    """Custom exception for rate limiting errors."""

    pass


def _get_config_window(config: PangeaChatConfig) -> Tuple[int, int]:
    duration = getattr(config, "public_courses_burst_duration_seconds", 120)
    requests = getattr(config, "public_courses_requests_per_burst", 120)
    if duration < 1:
        duration = 1
    if requests < 1:
        requests = 1
    return duration, requests


def is_rate_limited(user_id: str, config: PangeaChatConfig) -> bool:
    current_time = time.time()
    window_seconds, max_requests = _get_config_window(config)

    # Get the list of request timestamps for the user, or create an empty list if new user
    if user_id not in request_log:
        request_log[user_id] = []

    # Filter out requests that are older than the time window
    request_log[user_id] = [
        timestamp
        for timestamp in request_log[user_id]
        if current_time - timestamp <= window_seconds
    ]

    # Check if the number of requests in the time window exceeds the max limit
    if len(request_log[user_id]) >= max_requests:
        raise RateLimitError()

    # If not rate-limited, record the new request timestamp
    request_log[user_id].append(current_time)
    return False
