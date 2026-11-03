from __future__ import annotations

import time
from typing import TYPE_CHECKING, Dict, List

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

_request_log: Dict[str, List[float]] = {}
_last_prune = 0.0


# A start page asks for its preview every few seconds while open, so the
# preview gets this many times the join allowance.
PREVIEW_ALLOWANCE_FACTOR = 3


def _prune(now: float, window: float) -> None:
    """Drops the users whose requests have all left the window, at most once
    per window, so the log holds only recent callers."""
    global _last_prune
    if now - _last_prune < window:
        return
    _last_prune = now
    for key in [
        k for k, times in _request_log.items() if not times or now - times[-1] > window
    ]:
        del _request_log[key]


def is_rate_limited(
    user_id: str, config: PangeaChatConfig, *, preview: bool = False
) -> bool:
    """Per-user sliding window for the discussion_room endpoint; previews are
    counted in a window of their own, so polling never uses up joins."""
    now = time.time()
    window = config.discussion_room_burst_duration_seconds
    _prune(now, window)
    key = f"{user_id}:preview" if preview else user_id
    limit = config.discussion_room_requests_per_burst * (
        PREVIEW_ALLOWANCE_FACTOR if preview else 1
    )
    recent = [t for t in _request_log.get(key, []) if now - t <= window]
    limited = len(recent) >= limit
    if not limited:
        recent.append(now)
    _request_log[key] = recent
    return limited
