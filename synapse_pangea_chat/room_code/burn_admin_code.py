"""Remove the admin_access_code from a room's join rules (single-use burn)."""

from __future__ import annotations

import logging

from synapse.module_api import ModuleApi

from synapse_pangea_chat.room_code.constants import (
    ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    EVENT_TYPE_M_ROOM_JOIN_RULES,
)
from synapse_pangea_chat.room_code.get_inviter_user import persist_without_auth_checks

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.room_code.burn_admin_code"
)


async def burn_admin_code(api: ModuleApi, room_id: str, burner_user_id: str) -> bool:
    """
    Remove the admin_access_code field from the room's m.room.join_rules state.
    Written without the room's auth rules (persist_without_auth_checks).
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

        await persist_without_auth_checks(
            api,
            {
                "type": EVENT_TYPE_M_ROOM_JOIN_RULES,
                "room_id": room_id,
                "sender": burner_user_id,
                "state_key": "",
                "content": current_content,
            },
        )

        logger.info(f"Burned admin_access_code for room {room_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to burn admin_access_code for room {room_id}: {e}")
        return False
