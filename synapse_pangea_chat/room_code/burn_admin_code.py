"""Remove the admin_access_code from a room's join rules (single-use burn)."""

from __future__ import annotations

import logging

from synapse.module_api import ModuleApi
from synapse.types import create_requester

from synapse_pangea_chat.room_code.constants import (
    ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    EVENT_TYPE_M_ROOM_JOIN_RULES,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.room_code.burn_admin_code"
)


async def burn_admin_code(api: ModuleApi, room_id: str, burner_user_id: str) -> bool:
    """
    Remove the admin_access_code field from the room's m.room.join_rules state.
    Uses internal Synapse APIs to bypass auth checks (same pattern as promote_user_to_admin).
    Returns True on success, False on failure.
    """
    try:
        state_events = await api.get_room_state(
            room_id=room_id,
            event_filter=[(EVENT_TYPE_M_ROOM_JOIN_RULES, None)],
        )

        current_content = None
        for state_event in state_events.values():
            if state_event.type == EVENT_TYPE_M_ROOM_JOIN_RULES:
                current_content = dict(state_event.content)
                break

        if current_content is None:
            logger.warning(f"No join rules state found for room {room_id}")
            return False

        if ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY not in current_content:
            # Already burned or never set
            return True

        del current_content[ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY]

        hs = api._hs
        event_creation_handler = hs.get_event_creation_handler()
        store = hs.get_datastores().main
        room_version = await store.get_room_version(room_id)

        builder = hs.get_event_builder_factory().for_room_version(
            room_version,
            {
                "type": EVENT_TYPE_M_ROOM_JOIN_RULES,
                "room_id": room_id,
                "sender": burner_user_id,
                "state_key": "",
                "content": current_content,
            },
        )

        requester = create_requester(
            burner_user_id,
            authenticated_entity=api.server_name,
        )

        event, unpersisted_context = (
            await event_creation_handler.create_new_client_event(
                builder=builder,
                requester=None,
            )
        )
        context = await unpersisted_context.persist(event)
        await event_creation_handler._persist_events(
            requester=requester,
            events_and_context=[(event, context)],
            ratelimit=False,
            extra_users=[],
        )

        logger.info(f"Burned admin_access_code for room {room_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to burn admin_access_code for room {room_id}: {e}")
        return False
