from synapse.module_api import ModuleApi

from synapse_pangea_chat.delete_room.constants import EVENT_TYPE_M_ROOM_POWER_LEVELS


async def user_has_highest_power_level(
    api: ModuleApi, user_id: str, room_id: str
) -> bool:
    """
    Check if the user has the highest power level in the room.
    """
    room_member_state_events = await api.get_room_state(
        room_id=room_id,
        event_filter=[(EVENT_TYPE_M_ROOM_POWER_LEVELS, "")],
    )
    highest_power_level = None
    user_power_level = None
    for state_event in room_member_state_events.values():
        if state_event.type != EVENT_TYPE_M_ROOM_POWER_LEVELS:
            continue
        # At this point we can expect `state_event.content` to follow the schema defined
        # in https://spec.matrix.org/v1.11/client-server-api/#mroompower_levels
        users_default = state_event.content.get("users_default", 0)
        user_power_levels = state_event.content.get("users", {})
        for user, power_level in user_power_levels.items():
            if user == user_id:
                user_power_level = power_level
            if highest_power_level is None or power_level > highest_power_level:
                highest_power_level = power_level
    if user_power_level is None:
        user_power_level = users_default
    return user_power_level >= highest_power_level
