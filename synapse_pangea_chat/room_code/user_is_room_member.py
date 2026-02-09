from synapse.module_api import ModuleApi

from synapse_pangea_chat.room_code.constants import (
    EVENT_TYPE_M_ROOM_MEMBER,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_JOIN,
)


async def user_is_room_member(api: ModuleApi, user_id: str, room_id: str) -> bool:
    room_member_state_events = await api.get_room_state(
        room_id=room_id,
        event_filter=[(EVENT_TYPE_M_ROOM_MEMBER, user_id)],
    )
    is_member = False
    for state_event in room_member_state_events.values():
        if (
            state_event.type != EVENT_TYPE_M_ROOM_MEMBER
            or state_event.state_key != user_id
            or state_event.content.get(MEMBERSHIP_CONTENT_KEY) != MEMBERSHIP_JOIN
        ):
            continue
        is_member = True
        break
    return is_member
