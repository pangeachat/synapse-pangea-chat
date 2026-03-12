from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

import time
from typing import Dict, List

request_log: Dict[str, List[float]] = {}


def is_rate_limited(user_id: str, config: PangeaChatConfig) -> bool:
    current_time = time.time()

    if user_id not in request_log:
        request_log[user_id] = []

    request_log[user_id] = [
        timestamp
        for timestamp in request_log[user_id]
        if current_time - timestamp <= config.export_user_data_burst_duration_seconds
    ]

    if len(request_log[user_id]) >= config.export_user_data_requests_per_burst:
        return True

    request_log[user_id].append(current_time)

    return False
