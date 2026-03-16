from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

import time
from typing import Dict, List

request_log: Dict[str, List[float]] = {}


def is_rate_limited(ip: str, config: PangeaChatConfig) -> bool:
    current_time = time.time()

    if ip not in request_log:
        request_log[ip] = []

    request_log[ip] = [
        timestamp
        for timestamp in request_log[ip]
        if current_time - timestamp <= config.register_email_burst_duration_seconds
    ]

    if len(request_log[ip]) >= config.register_email_requests_per_burst:
        return True

    request_log[ip].append(current_time)

    return False
