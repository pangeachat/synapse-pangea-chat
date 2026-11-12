"""Which course an incident belongs to, and who may read a course's incidents.

An incident - a Tier 1 block, a Tier 2 verdict or a report - belongs to the
course that owns the chat it happened in: the chat itself when it is a course
space (a room carrying a `pangea.course_plan`), otherwise each course space
the chat names in a live `m.space.parent` that lists it back in a live
`m.space.child`. A chat no course owns, such as a direct message, belongs to
no course and reaches no admin. The chat's links are read as they stood at the
incident's position, so a chat moved to another course afterwards stays with
the course it was in.

A **course admin** of a space is a member currently joined at power level 100
or above; only they may read its incidents. The rule over a room's state is a
pure function (`is_course_admin_in`), tested without a homeserver.
"""

from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from synapse_pangea_chat.activity_session_previews.get_activity_session_previews import (
    has_valid_via,
)
from synapse_pangea_chat.course_member_emails.members import COURSE_ADMIN_POWER_LEVEL

COURSE_PLAN_EVENT_TYPE = "pangea.course_plan"
CREATE_EVENT_TYPE = "m.room.create"
POWER_LEVELS_EVENT_TYPE = "m.room.power_levels"
MEMBER_EVENT_TYPE = "m.room.member"
SPACE_PARENT_EVENT_TYPE = "m.space.parent"
SPACE_CHILD_EVENT_TYPE = "m.space.child"
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


#: State keys to read; a state key of None reads every key of that type.
StateKeys = Sequence[Tuple[str, Optional[str]]]

#: Reads `(room_id, keys, position)` -> those keys of the room's state AFTER
#: its last event at or before `position`. `{}` when the room had no events
#: yet at that position.
StateAtPosition = Callable[[str, StateKeys, int], Awaitable[StateMap]]


def links_child(state: StateMap, room_id: str) -> bool:
    """The space lists `room_id` in a live `m.space.child`."""
    link = state.get((SPACE_CHILD_EVENT_TYPE, room_id))
    return link is not None and has_valid_via(getattr(link, "content", None))


class ChatCourses:
    """Resolves the course that owns a chat AS OF a stream position.

    Every caller captures a position at the moment of the incident - the
    message's own stream ordering for Tier 2, the current maximum for a block
    or a report - so a lookup that runs late, or is retried, answers the same
    question.
    """

    def __init__(
        self, *, state_at: StateAtPosition, current_position: Callable[[], int]
    ) -> None:
        self._state_at = state_at
        self._current_position = current_position

    @classmethod
    def from_homeserver(cls, homeserver: Any) -> "ChatCourses":
        return cls(
            state_at=_state_at_position(homeserver),
            current_position=homeserver.get_datastores().main.get_room_max_stream_ordering,
        )

    def position_now(self) -> int:
        """The position to anchor an incident happening now.

        Owner-accepted limit (moderation.instructions.md, "The Safety page's
        record"): this is the contiguous persisted watermark, so with more
        than one event persister an unfinished lower position can hold it
        behind an already-committed link change, and the incident freezes
        the older course. Revisit if the homeserver gains a second event
        persister.
        """
        return int(self._current_position())

    async def for_room(self, room_id: str, position: int) -> Tuple[str, ...]:
        """The courses owning `room_id` at `position`, sorted; `()` for a chat
        no course owns. Raises when state cannot be read: an empty answer
        means "no course", and must never be what a failed read looks like."""
        state = await self._state_at(
            room_id,
            [(COURSE_PLAN_EVENT_TYPE, ""), (SPACE_PARENT_EVENT_TYPE, None)],
            position,
        )
        if is_course(state):
            return (room_id,)
        parents = sorted(
            key
            for (kind, key), event in state.items()
            if kind == SPACE_PARENT_EVENT_TYPE
            and has_valid_via(getattr(event, "content", None))
        )
        courses = []
        for parent_id in parents:
            space = await self._state_at(
                parent_id,
                [(COURSE_PLAN_EVENT_TYPE, ""), (SPACE_CHILD_EVENT_TYPE, room_id)],
                position,
            )
            if is_course(space) and links_child(space, room_id):
                courses.append(parent_id)
        return tuple(courses)


def _state_at_position(homeserver: Any) -> StateAtPosition:
    # Owner-accepted limits (moderation.instructions.md, "The Safety page's
    # record"), each with the condition that reopens it:
    # - A forked room graph (a late federated event on an older branch, or a
    #   soft-failed event) can still show a link that was removed at the last
    #   persisted event, so later incidents land on the former course.
    #   Revisit if the homeserver federates.
    # - A course plan redacted and then pruned by retention can no longer be
    #   read at an earlier position, so a late lookup lands on no course.
    #   Revisit if the product ever redacts a course plan; it does not today.
    after_event = state_after_event_reader(homeserver)

    async def _read(room_id: str, keys: StateKeys, position: int) -> StateMap:
        from synapse.types import RoomStreamToken

        store = homeserver.get_datastores().main
        last = await store.get_last_event_id_in_room_before_stream_ordering(
            room_id, RoomStreamToken(stream=position)
        )
        if last is None:
            # The room had no events yet, or this server holds none of it.
            return {}
        return await after_event(last, keys)

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
) -> Callable[[str, StateKeys], Awaitable[StateMap]]:
    """Reads the given keys from the state AFTER an event."""

    async def _read(event_id: str, keys: StateKeys) -> StateMap:
        from synapse.types.state import StateFilter

        controller = homeserver.get_storage_controllers().state
        ids = await controller.get_state_ids_for_event(
            event_id, StateFilter.from_types(keys)
        )
        # Only the plan is read as sent; a link redacted before the incident
        # must read as removed, so the rest are read as redacted.
        plan = {k: v for k, v in ids.items() if k[0] == COURSE_PLAN_EVENT_TYPE}
        rest = {k: v for k, v in ids.items() if k[0] != COURSE_PLAN_EVENT_TYPE}
        return {
            **await _load(homeserver, plan, as_sent=True),
            **await _load(homeserver, rest),
        }

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

    What it cannot do is read a course plan that Synapse's retention has
    physically pruned after its redaction: an incident whose course is looked
    up after that prune lands on no course. Owner-accepted
    (.github/instructions/moderation.instructions.md, Decisions); revisit if
    the product ever redacts a course plan, which it does not today.
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
