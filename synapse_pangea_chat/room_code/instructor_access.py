"""Current instructor authority, including creator power in newer rooms."""
from synapse.api.constants import EventTypes


def user_power(state, user_id):
    create = state.get((EventTypes.Create, ""))
    if create is not None and getattr(
        create.room_version, "msc4289_creator_power_enabled", False
    ):
        if user_id == create.sender or user_id in create.content.get(
            "additional_creators", []
        ):
            return float("inf")
    power = state.get((EventTypes.PowerLevels, ""))
    if power is None:
        return 0
    return power.content.get("users", {}).get(
        user_id, power.content.get("users_default", 0)
    )


def is_joined_instructor(state, user_id):
    member = state.get((EventTypes.Member, user_id))
    return bool(
        member
        and member.content.get("membership") == "join"
        and user_power(state, user_id) >= 100
    )
