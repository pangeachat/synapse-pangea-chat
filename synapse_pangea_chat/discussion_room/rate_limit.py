from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict, List

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

_request_log: Dict[str, List[float]] = {}


def is_rate_limited(user_id: str, config: PangeaChatConfig) -> bool:
    """Per-user sliding window for the discussion_room endpoint."""
    now = time.time()
    window = config.discussion_room_burst_duration_seconds
    recent = [t for t in _request_log.get(user_id, []) if now - t <= window]
    if len(recent) >= config.discussion_room_requests_per_burst:
        _request_log[user_id] = recent
        return True
    recent.append(now)
    _request_log[user_id] = recent
    return False
