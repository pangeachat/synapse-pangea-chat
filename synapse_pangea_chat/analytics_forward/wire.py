"""What the forwarder sends, and what counts as an acknowledgement.

The ingest contract with admin-dash-api ``POST /api/internal/matrix-events``.
Its JSON schema and an example live in
``tests/fixtures/analytics_forward/ingest_contract.json``, which both sides
test against; a field changed here without changing that file fails
``tests/test_analytics_forward_unit.py``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

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
        # The session's summary (state_key = a language code); the dashboard's
        # session XP comes from it.
        "pangea.activity_summary",
        # Analytics-room state.
        "pangea.analytics_settings",
        "pangea.activity_room_ids",
        # A student's progress in a course (state_key = the course room id),
        # written by the app into their analytics room.
        "pangea.course_progress",
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


def incident_record(incident: Any, revision: int) -> Dict[str, Any]:
    """One safety incident as the ingest takes it (admin-dash#300): the read
    endpoint's shape plus the courses it is stored under, which the ingest
    uses only as a key and never serves, and the outbox revision."""
    record = dict(incident.to_json())
    record["course_ids"] = list(incident.course_ids or ())
    record["revision"] = int(revision)
    return record


def ingest_body(
    *,
    events: Sequence[Dict[str, Any]],
    profiles: Sequence[Dict[str, Any]],
    delivered_through: int,
    profiles_clean_before: int,
    accept_join_pos: Optional[int],
    pending_accepts: Sequence[Any] = (),
    pending_accepts_rev: int = 0,
    safety_incidents: Sequence[Dict[str, Any]] = (),
    safety_order: Optional[Tuple[int, int]] = None,
    safety_backlog: bool = False,
) -> Dict[str, Any]:
    """The batch. ``safety_backlog`` and ``safety_order`` go together, and only
    when ``safety_order`` is given; ``safety_incidents`` only when not empty."""
    owed = [
        {"room_id": item[0], "user_id": item[1]} if isinstance(item, tuple) else item
        for item in pending_accepts
    ]
    body: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "events": list(events),
        "profiles": list(profiles),
        "delivered_through": int(delivered_through),
        "profiles_clean_before": int(profiles_clean_before),
        "accept_join_pos": accept_join_pos,
        "pending_accepts": owed,
        "pending_accepts_rev": int(pending_accepts_rev),
    }
    if safety_incidents:
        body["safety_incidents"] = list(safety_incidents)
    if safety_order is not None:
        body["safety_backlog"] = bool(safety_backlog)
        body["safety_order"] = [int(safety_order[0]), int(safety_order[1])]
    return body


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


def check_safety_ack(body: Any, sent: Iterable[str]) -> Set[str]:
    """The ids among ``sent`` to park: every one the ingest named in
    ``safety_rejected`` (echoed exactly as sent), or EVERY sent one when it
    counted a reject it could not name (``safety_unnamed`` > 0); a re-send is a
    no-op at the ingest. Every other sent incident is stored.

    A batch that carried incidents must be answered with both fields: an
    answer without either cannot say what was stored, so it is no
    acknowledgement (``BadAck``) and the batch is sent again. A field that is
    present but malformed is refused whatever was sent. Ids the batch did not
    carry are ignored.
    """
    sent_ids = set(sent)
    if not isinstance(body, dict):
        raise BadAck("acknowledgement is not a JSON object")
    named = body.get("safety_rejected")
    unnamed = body.get("safety_unnamed")
    if named is not None and (
        not isinstance(named, list) or not all(isinstance(i, str) for i in named)
    ):
        raise BadAck("acknowledgement has an invalid safety_rejected list")
    if unnamed is not None and (
        isinstance(unnamed, bool) or not isinstance(unnamed, int) or unnamed < 0
    ):
        raise BadAck("acknowledgement has an invalid safety_unnamed count")
    if named is None or unnamed is None:
        if sent_ids:
            raise BadAck("acknowledgement does not account for the safety incidents")
        return set()
    if unnamed > 0:
        return sent_ids
    rejected: List[str] = [i for i in named if i in sent_ids]
    return set(rejected)
