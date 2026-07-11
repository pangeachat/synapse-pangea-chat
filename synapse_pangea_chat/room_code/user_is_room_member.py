from typing import Optional

from synapse.module_api import ModuleApi

from synapse_pangea_chat.room_code.constants import (
    EVENT_TYPE_M_ROOM_MEMBER,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_JOIN,
)


async def get_user_room_membership(
    api: ModuleApi, user_id: str, room_id: str
) -> Optional[str]:
    """Return the user's current membership value in the room, or None
    when the user has no m.room.member state there."""
    room_member_state_events = await api.get_room_state(
        room_id=room_id,
        event_filter=[(EVENT_TYPE_M_ROOM_MEMBER, user_id)],
    )
    for state_event in room_member_state_events.values():
        if (
            state_event.type != EVENT_TYPE_M_ROOM_MEMBER
            or state_event.state_key != user_id
        ):
            continue
        membership = state_event.content.get(MEMBERSHIP_CONTENT_KEY)
        if isinstance(membership, str):
            return membership
    return None


async def user_is_room_member(api: ModuleApi, user_id: str, room_id: str) -> bool:
    membership = await get_user_room_membership(api, user_id, room_id)
    return membership == MEMBERSHIP_JOIN
