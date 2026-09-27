import logging
from typing import Optional

from synapse.api.constants import EventTypes
from synapse.module_api import ModuleApi
from synapse.types import UserID, create_requester

from synapse_pangea_chat.room_code.constants import (
    EVENT_TYPE_M_ROOM_POWER_LEVELS,
    USERS_POWER_LEVEL_KEY,
)
from synapse_pangea_chat.room_code.instructor_access import (
    is_joined_instructor,
    user_power,
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
    state = await api.get_room_state(room_id=room_id)
    power = state.get((EventTypes.PowerLevels, ""))
    threshold = max(100, power.content.get("invite", 0) if power else 100)
    candidates = [
        key
        for kind, key in state
        if kind == EventTypes.Member
        and api.is_mine(key)
        and is_joined_instructor(state, key)
        and user_power(state, key) >= threshold
    ]
    if not candidates:
        logger.warning("No eligible joined administrator in %s", room_id)
        return None
    return UserID.from_string(
        sorted(candidates, key=lambda user: (-user_power(state, user), user))[0]
    )


async def was_previously_admin(api: ModuleApi, room_id: str, user_id: str) -> bool:
    """Conservative withdrawal check before resuming a partial legacy grant."""
    import json

    def select(txn):
        txn.execute(
            """SELECT j.json FROM event_json j JOIN state_events s ON s.event_id = j.event_id
            WHERE j.room_id = ? AND s.type = 'm.room.power_levels'""",
            (room_id,),
        )
        for row in txn.fetchall():
            content = json.loads(row[0]).get("content", {})
            if (
                content.get("users", {}).get(user_id, content.get("users_default", 0))
                >= 100
            ):
                return True
        return False

    return await api._hs.get_datastores().main.db_pool.runInteraction(
        "pangea_claim_prior_rights", select
    )
