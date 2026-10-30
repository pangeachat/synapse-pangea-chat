"""What the routes read about a course space: who is its admin, its name, its
class code."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Set

from synapse.api.constants import EventTypes

from synapse_pangea_chat.course_member_emails.members import (
    COURSE_ADMIN_POWER_LEVEL,
    effective_power_levels,
)
from synapse_pangea_chat.room_code.constants import ACCESS_CODE_JOIN_RULE_CONTENT_KEY

SPACE_ROOM_TYPE = "m.space"
MEMBERSHIP_JOIN = "join"


class ModuleCourseAdmins:
    """Course admin = joined, in a space, at power level 100 as the room
    enforces it (room-version creators count), as ``course_member_emails``
    decides it. Read fresh on every call."""

    def __init__(self, api: Any) -> None:
        self._api = api
        self._main = api._hs.get_datastores().main

    async def is_course_admin(self, room_id: str, user_id: str) -> bool:
        membership, _ = await self._main.get_local_current_membership_for_user_in_room(
            user_id, room_id
        )
        if membership != MEMBERSHIP_JOIN:
            return False
        state = await self._api.get_room_state(
            room_id=room_id,
            event_filter=[(EventTypes.Create, ""), (EventTypes.PowerLevels, "")],
        )
        create = state.get((EventTypes.Create, ""))
        if create is None or create.content.get("type") != SPACE_ROOM_TYPE:
            return False
        creators: Set[str] = set()
        if getattr(create.room_version, "msc4289_creator_power_enabled", False):
            additional = create.content.get("additional_creators", [])
            if isinstance(additional, list):
                creators.update(c for c in additional if isinstance(c, str))
            creators.add(create.sender)
        power = state.get((EventTypes.PowerLevels, ""))
        levels = effective_power_levels(
            [user_id], power.content if power is not None else None, creators
        )
        return levels[user_id] >= COURSE_ADMIN_POWER_LEVEL


class ModuleCourseRooms:
    def __init__(self, api: Any) -> None:
        self._api = api
        self._main = api._hs.get_datastores().main

    async def _state_content(self, room_id: str, event_type: str) -> Mapping:
        state = await self._api.get_room_state(
            room_id=room_id, event_filter=[(event_type, "")]
        )
        event = state.get((event_type, ""))
        # Event content is an immutable mapping, not a dict.
        content = event.content if event is not None else None
        return content if isinstance(content, Mapping) else {}

    async def course_name(self, room_id: str) -> Optional[str]:
        name = (await self._state_content(room_id, EventTypes.Name)).get("name")
        return name if isinstance(name, str) and name else None

    async def course_topic(self, room_id: str) -> Optional[str]:
        topic = (await self._state_content(room_id, EventTypes.Topic)).get("topic")
        return topic if isinstance(topic, str) and topic else None

    async def access_code(self, room_id: str) -> Optional[str]:
        content = await self._state_content(room_id, EventTypes.JoinRules)
        code = content.get(ACCESS_CODE_JOIN_RULE_CONTENT_KEY)
        return code if isinstance(code, str) and code else None

    async def membership(self, room_id: str, user_id: str) -> Optional[str]:
        membership, _ = await self._main.get_local_current_membership_for_user_in_room(
            user_id, room_id
        )
        return membership
