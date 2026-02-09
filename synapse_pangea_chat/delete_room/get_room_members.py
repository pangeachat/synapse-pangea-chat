import logging

from synapse.module_api import ModuleApi

from synapse_pangea_chat.delete_room.constants import (
    EVENT_TYPE_M_ROOM_MEMBER,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_JOIN,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.delete_room.get_room_members"
)


async def get_room_members(api: ModuleApi, room_id: str) -> set[str]:
    room_member_state_events = await api.get_room_state(
        room_id=room_id,
        event_filter=[(EVENT_TYPE_M_ROOM_MEMBER, None)],
    )
    room_members = set[str]()
    for state_event in room_member_state_events.values():
        if (
            state_event.type == EVENT_TYPE_M_ROOM_MEMBER
            and state_event.content.get(MEMBERSHIP_CONTENT_KEY) == MEMBERSHIP_JOIN
        ):
            room_members.add(state_event.state_key)
    return room_members
