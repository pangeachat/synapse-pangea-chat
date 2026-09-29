from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict, List

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

request_log: Dict[str, List[float]] = {}


def is_rate_limited(user_id: str, config: PangeaChatConfig) -> bool:
    """Sliding-window limit per caller: at most
    ``course_member_emails_requests_per_burst`` calls in any
    ``course_member_emails_burst_duration_seconds``."""
    now = time.time()
    window = max(1, config.course_member_emails_burst_duration_seconds)
    limit = max(1, config.course_member_emails_requests_per_burst)

    recent = [t for t in request_log.get(user_id, []) if now - t <= window]
    if len(recent) >= limit:
        request_log[user_id] = recent
        return True
    recent.append(now)
    request_log[user_id] = recent
    return False
