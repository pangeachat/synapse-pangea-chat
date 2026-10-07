"""The readable name of the room an incident happened in, for the Safety page.

A course admin cannot read `!abc:server`. So each incident snapshots a label
when it is written, like the rest of the row:

- the room's `m.room.name`, when it has a non-empty one;
- otherwise, for a direct chat - a room with exactly two joined members, the
  subject and one other - "Direct chat with <the other member's display
  name>", falling back to their Matrix ID when they have no display name;
- otherwise None, and the page shows what it has.

Read at the incident's stream position, from the room's state after its last
event at or before it, with state events as sent - the same anchoring the
course lookup uses - so a rename or a departure after the incident does not
rewrite the label, and the backfill reads the name each legacy incident had.

**Best effort, unlike the courses.** A label that cannot be read is None and
a WARNING, never a failed write: the record and the safety decision around it
must not wait on a display string. The courses decide who sees an incident;
the name only helps them read it.
"""

from typing import Any, Awaitable, Callable, Mapping, Optional, Tuple

from synapse_pangea_chat.moderation.compat import reraise_if_cancelled
from synapse_pangea_chat.moderation.log_safety import error_site, scrubbing_logger

logger = scrubbing_logger("synapse.modules.synapse_pangea_chat.moderation.room_names")

NAME_EVENT_TYPE = "m.room.name"
MEMBER_EVENT_TYPE = "m.room.member"
JOIN = "join"

#: `(event_type, state_key) -> event` for the room's name and its members.
NameState = Mapping[Tuple[str, str], Any]

#: `(room_id, position) -> NameState`; `{}` when the room had no events yet.
NameStateReader = Callable[[str, int], Awaitable[NameState]]


def label_from_state(state: NameState, subject_id: Optional[str]) -> Optional[str]:
    """The rule above, over one room's state."""
    name_event = state.get((NAME_EVENT_TYPE, ""))
    if name_event is not None:
        content = getattr(name_event, "content", None)
        name = content.get("name") if isinstance(content, Mapping) else None
        if isinstance(name, str) and name.strip():
            return name
    joined = {}
    for (event_type, state_key), event in state.items():
        if event_type != MEMBER_EVENT_TYPE:
            continue
        content = getattr(event, "content", None)
        if isinstance(content, Mapping) and content.get("membership") == JOIN:
            joined[state_key] = content
    if subject_id is None or subject_id not in joined or len(joined) != 2:
        return None
    other_id = next(user for user in joined if user != subject_id)
    display = joined[other_id].get("displayname")
    if not isinstance(display, str) or not display.strip():
        display = other_id
    return f"Direct chat with {display}"


class RoomNames:
    def __init__(
        self, *, read_state: NameStateReader, current_position: Callable[[], int]
    ) -> None:
        self._read_state = read_state
        self._current_position = current_position

    @classmethod
    def from_homeserver(cls, homeserver: Any) -> "RoomNames":
        store = homeserver.get_datastores().main
        return cls(
            read_state=_state_at_position(homeserver),
            current_position=lambda: int(store.get_room_max_stream_ordering()),
        )

    async def label(
        self, room_id: str, subject_id: Optional[str], position: Optional[int]
    ) -> Optional[str]:
        """The label at `position` (now, when None), or None. Never raises
        other than to let a cancellation past."""
        try:
            at = self._current_position() if position is None else position
            return label_from_state(await self._read_state(room_id, at), subject_id)
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok: a label is best effort, see the module docstring.
            logger.warning(
                "safety incident room name unreadable for %s at %s (%s)",
                room_id,
                error_site(exc),
                type(exc).__name__,
            )
            return None


def _state_at_position(homeserver: Any) -> NameStateReader:
    async def _read(room_id: str, position: int) -> NameState:
        from synapse.storage.databases.main.events_worker import (
            EventRedactBehaviour,
        )
        from synapse.types import RoomStreamToken
        from synapse.types.state import StateFilter

        store = homeserver.get_datastores().main
        last = await store.get_last_event_id_in_room_before_stream_ordering(
            room_id, RoomStreamToken(stream=position)
        )
        if last is None:
            return {}
        controller = homeserver.get_storage_controllers().state

        async def _load(state_filter: Any) -> NameState:
            ids = await controller.get_state_ids_for_event(last, state_filter)
            events = await store.get_events(
                list(ids.values()), redact_behaviour=EventRedactBehaviour.as_is
            )
            return {
                key: events[event_id]
                for key, event_id in ids.items()
                if event_id in events
            }

        named = await _load(StateFilter.from_types([(NAME_EVENT_TYPE, "")]))
        if label_from_state(named, None) is not None:
            return named
        # No usable name: the members decide whether it is a direct chat.
        return await _load(StateFilter.from_types([(MEMBER_EVENT_TYPE, None)]))

    return _read
