import logging
from typing import Optional

from synapse.module_api import ModuleApi
from synapse.types import UserID

from synapse_pangea_chat.room_code.constants import (
    DEFAULT_INVITE_POWER_LEVEL,
    DEFAULT_USERS_DEFAULT_POWER_LEVEL,
    EVENT_TYPE_M_ROOM_MEMBER,
    EVENT_TYPE_M_ROOM_POWER_LEVELS,
    INVITE_POWER_LEVEL_KEY,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_JOIN,
    USERS_DEFAULT_POWER_LEVEL_KEY,
    USERS_POWER_LEVEL_KEY,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.room_code.get_inviter_user"
)


async def get_inviter_user(api: ModuleApi, room_id: str) -> Optional[UserID]:
    # inviter must be local and have sufficient power to invite

    # extract room power levels
    power_levels_state_events = await api.get_room_state(
        room_id=room_id,
        event_filter=[(EVENT_TYPE_M_ROOM_POWER_LEVELS, None)],
    )
    power_levels = None
    for state_event in power_levels_state_events.values():
        if state_event.type != EVENT_TYPE_M_ROOM_POWER_LEVELS:
            continue
        power_levels = state_event.content
        break
    if not power_levels:
        return None

    # extract power required to invite
    try:
        invite_power = int(
            power_levels.get(
                INVITE_POWER_LEVEL_KEY,
                DEFAULT_INVITE_POWER_LEVEL,
            )
        )
    except ValueError:
        invite_power = DEFAULT_INVITE_POWER_LEVEL

    # extract default power level
    try:
        users_default = int(
            power_levels.get(
                USERS_DEFAULT_POWER_LEVEL_KEY,
                DEFAULT_USERS_DEFAULT_POWER_LEVEL,
            )
        )
    except ValueError:
        users_default = DEFAULT_USERS_DEFAULT_POWER_LEVEL

    # extract users power levels
    users_power_level = power_levels.get(USERS_POWER_LEVEL_KEY, None)
    if not isinstance(users_power_level, dict):
        users_power_level = {}

    # Get all room members to consider users with default power level
    member_state_events = await api.get_room_state(
        room_id=room_id,
        event_filter=[(EVENT_TYPE_M_ROOM_MEMBER, None)],
    )

    # Build a set of local joined members
    local_joined_members: set[str] = set()
    for state_event in member_state_events.values():
        if state_event.type != EVENT_TYPE_M_ROOM_MEMBER:
            continue
        membership = state_event.content.get(MEMBERSHIP_CONTENT_KEY)
        if membership != MEMBERSHIP_JOIN:
            continue
        user_id = state_event.state_key
        if not isinstance(user_id, str):
            continue
        # Only consider local users
        if not api.is_mine(user_id):
            continue
        local_joined_members.add(user_id)

    if not local_joined_members:
        logger.warning(f"No local joined members found in room {room_id}")
        return None

    # Find the local user with the highest power level
    local_user_id_with_highest_power = None
    highest_local_power = None

    for user_id in local_joined_members:
        # Get user's power level (from explicit setting or default)
        if user_id in users_power_level:
            try:
                power_level = int(users_power_level[user_id])
            except (ValueError, TypeError):
                power_level = users_default
        else:
            # User has default power level
            power_level = users_default

        # Track the highest power level among local members
        if highest_local_power is None or power_level > highest_local_power:
            highest_local_power = power_level
            local_user_id_with_highest_power = user_id

    if local_user_id_with_highest_power is None or highest_local_power is None:
        logger.warning(f"No local user found in room {room_id}")
        return None

    logger.info(
        f"Found local user {local_user_id_with_highest_power} with power {highest_local_power} "
        f"in room {room_id}, invite power required: {invite_power}"
    )

    # Check if the user with the highest power level can invite
    if highest_local_power < invite_power:
        logger.warning(
            "No local user in room %s has sufficient power to invite "
            "(highest: %d, required: %d). Cannot auto-invite.",
            room_id,
            highest_local_power,
            invite_power,
        )
        return None

    return UserID.from_string(local_user_id_with_highest_power)
