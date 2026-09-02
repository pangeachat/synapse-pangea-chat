"""The shared predicate behind every gated entry path.

Design: .github/instructions/blocked-join-gate.instructions.md
"""

import logging
from typing import Set

from synapse.module_api import ModuleApi

from synapse_pangea_chat.blocked_join_gate.constants import (
    ADMIN_POWER_LEVEL,
    EVENT_TYPE_M_ROOM_CREATE,
    EVENT_TYPE_M_ROOM_MEMBER,
    EVENT_TYPE_M_ROOM_POWER_LEVELS,
    MAX_ADMIN_CANDIDATES,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_JOIN,
    USERS_DEFAULT_POWER_LEVEL_KEY,
    USERS_POWER_LEVEL_KEY,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.blocked_join_gate.is_blocked_by_room_admin"
)


async def is_blocked_by_room_admin(api: ModuleApi, room_id: str, user_id: str) -> bool:
    """True when every joined admin (power level >= 100) of ``room_id`` has
    ``user_id`` on their block (Matrix ignore) list. A room with no joined
    admin never blocks."""
    ignorers = await _users_ignoring(api, user_id)
    if not ignorers:
        # The common case: nobody has blocked this user, so no room state
        # is read at all.
        return False

    admins = await _joined_admins(api, room_id)
    if not admins:
        return False
    if admins <= ignorers:
        logger.info(
            "Refusing %s entry to %s: blocked by every room admin", user_id, room_id
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


async def _joined_admins(api: ModuleApi, room_id: str) -> Set[str]:
    """Currently joined members whose power level is at least ADMIN_POWER_LEVEL."""
    state = await api.get_room_state(
        room_id=room_id,
        event_filter=[
            (EVENT_TYPE_M_ROOM_POWER_LEVELS, ""),
            (EVENT_TYPE_M_ROOM_CREATE, ""),
        ],
    )
    power_levels = state.get((EVENT_TYPE_M_ROOM_POWER_LEVELS, ""))
    create = state.get((EVENT_TYPE_M_ROOM_CREATE, ""))
    if power_levels is None:
        # Per the auth rules a room with no power-levels event gives its
        # creator power level 100 and everyone else 0.
        candidates: Set[str] = {create.sender} if create is not None else set()
    else:
        content = power_levels.content
        users = content.get(USERS_POWER_LEVEL_KEY) or {}
        users_default = _as_int(content.get(USERS_DEFAULT_POWER_LEVEL_KEY), 0)
        if users_default >= ADMIN_POWER_LEVEL:
            # Every member is an admin — a hostile-shaped config, not a real
            # course. Fail open rather than enumerate the whole membership.
            logger.warning(
                "blocked_join_gate: %s has users_default >= %d; allowing",
                room_id,
                ADMIN_POWER_LEVEL,
            )
            return set()
        candidates = {
            user
            for user, level in users.items()
            if _as_int(level, 0) >= ADMIN_POWER_LEVEL
        }

    # In room versions with creator power (MSC4289, room v12+) the creator
    # holds admin power without a power-levels entry; missing them here would
    # let the listed admins alone refuse against the design's all-admins rule.
    if create is not None and getattr(
        create.room_version, "msc4289_creator_power_enabled", False
    ):
        candidates.add(create.sender)
        additional = create.content.get("additional_creators")
        if isinstance(additional, list):
            candidates.update(u for u in additional if isinstance(u, str))

    if len(candidates) > MAX_ADMIN_CANDIDATES:
        # An attacker can stuff arbitrarily many admin entries into a room
        # they control; a real course has a handful. Fail open past the bound.
        logger.warning(
            "blocked_join_gate: %s names %d admin candidates (bound %d); allowing",
            room_id,
            len(candidates),
            MAX_ADMIN_CANDIDATES,
        )
        return set()

    joined: Set[str] = set()
    for user in candidates:
        if await _is_joined(api, room_id, user):
            joined.add(user)
    return joined


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
