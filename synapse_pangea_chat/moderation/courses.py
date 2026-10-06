"""Which courses an incident belongs to, and who may read a course's incidents.

A user's **student courses** are the course spaces - rooms carrying a
`pangea.course_plan` state event - that they are joined to below power level
100, excluding exempt senders and bots. An incident belongs to its subject's
student courses, captured when it happens:

- Tier 1: at block time.
- Tier 2: when the message is queued, and carried on the job, so a learner
  who leaves before the verdict still lands on their course.
- Report: at report time. The subject's student courses, or, only when the
  subject is a student nowhere, the reporter's.

A **course admin** of a space is a member currently joined at power level 100
or above; only they may read its incidents.

The rule over a room's state is a pure function, `student_level`, so it is
tested without a homeserver; the class around it only reads state.
"""

from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Set, Tuple

from synapse_pangea_chat.bot_user_ids import is_probable_bot_user_id
from synapse_pangea_chat.course_member_emails.members import (
    COURSE_ADMIN_POWER_LEVEL,
    effective_power_levels,
)

COURSE_PLAN_EVENT_TYPE = "pangea.course_plan"
CREATE_EVENT_TYPE = "m.room.create"
POWER_LEVELS_EVENT_TYPE = "m.room.power_levels"
MEMBER_EVENT_TYPE = "m.room.member"
JOIN = "join"

#: `(event_type, state_key) -> event`, as far as the rule reads one: `content`,
#: `sender`, and `room_version` on the create event.
StateMap = Mapping[Tuple[str, str], Any]

#: Reads the state of a room - current, or at a point in its history - for the
#: given user's membership and the course keys.
StateReader = Callable[[str, str], Awaitable[StateMap]]


def is_course(state: StateMap) -> bool:
    """A room is a course space when it carries a non-empty course plan."""
    plan = state.get((COURSE_PLAN_EVENT_TYPE, ""))
    if plan is None:
        return False
    content = getattr(plan, "content", None)
    return isinstance(content, Mapping) and len(content) > 0


def power_level(state: StateMap, user_id: str) -> int:
    """`user_id`'s power level as the room enforces it, creators included."""
    create_event = state.get((CREATE_EVENT_TYPE, ""))
    creators: Set[str] = set()
    if create_event is not None and getattr(
        getattr(create_event, "room_version", None),
        "msc4289_creator_power_enabled",
        False,
    ):
        additional = create_event.content.get("additional_creators", [])
        if isinstance(additional, list):
            creators.update(c for c in additional if isinstance(c, str))
        creators.add(create_event.sender)
    power_event = state.get((POWER_LEVELS_EVENT_TYPE, ""))
    levels = effective_power_levels(
        [user_id], power_event.content if power_event is not None else None, creators
    )
    return levels[user_id]


def is_joined(state: StateMap, user_id: str) -> bool:
    member = state.get((MEMBER_EVENT_TYPE, user_id))
    if member is None:
        return False
    content = getattr(member, "content", None)
    return isinstance(content, Mapping) and content.get("membership") == JOIN


def is_student_in(state: StateMap, user_id: str) -> bool:
    """Joined to a course space below power level 100."""
    return (
        is_course(state)
        and is_joined(state, user_id)
        and power_level(state, user_id) < COURSE_ADMIN_POWER_LEVEL
    )


def is_course_admin_in(state: StateMap, user_id: str) -> bool:
    """Joined to a course space at power level 100 or above."""
    return (
        is_course(state)
        and is_joined(state, user_id)
        and power_level(state, user_id) >= COURSE_ADMIN_POWER_LEVEL
    )


def state_keys_for(user_id: str) -> List[Tuple[str, str]]:
    return [
        (COURSE_PLAN_EVENT_TYPE, ""),
        (CREATE_EVENT_TYPE, ""),
        (POWER_LEVELS_EVENT_TYPE, ""),
        (MEMBER_EVENT_TYPE, user_id),
    ]


class StudentCourses:
    """Resolves a user's student courses from current state."""

    def __init__(
        self,
        *,
        rooms_for_user: Callable[[str], Awaitable[Iterable[str]]],
        read_state: StateReader,
        is_exempt: Callable[[str], bool],
    ) -> None:
        self._rooms_for_user = rooms_for_user
        self._read_state = read_state
        self._is_exempt = is_exempt

    @classmethod
    def from_homeserver(
        cls, homeserver: Any, is_exempt: Callable[[str], bool]
    ) -> "StudentCourses":
        store = homeserver.get_datastores().main
        return cls(
            rooms_for_user=store.get_rooms_for_user,
            read_state=current_state_reader(homeserver),
            is_exempt=is_exempt,
        )

    def excluded(self, user_id: str) -> bool:
        """Exempt senders and bots are nobody's students."""
        return self._is_exempt(user_id) or is_probable_bot_user_id(user_id)

    async def for_user(self, user_id: str) -> Tuple[str, ...]:
        """The user's student courses now, sorted. Raises when they cannot be
        read: an empty answer means "a student nowhere", and must never be
        what a failed read looks like."""
        if self.excluded(user_id):
            return ()
        rooms = await self._rooms_for_user(user_id)
        courses = []
        for room_id in sorted(rooms):
            state = await self._read_state(room_id, user_id)
            if is_student_in(state, user_id):
                courses.append(room_id)
        return tuple(courses)

    async def for_report(self, subject_id: str, reporter_id: str) -> Tuple[str, ...]:
        """The subject's student courses; the reporter's only when the
        subject is a student nowhere. So a report about a course's learner
        reaches that course and none of the reporter's others."""
        subject_courses = await self.for_user(subject_id)
        if subject_courses:
            return subject_courses
        return await self.for_user(reporter_id)


def current_state_reader(homeserver: Any) -> StateReader:
    """Reads the course keys and one member from a room's CURRENT state."""

    async def _read(room_id: str, user_id: str) -> StateMap:
        from synapse.types.state import StateFilter

        controller = homeserver.get_storage_controllers().state
        ids = await controller.get_current_state_ids(
            room_id, StateFilter.from_types(state_keys_for(user_id))
        )
        if (COURSE_PLAN_EVENT_TYPE, "") not in ids:
            # Not a course: nothing else in the room is needed.
            return {}
        return await _load(homeserver, ids)

    return _read


def state_after_event_reader(
    homeserver: Any,
) -> Callable[[str, str], Awaitable[StateMap]]:
    """Reads the course keys and one member from the state AFTER an event."""

    async def _read(event_id: str, user_id: str) -> StateMap:
        from synapse.types.state import StateFilter

        controller = homeserver.get_storage_controllers().state
        ids = await controller.get_state_ids_for_event(
            event_id, StateFilter.from_types(state_keys_for(user_id))
        )
        if (COURSE_PLAN_EVENT_TYPE, "") not in ids:
            return {}
        return await _load(homeserver, ids)

    return _read


async def _load(homeserver: Any, ids: Mapping[Tuple[str, str], str]) -> StateMap:
    store = homeserver.get_datastores().main
    events = await store.get_events(list(ids.values()))
    result: Dict[Tuple[str, str], Any] = {}
    for key, event_id in ids.items():
        event = events.get(event_id)
        if event is not None:
            result[key] = event
    return result
