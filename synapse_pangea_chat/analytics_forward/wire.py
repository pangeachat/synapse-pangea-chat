"""What the forwarder sends, and what counts as an acknowledgement.

The ingest contract with admin-dash-api ``POST /api/internal/matrix-events``.
Its JSON schema and an example live in
``tests/fixtures/analytics_forward/ingest_contract.json``, which both sides
test against; a field changed here without changing that file fails
``tests/test_analytics_forward_unit.py``.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

#: The one room-timeline (non-state) type forwarded: a student's construct uses.
CONSTRUCT_TYPE = "pangea.construct"

#: State types forwarded from every room. The list the dashboard reads; the
#: inventory test in admin-dash-api (plan PR A2) is what fixes it, and a type
#: it finds missing is added here.
STATE_TYPES = frozenset(
    {
        # Membership, power and room identity (access, roster, ownership).
        "m.room.member",
        "m.room.power_levels",
        "m.room.create",
        "m.room.name",
        "m.room.topic",
        "m.room.avatar",
        "m.room.join_rules",
        "m.room.history_visibility",
        # How the app marks a course's Announcements chat (alias localpart
        # announcementsChat...).
        "m.room.canonical_alias",
        "m.space.child",
        "m.space.parent",
        # Course-space state.
        "pangea.course_plan",
        "pangea.course_missions",
        "pangea.course_settings",
        "pangea.teacher_mode",
        # Session-room state.
        "pangea.activity_plan",
        "pangea.activity_roles",
        "pangea.orchestrator_awarded_goals",
        # Analytics-room state.
        "pangea.analytics_settings",
        "pangea.activity_room_ids",
    }
)

FORWARDED_TYPES = frozenset(STATE_TYPES | {CONSTRUCT_TYPE})

SCHEMA_VERSION = 1


def is_forwarded(event_type: str, state_key: Optional[str]) -> bool:
    """A state type only as state, the construct type only as a timeline event."""
    if event_type == CONSTRUCT_TYPE:
        return state_key is None
    return event_type in STATE_TYPES and state_key is not None


def event_record(event: Any) -> Dict[str, Any]:
    """One event as the ingest takes it. ``stream_pos`` orders everything."""
    state_key = event.get_state_key() if event.is_state() else None
    return {
        "stream_pos": int(event.internal_metadata.stream_ordering),
        "event_id": event.event_id,
        "room_id": event.room_id,
        "type": event.type,
        "state_key": state_key,
        "sender": event.sender,
        "origin_server_ts": int(event.origin_server_ts),
        "content": dict(event.content),
    }


def profile_record(user_id: str, revision: int, profile: Any) -> Dict[str, Any]:
    """A user's whole current profile, ``None`` when the user has none."""
    return {
        "user_id": user_id,
        "revision": int(revision),
        "profile": dict(profile) if isinstance(profile, Mapping) else None,
    }


def ingest_body(
    *,
    events: Sequence[Dict[str, Any]],
    profiles: Sequence[Dict[str, Any]],
    delivered_through: int,
    profiles_clean_before: int,
    accept_join_pos: Optional[int],
    pending_accepts: Sequence[Any] = (),
    pending_accepts_rev: int = 0,
) -> Dict[str, Any]:
    owed = [
        {"room_id": item[0], "user_id": item[1]} if isinstance(item, tuple) else item
        for item in pending_accepts
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "events": list(events),
        "profiles": list(profiles),
        "delivered_through": int(delivered_through),
        "profiles_clean_before": int(profiles_clean_before),
        "accept_join_pos": accept_join_pos,
        "pending_accepts": owed,
        "pending_accepts_rev": int(pending_accepts_rev),
    }


class BadAck(Exception):
    """A 2xx whose body is not the ingest's acknowledgement of THIS batch."""


def check_ack(body: Any, delivered_through: int) -> int:
    """The number of events the ingest set aside as rejects.

    Raises :class:`BadAck` unless the body echoes this batch's
    ``delivered_through``: a 200 from anything but the ingest (a misrouted
    URL, a proxy's error page) must never move the cursor.
    """
    if not isinstance(body, dict):
        raise BadAck("acknowledgement is not a JSON object")
    echoed = body.get("delivered_through")
    if isinstance(echoed, bool) or echoed != delivered_through:
        raise BadAck("acknowledgement does not echo delivered_through")
    rejected = body.get("rejected", 0)
    if isinstance(rejected, bool) or not isinstance(rejected, int) or rejected < 0:
        raise BadAck("acknowledgement has an invalid rejected count")
    return rejected
