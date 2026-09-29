"""Which members of a course a course admin may see the email of.

Pure functions over room state, so the rule is testable without a homeserver.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Mapping, Set, Tuple

from synapse_pangea_chat.bot_user_ids import is_probable_bot_user_id

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.course_member_emails.members"
)

# Course admin: the power level a course's teachers hold.
COURSE_ADMIN_POWER_LEVEL = 100


def _coerce_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        logger.warning("Boolean power level %r; using %d", value, default)
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Non-integer power level %r; using %d", value, default)
        return default


def effective_power_levels(
    user_ids: Iterable[str],
    power_levels_content: Mapping[str, Any] | None,
    creators: Set[str],
) -> Dict[str, int]:
    """Each user's power level as the room enforces it.

    Room versions with MSC4289 give creators unlimited power without listing
    them in ``m.room.power_levels``; they are counted as course admins here.
    """
    users_default = 0
    users: Mapping[str, Any] = {}
    if power_levels_content is not None:
        users_default = _coerce_int(power_levels_content.get("users_default"), 0)
        raw_users = power_levels_content.get("users", {})
        if isinstance(raw_users, Mapping):
            users = raw_users
    levels: Dict[str, int] = {}
    for user_id in user_ids:
        if user_id in creators:
            levels[user_id] = COURSE_ADMIN_POWER_LEVEL
        elif user_id in users:
            levels[user_id] = _coerce_int(users[user_id], users_default)
        else:
            levels[user_id] = users_default
    return levels


def visible_member_ids(
    *,
    joined_member_ids: Iterable[str],
    power_levels: Mapping[str, int],
    caller_id: str,
    is_mine: Callable[[str], bool],
) -> List[str]:
    """Members whose email a course admin may see: joined, local, not the
    caller, not a bot, and not another course admin. Sorted for a stable
    response."""
    return sorted(
        user_id
        for user_id in set(joined_member_ids)
        if user_id != caller_id
        and is_mine(user_id)
        and not is_probable_bot_user_id(user_id)
        and power_levels.get(user_id, 0) < COURSE_ADMIN_POWER_LEVEL
    )


def pick_email_per_user(
    rows: Iterable[Mapping[str, Any]],
) -> Dict[str, str]:
    """One address per user when a user has bound several: the one bound
    first (then alphabetical), so the answer does not change between calls."""
    chosen: Dict[str, Tuple[Tuple[int, str], str]] = {}
    for row in rows:
        user_id = row.get("user_id")
        address = row.get("address")
        if not isinstance(user_id, str) or not isinstance(address, str):
            continue
        added_at = row.get("added_at")
        key = (added_at if isinstance(added_at, int) else 0, address)
        current = chosen.get(user_id)
        if current is None or key < current[0]:
            chosen[user_id] = (key, address)
    return {user_id: value[1] for user_id, value in chosen.items()}
