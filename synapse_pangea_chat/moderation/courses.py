"""Which courses an incident belongs to, and who may read a course's incidents.

A user's **student courses** are the course spaces - rooms carrying a
`pangea.course_plan` state event - that they are joined to below power level
100, excluding exempt senders and bots. An incident belongs to its subject's
student courses, captured when it happens:

- Tier 1: at block time.
- Tier 2: at the message's own stream position, carried on the job from the
  moment it is queued, so a learner who leaves before the verdict still lands
  on their course.
- Report: at report time. The subject's student courses, or, only when the
  subject is a student nowhere, the reporter's.

A **course admin** of a space is a member currently joined at power level 100
or above; only they may read its incidents.

The rules over a room's state are pure functions (`is_student_in`,
`is_course_admin_in`), so they are tested without a homeserver; the class
around them only reads state.
"""

from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Tuple

from synapse_pangea_chat.bot_user_ids import is_probable_bot_user_id
from synapse_pangea_chat.course_member_emails.members import COURSE_ADMIN_POWER_LEVEL

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
    """`user_id`'s power level exactly as the room enforces it.

    Synapse's own `event_auth.get_user_power_level`, not a re-implementation:
    which user counts as a room's creator, and what a creator holds, differ
    by room version - the create event's `sender` or its `content.creator`,
    100 or unlimited - and a copy of that rule was wrong in both directions,
    granting a course admin's access to a power-0 member of an older room and
    reading its real creator as a student. A state with no create event is
    no room the rule can be applied to, and reads as level 0, which grants
    nothing on the read endpoint.
    """
    from synapse.event_auth import get_user_power_level

    if state.get((CREATE_EVENT_TYPE, "")) is None:
        return 0
    auth_events = {
        key: state[key]
        for key in ((CREATE_EVENT_TYPE, ""), (POWER_LEVELS_EVENT_TYPE, ""))
        if key in state
    }
    return int(get_user_power_level(user_id, auth_events))


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


#: Reads `(room_id, user_id, position)` -> the course keys and that user's
#: membership in the room's state AFTER its last event at or before
#: `position`. `{}` when the room had no events yet at that position.
StateAtPosition = Callable[[str, str, int], Awaitable[StateMap]]


class StudentCourses:
    """Resolves a user's student courses AS OF a stream position.

    **Anchored to a position, not to the moment the lookup runs.** The rule
    is "the courses the sender was a student of when the incident happened",
    and a lookup that read current state would answer "when the lookup ran"
    instead: a learner who left course A and joined course B between the
    incident and the read would land on B. So every caller captures a
    position at the moment of the incident - the message's own stream
    ordering for Tier 2, the current maximum for a block or a report - and
    the state read is the state of each course space after its last event at
    or before that position. A lookup that fails and is retried later answers
    the same question.

    The candidates are the rooms the user has ever joined that carry a course
    plan now, plus any room this server no longer holds current state for -
    the state at the position decides those. A course plan is set when a
    course space is created and never removed, so the current-state filter
    only saves reading every DM and activity room's history.
    """

    def __init__(
        self,
        *,
        rooms_ever_joined: Callable[[str], Awaitable[Iterable[str]]],
        is_course_now: Callable[[str], Awaitable[bool]],
        state_at: StateAtPosition,
        current_position: Callable[[], int],
        is_exempt: Callable[[str], bool],
    ) -> None:
        self._rooms_ever_joined = rooms_ever_joined
        self._is_course_now = is_course_now
        self._state_at = state_at
        self._current_position = current_position
        self._is_exempt = is_exempt

    @classmethod
    def from_homeserver(
        cls, homeserver: Any, is_exempt: Callable[[str], bool]
    ) -> "StudentCourses":
        store = homeserver.get_datastores().main
        return cls(
            rooms_ever_joined=store.get_rooms_user_has_been_in,
            is_course_now=_is_course_now(homeserver),
            state_at=_state_at_position(homeserver),
            current_position=store.get_room_max_stream_ordering,
            is_exempt=is_exempt,
        )

    def excluded(self, user_id: str) -> bool:
        """Exempt senders and bots are nobody's students."""
        return self._is_exempt(user_id) or is_probable_bot_user_id(user_id)

    def position_now(self) -> int:
        """The position to anchor an incident happening now."""
        return int(self._current_position())

    async def for_user(self, user_id: str, position: int) -> Tuple[str, ...]:
        """The user's student courses at `position`, sorted. Raises when they
        cannot be read: an empty answer means "a student nowhere", and must
        never be what a failed read looks like."""
        if self.excluded(user_id):
            return ()
        rooms = await self._rooms_ever_joined(user_id)
        courses = []
        for room_id in sorted(rooms):
            if not await self._is_course_now(room_id):
                continue
            state = await self._state_at(room_id, user_id, position)
            if is_student_in(state, user_id):
                courses.append(room_id)
        return tuple(courses)

    async def for_report(
        self, subject_id: str, reporter_id: str, position: int
    ) -> Tuple[str, ...]:
        """The subject's student courses; the reporter's only when the
        subject is a student nowhere. So a report about a course's learner
        reaches that course and none of the reporter's others."""
        subject_courses = await self.for_user(subject_id, position)
        if subject_courses:
            return subject_courses
        return await self.for_user(reporter_id, position)


def _is_course_now(homeserver: Any) -> Callable[[str], Awaitable[bool]]:
    async def _check(room_id: str) -> bool:
        from synapse.types.state import StateFilter

        controller = homeserver.get_storage_controllers().state
        ids = await controller.get_current_state_ids(
            room_id,
            StateFilter.from_types(
                [(COURSE_PLAN_EVENT_TYPE, ""), (CREATE_EVENT_TYPE, "")]
            ),
        )
        if (CREATE_EVENT_TYPE, "") not in ids:
            # This server holds no current state for the room - every local
            # member has left, and Synapse drops current state then - so it
            # cannot say the room is not a course. It stays a candidate, and
            # the state AT the position decides.
            return True
        return (COURSE_PLAN_EVENT_TYPE, "") in ids

    return _check


def _state_at_position(homeserver: Any) -> StateAtPosition:
    after_event = state_after_event_reader(homeserver)

    async def _read(room_id: str, user_id: str, position: int) -> StateMap:
        from synapse.types import RoomStreamToken

        store = homeserver.get_datastores().main
        last = await store.get_last_event_id_in_room_before_stream_ordering(
            room_id, RoomStreamToken(stream=position)
        )
        if last is None:
            # The room had no events yet: nobody was a member of it.
            return {}
        return await after_event(last, user_id)

    return _read


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
        return await _load(homeserver, ids, as_sent=True)

    return _read


async def _load(
    homeserver: Any, ids: Mapping[Tuple[str, str], str], *, as_sent: bool = False
) -> StateMap:
    """The state events behind `ids`.

    `as_sent` reads them as they were when sent, not as a later redaction
    left them. A historical lookup needs that: a course plan redacted after
    the incident - replaced by a new one, say - was in force at the
    incident's position, and pruned to an empty content it would read as no
    course at all, moving the incident off the course it belonged to.
    """
    from synapse.storage.databases.main.events_worker import EventRedactBehaviour

    store = homeserver.get_datastores().main
    events = await store.get_events(
        list(ids.values()),
        redact_behaviour=(
            EventRedactBehaviour.as_is if as_sent else EventRedactBehaviour.redact
        ),
    )
    result: Dict[Tuple[str, str], Any] = {}
    for key, event_id in ids.items():
        event = events.get(event_id)
        if event is not None:
            result[key] = event
    return result
