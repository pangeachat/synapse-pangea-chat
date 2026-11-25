"""What graduating from a Protected course does to the student's rooms
(pangeachat/.github#799, managed-accounts "Graduation"), once the record is
graduated. ``reconcile`` lifts this course's controls (another managing
course keeps its own) and puts the student back into the rooms they had taken
them out of. Then, in each of the course's rooms the student is joined to,
their power level is set below sending: the history stays readable, not
changeable. The course's admins leave the student's analytics rooms; the app
offers the existing share flow. Membership and bans are never changed."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

from synapse.api.constants import EventTypes
from synapse.util.frozenutils import unfreeze

from synapse_pangea_chat.analytics_push_suppression import ANALYTICS_ROOM_TYPE
from synapse_pangea_chat.moderation.courses import ChatCourses, power_level
from synapse_pangea_chat.public_courses.select_state_sender import (
    select_state_sender,
)
from synapse_pangea_chat.student_invitations.report import report_failure


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return default


def read_only(
    content: Mapping[str, Any], user_id: str, current: int
) -> Optional[Dict[str, Any]]:
    """The power levels with ``user_id`` one below sending any event, state
    or not; None when they already are."""
    events = content.get("events")
    levels = [_int(v) for v in events.values()] if isinstance(events, Mapping) else []
    defaults = [
        _int(content.get("events_default")),
        _int(content.get("state_default"), 50),
    ]
    below = min(defaults + levels) - 1
    if current <= below:
        return None
    users = content.get("users")
    users = dict(users) if isinstance(users, Mapping) else {}
    return {**content, "users": {**users, user_id: below}}


class Graduation:
    def __init__(self, api: Any, admins: Any, controls: Any) -> None:
        self._api = api
        self._main = api._hs.get_datastores().main
        self._admins = admins
        self._controls = controls
        self._courses = ChatCourses.from_homeserver(api._hs)

    def later(self, room_id: str, user_ids: Iterable[str]) -> None:
        self._api.run_as_background_process(
            "pangea_protected_graduation", self.apply, room_id, list(user_ids)
        )

    async def apply(self, room_id: str, user_ids: List[str]) -> None:
        """Each step of each student is reported on failure and the rest go
        on."""
        for user_id in user_ids:
            try:
                await self._controls.reconcile(user_id)
                rooms = sorted(await self._main.get_rooms_for_user(user_id))
                # A student promoted to the course's admin keeps their level.
                admin = await self._admins.is_course_admin(room_id, user_id)
            except Exception as error:
                report_failure("graduation", error, room=room_id, user=user_id)
                continue
            for room in rooms:
                try:
                    owners = await self._courses.for_room(
                        room, self._courses.position_now()
                    )
                    if room_id in owners:
                        if not admin:
                            await self._read_only(room, user_id)
                    else:
                        await self._unshare(room, user_id, room_id)
                except Exception as error:
                    report_failure("graduation", error, room=room, user=user_id)

    async def _read_only(self, room_id: str, user_id: str) -> None:
        state = await self._api.get_room_state(
            room_id, [(EventTypes.Create, ""), (EventTypes.PowerLevels, "")]
        )
        levels = state.get((EventTypes.PowerLevels, ""))
        content = read_only(
            unfreeze(levels.content) if levels is not None else {},
            user_id,
            power_level(state, user_id),
        )
        if content is None:
            return
        sender = await select_state_sender(self._api, room_id, EventTypes.PowerLevels)
        if sender is None:
            raise RuntimeError(f"no member can change the power levels of {room_id}")
        await self._api.create_and_send_event_into_room(
            {
                "type": EventTypes.PowerLevels,
                "room_id": room_id,
                "sender": sender,
                "state_key": "",
                "content": content,
            }
        )

    async def _unshare(self, room_id: str, user_id: str, course: str) -> None:
        """Remove the course's admins from the student's own analytics room."""
        state = await self._api.get_room_state(room_id, [(EventTypes.Create, "")])
        create = state.get((EventTypes.Create, ""))
        if create is None or create.sender != user_id:
            return
        if create.content.get("type") != ANALYTICS_ROOM_TYPE:
            return
        members = await self._api.get_room_state(room_id, [(EventTypes.Member, None)])
        for (_, member), event in members.items():
            if member == user_id:
                continue
            if event.content.get("membership") not in ("join", "invite"):
                continue
            if await self._admins.is_course_admin(course, member):
                await self._api.update_room_membership(
                    user_id, member, room_id, "leave"
                )
