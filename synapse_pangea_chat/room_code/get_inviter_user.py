import logging
from typing import Optional

from synapse.module_api import ModuleApi
from synapse.types import UserID, create_requester

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


async def promote_user_to_admin(
    api: ModuleApi,
    room_id: str,
    user_to_promote: str,
    invite_power: int,
) -> bool:
    """
    Promote a user to have sufficient power level to invite other users.
    Uses internal Synapse APIs to bypass auth checks.
    Returns True if successful, False otherwise.
    """
    try:
        # Get current power levels state
        power_levels_state_events = await api.get_room_state(
            room_id=room_id,
            event_filter=[(EVENT_TYPE_M_ROOM_POWER_LEVELS, None)],
        )

        current_power_levels = None
        for state_event in power_levels_state_events.values():
            if state_event.type != EVENT_TYPE_M_ROOM_POWER_LEVELS:
                continue
            current_power_levels = dict(state_event.content)
            break

        if current_power_levels is None:
            return False

        # Update the user's power level to be able to invite
        users_power_levels = dict(current_power_levels.get(USERS_POWER_LEVEL_KEY, {}))
        users_power_levels[user_to_promote] = invite_power
        current_power_levels[USERS_POWER_LEVEL_KEY] = users_power_levels

        # Access internal Synapse handlers to bypass auth checks
        # WARNING: This uses internal APIs and may break with Synapse updates
        hs = api._hs
        event_creation_handler = hs.get_event_creation_handler()
        store = hs.get_datastores().main

        # Build the event - get room version from the main store
        room_version = await store.get_room_version(room_id)
        builder = hs.get_event_builder_factory().for_room_version(
            room_version,
            {
                "type": EVENT_TYPE_M_ROOM_POWER_LEVELS,
                "room_id": room_id,
                "sender": user_to_promote,
                "state_key": "",
                "content": current_power_levels,
            },
        )

        # Create a requester for persistence (needed for _persist_events)
        # We use create_requester which is used by ModuleApi internally

        requester = create_requester(
            user_to_promote,
            authenticated_entity=api.server_name,
        )

        # Create the event without auth checks (requester=None bypasses auth)
        (
            event,
            unpersisted_context,
        ) = await event_creation_handler.create_new_client_event(
            builder=builder,
            requester=None,  # No requester means no auth checks
        )

        # Persist the event context
        context = await unpersisted_context.persist(event)

        # Use _persist_events directly which handles worker routing
        # but does not check auth rules (unlike handle_new_client_event)
        await event_creation_handler._persist_events(
            requester=requester,
            events_and_context=[(event, context)],
            ratelimit=False,
            extra_users=[],
        )

        logger.info(
            f"Successfully promoted user {user_to_promote} to power level {invite_power} "
            f"in room {room_id}"
        )
        return True
    except Exception as e:
        logger.error(f"Failed to promote user {user_to_promote} in room {room_id}: {e}")
        return False


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
        # Promote the user to have sufficient power to invite
        promoted = await promote_user_to_admin(
            api=api,
            room_id=room_id,
            user_to_promote=local_user_id_with_highest_power,
            invite_power=invite_power,
        )
        if not promoted:
            return None

    return UserID.from_string(local_user_id_with_highest_power)
