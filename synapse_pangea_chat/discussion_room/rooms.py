"""What a discussion room is, who holds a seat in it and who counts toward
its students. Shared by the endpoint, the join check and the admin mirror.

Design: discussion-room.instructions.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

from synapse.api.constants import EventTypes

from synapse_pangea_chat.activity_session_previews.get_activity_session_previews import (
    has_valid_via,
)
from synapse_pangea_chat.course_session_admins.course_session_admins import (
    ACTIVITY_SESSION_ROOM_TYPE,
)
from synapse_pangea_chat.room_preview.constants import (
    PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE,
    PANGEA_ACTIVITY_ROLE_STATE_EVENT_TYPE,
)

ACTIVITY_PLAN_EVENT = PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE
ACTIVITY_ROLES_EVENT = PANGEA_ACTIVITY_ROLE_STATE_EVENT_TYPE
ACTIVITY_SUMMARY_EVENT = "pangea.activity_summary"

# The creation-content key that marks a discussion room. It is trusted only on
# a room the discussion bot created, since any client can set creation content.
DISCUSSION_MARKER = "pangea.discussion"
# The plan mode, compared case-insensitively, and written on the room's plan
# reference so readers can tell the mode before the plan loads.
DISCUSSION_MODE = "discussion"
# The plan's one shared role; every student's seat is on it.
DISCUSSION_ROLE_ID = "participant"
DISCUSSION_ROLE_NAME = "Participant"

BOT_POWER_LEVEL = 100
# Course admins moderate students but cannot touch the bot or power levels.
ADMIN_POWER_LEVEL = 50

MEMBERSHIP_JOIN = "join"
MEMBERSHIP_LEAVE = "leave"
MEMBERSHIP_BAN = "ban"


@dataclass(frozen=True)
class DiscussionMarker:
    course_id: str
    activity_id: str


def session_room_type(activity_id: str) -> str:
    return f"{ACTIVITY_SESSION_ROOM_TYPE}:{activity_id}"


def creation_content(course_id: str, activity_id: str) -> Dict[str, Any]:
    return {
        "type": session_room_type(activity_id),
        DISCUSSION_MARKER: {"course_id": course_id, "activity_id": activity_id},
    }


def discussion_marker(
    create_sender: Optional[str],
    create_content: Optional[Mapping[str, Any]],
    bot_user_id: Optional[str],
) -> Optional[DiscussionMarker]:
    """The room's course and discussion when the room is a discussion room:
    created by the discussion bot, with the marker naming the same activity
    as the session room type. None for every other room, including a room
    a student made to look like one."""
    if not bot_user_id or create_sender != bot_user_id or create_content is None:
        return None
    marker = create_content.get(DISCUSSION_MARKER)
    if not isinstance(marker, Mapping):
        return None
    course_id = marker.get("course_id")
    activity_id = marker.get("activity_id")
    if not (isinstance(course_id, str) and course_id):
        return None
    if not (isinstance(activity_id, str) and activity_id):
        return None
    if create_content.get("type") != session_room_type(activity_id):
        return None
    return DiscussionMarker(course_id, activity_id)


def is_discussion_plan(plan: Mapping[str, Any]) -> bool:
    """A plan is a discussion only in the Discussion mode (any case) with
    exactly one role, ``participant``: old free-text plans whose mode says
    "Discussion" are shaped like roleplays and are not."""
    mode = plan.get("mode")
    if not isinstance(mode, str) or mode.strip().casefold() != DISCUSSION_MODE:
        return False
    roles = plan.get("roles")
    if isinstance(roles, Mapping):
        return list(roles.keys()) == [DISCUSSION_ROLE_ID]
    if isinstance(roles, list) and len(roles) == 1 and isinstance(roles[0], Mapping):
        role = roles[0]
        # A CMS row's array items carry Payload's own row `id`, so the role
        # id is `role_id` there; `id` is the role id only in other shapes.
        return (role.get("role_id") or role.get("id")) == DISCUSSION_ROLE_ID
    return False


# --- Seats ------------------------------------------------------------------


def seat_key(user_id: str) -> str:
    """A student's seat in ``pangea.activity_roles``: keyed by student, since
    every student holds the same role."""
    return f"{DISCUSSION_ROLE_ID}:{user_id}"


def _roles(content: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    roles = (content or {}).get("roles")
    return dict(roles) if isinstance(roles, Mapping) else {}


def seat_holders(roles_content: Optional[Mapping[str, Any]]) -> FrozenSet[str]:
    return frozenset(
        seat["user_id"]
        for seat in _roles(roles_content).values()
        if isinstance(seat, Mapping) and isinstance(seat.get("user_id"), str)
    )


def find_seat(
    roles_content: Optional[Mapping[str, Any]], user_id: str
) -> Optional[Tuple[str, Dict[str, Any]]]:
    for key, seat in _roles(roles_content).items():
        if isinstance(seat, Mapping) and seat.get("user_id") == user_id:
            return key, dict(seat)
    return None


def with_seat(
    roles_content: Optional[Mapping[str, Any]], user_id: str
) -> Dict[str, Any]:
    """The roles content with ``user_id``'s seat added; an existing seat for
    them (finished or not) is kept as it is."""
    content = dict(roles_content or {})
    roles = _roles(content)
    content["roles"] = roles
    if find_seat(content, user_id) is None:
        roles[seat_key(user_id)] = {
            "id": DISCUSSION_ROLE_ID,
            "user_id": user_id,
            "role": DISCUSSION_ROLE_NAME,
        }
    return content


def without_seat(
    roles_content: Optional[Mapping[str, Any]], user_id: str
) -> Dict[str, Any]:
    content = dict(roles_content or {})
    content["roles"] = {
        key: seat
        for key, seat in _roles(content).items()
        if not (isinstance(seat, Mapping) and seat.get("user_id") == user_id)
    }
    return content


def with_seat_changed(
    roles_content: Optional[Mapping[str, Any]],
    key: str,
    seat: Mapping[str, Any],
) -> Dict[str, Any]:
    content = dict(roles_content or {})
    roles = _roles(content)
    roles[key] = dict(seat)
    content["roles"] = roles
    return content


def ended_seat(seat: Mapping[str, Any], now: str) -> Dict[str, Any]:
    """The seat ended now; an end already recorded keeps its time."""
    ended = dict(seat)
    ended["finished_at"] = seat.get("finished_at") or now
    ended["archived_at"] = seat.get("archived_at") or now
    return ended


def continued_seat(seat: Mapping[str, Any]) -> Dict[str, Any]:
    reopened = dict(seat)
    reopened["finished_at"] = None
    reopened["archived_at"] = None
    return reopened


def utc_timestamp(seconds: float) -> str:
    """An instant in the client's own form (Dart ``toUtc().toIso8601String()``,
    millisecond precision, trailing Z), so both writers store one format."""
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


# --- A room as placement sees it ----------------------------------------------


@dataclass(frozen=True)
class Member:
    membership: str
    # A leave sent by someone else: the member was removed, not gone of their
    # own accord.
    kicked: bool


@dataclass(frozen=True)
class RoomView:
    room_id: str
    created_ts: int
    bot_joined: bool
    members: Mapping[str, Member]
    holders: FrozenSet[str]
    bot_user_id: str

    def joined(self, user_id: str) -> bool:
        member = self.members.get(user_id)
        return member is not None and member.membership == MEMBERSHIP_JOIN

    def refuses(self, user_id: str) -> bool:
        """Banned or removed here: placement never puts them back."""
        member = self.members.get(user_id)
        return member is not None and (
            member.membership == MEMBERSHIP_BAN
            or (member.membership == MEMBERSHIP_LEAVE and member.kicked)
        )

    def is_own(self, user_id: str) -> bool:
        """The caller's room: they hold a seat and are joined, not yet joined,
        or left it of their own accord, so their next tap takes them back."""
        if user_id not in self.holders:
            return False
        member = self.members.get(user_id)
        return (
            member is None
            or member.membership == MEMBERSHIP_JOIN
            or (member.membership == MEMBERSHIP_LEAVE and not member.kicked)
        )

    @property
    def students(self) -> int:
        """Seats that count toward capacity: held by a joined member, or
        written ahead of a join still on its way. A seat whose holder left,
        was removed or was banned does not count; neither does a member
        without a seat, such as a course admin reading along."""
        return sum(
            1
            for user_id in self.holders
            if user_id != self.bot_user_id
            and (user_id not in self.members or self.joined(user_id))
        )


def room_view(
    room_id: str,
    state: Mapping[Tuple[str, str], Any],
    bot_user_id: str,
    created_ts: int,
) -> RoomView:
    members: Dict[str, Member] = {}
    for (event_type, state_key), event in state.items():
        if event_type != EventTypes.Member or not isinstance(state_key, str):
            continue
        membership = event.content.get("membership")
        if not isinstance(membership, str):
            continue
        members[state_key] = Member(
            membership=membership,
            kicked=membership == MEMBERSHIP_LEAVE and event.sender != state_key,
        )
    roles = state.get((ACTIVITY_ROLES_EVENT, ""))
    bot = members.get(bot_user_id)
    return RoomView(
        room_id=room_id,
        created_ts=created_ts,
        bot_joined=bot is not None and bot.membership == MEMBERSHIP_JOIN,
        members=members,
        holders=seat_holders(roles.content if roles is not None else None),
        bot_user_id=bot_user_id,
    )


def pick_room(student_counts: List[Tuple[str, int]], capacity: int) -> Optional[str]:
    """The fullest room with space, so conversations fill up rather than
    spread thin; ties go to the room listed first."""
    open_rooms = [
        (count, i, room)
        for i, (room, count) in enumerate(student_counts)
        if count < capacity
    ]
    if not open_rooms:
        return None
    open_rooms.sort(key=lambda r: (-r[0], r[1]))
    return open_rooms[0][2]


def discussion_power_levels(bot_user_id: str, admins: FrozenSet[str]) -> Dict[str, Any]:
    """The bot at 100 and never lowered, so no one can remove it; course
    admins at 50, enough to remove or ban a student; students at 0. Only the
    server (as the bot) writes the plan and the seats. Differs from the
    client's roleplay ``RoomDefaults`` on purpose: no student owns the room."""
    users: Dict[str, int] = {admin: ADMIN_POWER_LEVEL for admin in admins}
    users[bot_user_id] = BOT_POWER_LEVEL
    return {
        "ban": 50,
        "kick": 50,
        "invite": 50,
        "redact": 50,
        "events": {
            ACTIVITY_PLAN_EVENT: 100,
            ACTIVITY_ROLES_EVENT: 100,
            # The client still writes the room's summary slot.
            ACTIVITY_SUMMARY_EVENT: 0,
            "m.room.power_levels": 100,
            "m.room.pinned_events": 50,
        },
        "events_default": 0,
        "state_default": 50,
        "users_default": 0,
        "notifications": {"room": 50},
        "users": users,
    }


def mirrored_admin_levels(
    users: Mapping[str, Any], bot_user_id: str, admins: FrozenSet[str]
) -> Optional[Dict[str, Any]]:
    """The room's ``users`` with the course's admins at 50 and former admins
    removed, or None when nothing changes. Only entries at exactly 50 are
    admin entries; the bot and any other level are left alone."""
    updated = dict(users)
    for admin in admins:
        level = updated.get(admin)
        if admin != bot_user_id and (
            not isinstance(level, int) or level < ADMIN_POWER_LEVEL
        ):
            updated[admin] = ADMIN_POWER_LEVEL
    for user_id, level in list(updated.items()):
        if (
            user_id != bot_user_id
            and level == ADMIN_POWER_LEVEL
            and user_id not in admins
        ):
            del updated[user_id]
    return updated if updated != dict(users) else None


# --- Finding a course's discussion rooms --------------------------------------

_DISCUSSION_ROOMS_SQL = """
    SELECT link.state_key, link_json.json, create_json.json
    FROM current_state_events AS link
    JOIN rooms AS r ON r.room_id = link.state_key
    JOIN current_state_events AS cr
        ON cr.room_id = link.state_key AND cr.type = ? AND cr.state_key = ''
    JOIN event_json AS link_json ON link_json.event_id = link.event_id
    JOIN event_json AS create_json ON create_json.event_id = cr.event_id
    WHERE link.room_id = ? AND link.type = ? AND r.creator = ?
"""


@dataclass(frozen=True)
class DiscussionRoomRef:
    room_id: str
    activity_id: str
    created_ts: int


def _decoded(value: Any) -> Any:
    return json.loads(value) if isinstance(value, (str, bytes)) else value


async def course_discussion_rooms(
    store: Any,
    course_id: str,
    bot_user_id: str,
    activity_id: Optional[str] = None,
) -> List[DiscussionRoomRef]:
    """The discussion rooms linked into a course, oldest first, optionally
    for one discussion. One query over the rooms the bot created among the
    course's children, so a course's roleplay history costs nothing."""
    rows = await store.db_pool.execute(
        "discussion_room_course_rooms",
        _DISCUSSION_ROOMS_SQL,
        EventTypes.Create,
        course_id,
        EventTypes.SpaceChild,
        bot_user_id,
    )
    found: List[DiscussionRoomRef] = []
    for room_id, link_json, create_json in rows:
        link = _decoded(link_json)
        create = _decoded(create_json)
        if not isinstance(link, Mapping) or not has_valid_via(link.get("content")):
            continue
        if not isinstance(create, Mapping):
            continue
        marker = discussion_marker(
            create.get("sender"), create.get("content"), bot_user_id
        )
        if marker is None or marker.course_id != course_id:
            continue
        if activity_id is not None and marker.activity_id != activity_id:
            continue
        created_ts = create.get("origin_server_ts")
        found.append(
            DiscussionRoomRef(
                room_id=room_id,
                activity_id=marker.activity_id,
                created_ts=created_ts if isinstance(created_ts, int) else 0,
            )
        )
    found.sort(key=lambda ref: (ref.created_ts, ref.room_id))
    return found
