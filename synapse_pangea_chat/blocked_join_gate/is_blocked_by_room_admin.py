"""The shared predicate behind every gated entry path.

Design: .github/instructions/blocked-join-gate.instructions.md
"""

import logging
from typing import Callable, Set

from synapse.module_api import ModuleApi

from synapse_pangea_chat.blocked_join_gate.constants import (
    ADMIN_POWER_LEVEL,
    EVENT_TYPE_M_ROOM_CREATE,
    EVENT_TYPE_M_ROOM_MEMBER,
    EVENT_TYPE_M_ROOM_POWER_LEVELS,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_JOIN,
    USERS_DEFAULT_POWER_LEVEL_KEY,
    USERS_POWER_LEVEL_KEY,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.blocked_join_gate.is_blocked_by_room_admin"
)


async def is_blocked_by_room_admin(api: ModuleApi, room_id: str, user_id: str) -> bool:
    """True when any joined admin (power level >= 100) of ``room_id`` has
    ``user_id`` on their block (Matrix ignore) list."""
    ignorers = await _users_ignoring(api, user_id)
    if not ignorers:
        # The common case: nobody has blocked this user, so no room state
        # is read at all.
        return False

    power_level_of = await _power_level_lookup(api, room_id)
    for candidate in ignorers:
        if power_level_of(candidate) < ADMIN_POWER_LEVEL:
            continue
        if await _is_joined(api, room_id, candidate):
            logger.info(
                "Refusing %s entry to %s: blocked by room admin", user_id, room_id
            )
            return True
    return False


async def _users_ignoring(api: ModuleApi, user_id: str) -> Set[str]:
    # ``ignored_by`` is Synapse's own cached index over m.ignored_user_list —
    # one lookup for "who blocks this user" instead of reading every admin's
    # account data. It is a private store method rather than ModuleApi, the
    # same trade room_code already makes for its state queries.
    store = api._hs.get_datastores().main
    return set(await store.ignored_by(user_id))  # type: ignore[call-arg, arg-type, misc]


async def _power_level_lookup(api: ModuleApi, room_id: str) -> Callable[[str], int]:
    """Return a function giving any user's power level in the room."""
    state = await api.get_room_state(
        room_id=room_id,
        event_filter=[
            (EVENT_TYPE_M_ROOM_POWER_LEVELS, ""),
            (EVENT_TYPE_M_ROOM_CREATE, ""),
        ],
    )
    power_levels = state.get((EVENT_TYPE_M_ROOM_POWER_LEVELS, ""))
    if power_levels is None:
        # Per the auth rules a room with no power-levels event gives its
        # creator power level 100 and everyone else 0.
        create = state.get((EVENT_TYPE_M_ROOM_CREATE, ""))
        creator = create.sender if create is not None else None
        return lambda user: ADMIN_POWER_LEVEL if user == creator else 0

    content = power_levels.content
    users = content.get(USERS_POWER_LEVEL_KEY) or {}
    users_default = _as_int(content.get(USERS_DEFAULT_POWER_LEVEL_KEY), 0)
    return lambda user: _as_int(users.get(user), users_default)


async def _is_joined(api: ModuleApi, room_id: str, user_id: str) -> bool:
    state = await api.get_room_state(
        room_id=room_id, event_filter=[(EVENT_TYPE_M_ROOM_MEMBER, user_id)]
    )
    member = state.get((EVENT_TYPE_M_ROOM_MEMBER, user_id))
    return (
        member is not None
        and member.content.get(MEMBERSHIP_CONTENT_KEY) == MEMBERSHIP_JOIN
    )


def _as_int(value: object, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default
