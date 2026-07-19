"""Pick a local user with the power to send a state event into a room.

The backfill writes a real state event, so it needs a sender the room's own
power levels already accept — typically the course creator. Nobody's power
level is ever raised to make a room writable: a room with no eligible local
sender is skipped, because escalating someone's power to fix a metadata field
changes who controls the room.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Set

from synapse.api.constants import EventTypes
from synapse.module_api import ModuleApi

MEMBERSHIP_JOIN = "join"
DEFAULT_STATE_DEFAULT = 50

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.public_courses.select_state_sender"
)


def _coerce_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def required_power_for_state_event(
    power_levels_content: Optional[Dict[str, Any]],
    event_type: str,
) -> int:
    """Power needed to send *event_type* as state, per m.room.power_levels."""
    if not power_levels_content:
        return DEFAULT_STATE_DEFAULT

    state_default = _coerce_int(
        power_levels_content.get("state_default"), DEFAULT_STATE_DEFAULT
    )
    events = power_levels_content.get("events")
    if isinstance(events, dict) and event_type in events:
        return _coerce_int(events[event_type], state_default)
    return state_default


async def select_state_sender(
    api: ModuleApi,
    room_id: str,
    event_type: str,
) -> Optional[str]:
    """Highest-powered local joined member able to send *event_type* as state.

    ``None`` when the room has no such member — a remote-owned room, or one
    whose local members all sit below the required level.
    """
    room_state = await api.get_room_state(
        room_id=room_id,
        event_filter=[
            (EventTypes.Create, ""),
            (EventTypes.PowerLevels, ""),
            (EventTypes.Member, None),
        ],
    )

    create_event = room_state.get((EventTypes.Create, ""))
    power_levels_event = room_state.get((EventTypes.PowerLevels, ""))

    local_joined_members: Set[str] = set()
    for state_event in room_state.values():
        if state_event.type != EventTypes.Member:
            continue
        if state_event.content.get("membership") != MEMBERSHIP_JOIN:
            continue
        member_id = state_event.state_key
        if not isinstance(member_id, str) or not api.is_mine(member_id):
            continue
        local_joined_members.add(member_id)

    if not local_joined_members:
        return None

    power_levels_content: Optional[Dict[str, Any]] = None
    if power_levels_event is not None and isinstance(power_levels_event.content, dict):
        power_levels_content = dict(power_levels_event.content)

    required_power = required_power_for_state_event(power_levels_content, event_type)

    users_default = 0
    users_power_levels: Dict[str, Any] = {}
    if power_levels_content is not None:
        users_default = _coerce_int(power_levels_content.get("users_default"), 0)
        raw_users = power_levels_content.get("users")
        if isinstance(raw_users, dict):
            users_power_levels = dict(raw_users)

    # Room versions with MSC4289 creator power give the creator(s) an implicit
    # 100 that never appears in the users map.
    creator_power_users: Set[str] = set()
    if create_event is not None and getattr(
        create_event.room_version,
        "msc4289_creator_power_enabled",
        False,
    ):
        creators = list(create_event.content.get("additional_creators", []) or [])
        creators.append(create_event.sender)
        for creator in creators:
            if isinstance(creator, str) and creator in local_joined_members:
                creator_power_users.add(creator)

    best_candidate: Optional[tuple[int, str]] = None
    for candidate_user_id in sorted(local_joined_members):
        candidate_power = users_default
        if candidate_user_id in creator_power_users:
            candidate_power = 100
        elif candidate_user_id in users_power_levels:
            candidate_power = _coerce_int(
                users_power_levels[candidate_user_id], users_default
            )

        candidate = (candidate_power, candidate_user_id)
        if best_candidate is None or candidate > best_candidate:
            best_candidate = candidate

    if best_candidate is None or best_candidate[0] < required_power:
        return None

    return best_candidate[1]
