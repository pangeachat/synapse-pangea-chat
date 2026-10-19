import logging
from typing import Any, Dict, Optional

from synapse.api.constants import EventTypes
from synapse.events import EventBase
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


async def persist_without_auth_checks(
    api: ModuleApi, event_dict: Dict[str, Any]
) -> EventBase:
    """Write an event into a local room without the room's auth rules.

    Synapse checks those rules in ``handle_new_client_event``; building the
    event with no requester and persisting it directly skips them. Only for
    writes the module has already decided are legitimate even though the
    room's own rules refuse them (instructor grants, a default chat's
    recovery). Internal Synapse APIs: re-check on every Synapse upgrade.
    """
    hs = api._hs
    event_creation_handler = hs.get_event_creation_handler()
    room_version = await hs.get_datastores().main.get_room_version(
        event_dict["room_id"]
    )
    builder = hs.get_event_builder_factory().for_room_version(room_version, event_dict)
    event, unpersisted_context = await event_creation_handler.create_new_client_event(
        builder=builder,
        requester=None,
    )
    context = await unpersisted_context.persist(event)
    await event_creation_handler._persist_events(
        requester=create_requester(
            event_dict["sender"], authenticated_entity=api.server_name
        ),
        events_and_context=[(event, context)],
        ratelimit=False,
        extra_users=[],
    )
    return event


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

        await persist_without_auth_checks(
            api,
            {
                "type": EVENT_TYPE_M_ROOM_POWER_LEVELS,
                "room_id": room_id,
                "sender": user_to_promote,
                "state_key": "",
                "content": current_power_levels,
            },
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
