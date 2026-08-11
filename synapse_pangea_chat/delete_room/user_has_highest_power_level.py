from synapse.module_api import ModuleApi

from synapse_pangea_chat.delete_room.constants import EVENT_TYPE_M_ROOM_POWER_LEVELS


async def user_has_highest_power_level(
    api: ModuleApi, user_id: str, room_id: str
) -> bool:
    """
    Check if the user is at the room's highest power level (ties qualify).

    Members not listed in `users` hold `users_default`, so the default is both
    the unlisted user's own level and a floor for the room's highest level.
    A room with no m.room.power_levels state event denies cleanly.
    """
    room_state = await api.get_room_state(
        room_id=room_id,
        event_filter=[(EVENT_TYPE_M_ROOM_POWER_LEVELS, "")],
    )
    power_levels = None
    for state_event in room_state.values():
        if state_event.type == EVENT_TYPE_M_ROOM_POWER_LEVELS:
            power_levels = state_event.content
            break
    if power_levels is None:
        return False
    users_default = power_levels.get("users_default", 0)
    user_power_levels = power_levels.get("users", {})
    user_power_level = user_power_levels.get(user_id, users_default)
    highest_power_level = max([users_default, *user_power_levels.values()])
    return user_power_level >= highest_power_level
