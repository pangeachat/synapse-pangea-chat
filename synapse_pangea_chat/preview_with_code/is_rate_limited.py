from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict, List

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

request_log: Dict[str, List[float]] = {}


def is_rate_limited(user_id: str, config: PangeaChatConfig) -> bool:
    current_time = time.time()

    if user_id not in request_log:
        request_log[user_id] = []

    request_log[user_id] = [
        timestamp
        for timestamp in request_log[user_id]
        if current_time - timestamp <= config.preview_with_code_burst_duration_seconds
    ]

    if len(request_log[user_id]) >= config.preview_with_code_requests_per_burst:
        return True

    request_log[user_id].append(current_time)

    return False
