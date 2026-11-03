"""``/_synapse/client/pangea/v1/discussion_room``: a student's room for one of
their course's discussions.

``POST {course_id, activity_id, avatar_url?}`` places the student and answers
``{room_id, created}``. ``GET ?course_id=&activity_id=`` answers where a
placement would put them now, without placing them: ``{room_id, students,
joined}``, ``room_id`` null meaning a new room. Both answer 403 to a caller who
is not a member of the course and 404 when the activity is not one of the
course's discussions.

The student is sent back to their own room for the discussion when they have
one; otherwise into the fullest room for it with fewer than
``discussion_room_capacity`` students, or a new room when none has space. The
server writes the student's seat, so two students arriving at once never
overfill a room or lose a seat. Design: org discussions.instructions.md
("Rooms").

A discussion room is an ordinary activity session room (its create type is
``p.activity.session:<activity id>`` and it carries ``pangea.activity_plan``),
so every activity-room reader works on it. What sets it apart is the seats:
every student holds their own seat on the plan's one shared role, keyed by
student rather than by role.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)
from urllib.parse import urlencode

from synapse.api.constants import EventTypes, RoomCreationPreset
from synapse.api.errors import (
    AuthError,
    InvalidClientTokenError,
    LimitExceededError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.client import read_body_with_max_size
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.module_api import ModuleApi
from synapse.types import create_requester
from synapse.util.async_helpers import Linearizer, timeout_deferred
from twisted.web.http_headers import Headers
from twisted.web.resource import Resource

from synapse_pangea_chat.activity_session_previews.get_activity_session_previews import (
    _activity_session_ids,
    _child_room_ids,
)
from synapse_pangea_chat.blocked_join_gate.blocked_join_gate import (
    server_initiated_join,
)
from synapse_pangea_chat.bot_user_ids import is_probable_bot_user_id
from synapse_pangea_chat.discussion_room.rate_limit import is_rate_limited
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.instructor_access import is_joined_instructor

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.discussion_room")

ACTIVITY_SESSION_TYPE_PREFIX = "p.activity.session:"
ACTIVITY_PLAN_EVENT = "pangea.activity_plan"
ACTIVITY_ROLES_EVENT = "pangea.activity_roles"
ACTIVITY_SUMMARY_EVENT = "pangea.activity_summary"
SPACE_CHILD_EVENT = "m.space.child"

DISCUSSION_MODE = "Discussion"
# The plan's one shared role; every student's seat is on it.
DISCUSSION_ROLE_ID = "participant"
DISCUSSION_ROLE_NAME = "Participant"

CMS_AUTH_COLLECTION = "service-users"
CMS_TIMEOUT_SECONDS = 5
CMS_MAX_BODY_BYTES = 1_000_000
# A start page polls its preview every few seconds; a discussion's plan is
# read from the CMS at most this often per course and discussion.
DISCUSSION_CACHE_SECONDS = 60

# Tries at joining a student past Synapse's per-room join limit, each waiting
# as long as Synapse asks, clamped: about ten seconds at its defaults.
JOIN_ATTEMPTS = 6
JOIN_WAIT_DEFAULT_MS = 1000
JOIN_WAIT_MIN_MS = 200
JOIN_WAIT_MAX_MS = 5000


class DiscussionNotFound(Exception):
    """The activity is not a discussion of this course."""


class CmsUnavailable(Exception):
    """The CMS could not be asked whether the activity is a discussion."""


class _Refused(Exception):
    """A request answered with a 4xx before any placement work."""

    def __init__(self, status: int, error: str, errcode: Optional[str] = None):
        super().__init__(error)
        self.status = status
        self.body: Dict[str, Any] = {"error": error}
        if errcode:
            self.body["errcode"] = errcode


@dataclass(frozen=True)
class _RoomView:
    room_id: str
    joined: FrozenSet[str]
    seated: FrozenSet[str]
    # Joined, or seated without ever having been a member: a seat written
    # ahead of its join holds its place, and keeps holding it if the join
    # fails, until the student's next try joins them.
    occupants: FrozenSet[str]
    students: int
    # Read only for a course admin, who is placed in the newest room.
    created_ts: int


def _roles(content: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    roles = (content or {}).get("roles")
    return dict(roles) if isinstance(roles, Mapping) else {}


def seated_user_ids(roles_content: Optional[Mapping[str, Any]]) -> Set[str]:
    return {
        seat["user_id"]
        for seat in _roles(roles_content).values()
        if isinstance(seat, Mapping) and isinstance(seat.get("user_id"), str)
    }


def seat_key(user_id: str) -> str:
    """A student's seat in ``pangea.activity_roles``: keyed by student, since
    every student holds the same role."""
    return f"{DISCUSSION_ROLE_ID}:{user_id}"


def with_seat(roles_content: Optional[Dict[str, Any]], user_id: str) -> Dict[str, Any]:
    """The roles content with ``user_id``'s seat added; an existing seat for
    them (finished or not) is kept as it is."""
    content = dict(roles_content or {})
    roles = _roles(content)
    content["roles"] = roles
    if user_id not in seated_user_ids(content):
        roles[seat_key(user_id)] = {
            "id": DISCUSSION_ROLE_ID,
            "user_id": user_id,
            "role": DISCUSSION_ROLE_NAME,
        }
    return content


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


def discussion_power_levels(
    creator: str, admins: FrozenSet[str], bot_user_id: Optional[str]
) -> Dict[str, Any]:
    """The activity room defaults (client ``RoomDefaults``), with the course's
    admins at 100, the bot at 50 and students at 0. The creator starts at 100
    so the server can finish setting the room up as them, then is lowered."""
    users: Dict[str, int] = {admin: 100 for admin in admins}
    if bot_user_id:
        users[bot_user_id] = 50
    users[creator] = 100
    return {
        "ban": 50,
        "kick": 50,
        "invite": 50,
        "redact": 50,
        "events": {
            ACTIVITY_PLAN_EVENT: 0,
            ACTIVITY_ROLES_EVENT: 0,
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


def _query_string(request: SynapseRequest, name: str) -> Optional[str]:
    values = request.args.get(name.encode()) if request.args else None
    return values[0].decode("utf-8", "replace") if values else None


def _nonempty(value: Any) -> Optional[str]:
    return (value.strip() or None) if isinstance(value, str) else None


class DiscussionRoom(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = api._hs.get_auth()
        self._store = api._hs.get_datastores().main
        self._clock = api._hs.get_clock()
        self._http = api.http_client
        # The bot's user id, read from user_activity_notification_bot_user_id:
        # it creates each room and writes seats ahead of their joins.
        self._bot_user_id = config.user_activity_notification_bot_user_id or None
        # (course_id, activity_id) -> (expires at, plan)
        self._plans: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
        # One placement at a time per course and discussion, so a room is
        # counted and its seat written by one request before the next looks.
        # Per process: a deploy that runs this endpoint on more than one
        # worker must route it to one, or move to the worker locks handler.
        self._locks = Linearizer("discussion_room", clock=self._clock)

    def render_GET(self, request: SynapseRequest):
        run_in_background(self._handle, request, preview=True)
        return server.NOT_DONE_YET

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._handle, request, preview=False)
        return server.NOT_DONE_YET

    async def _handle(self, request: SynapseRequest, *, preview: bool) -> None:
        action = "preview" if preview else "place"
        try:
            status, body = 200, await self._answer(request, preview)
        # silent-ok: the caller's own error, answered with its 4xx
        except _Refused as e:
            status, body = e.status, e.body
        # silent-ok: answered 404, a caller asking for an activity that is not
        # one of this course's discussions
        except DiscussionNotFound:
            status, body = 404, {"error": "Not a discussion of this course"}
        # silent-ok: answered 429, the room still refusing joins after every
        # retry; the student is asked to try again
        except LimitExceededError:
            logger.warning("discussion_room: joins still rate limited, answered busy")
            status = 429
            body = {"error": "Busy, try again", "errcode": "M_LIMIT_EXCEEDED"}
        except CmsUnavailable as e:
            logger.error("discussion_room %s: CMS unavailable: %s", action, e)
            status, body = 502, {"error": "Could not check the discussion"}
        except Exception:
            logger.exception("discussion_room %s failed", action)
            status, body = 500, {"error": "Internal server error"}
        respond_with_json(request, status, body, send_cors=True)

    async def _answer(self, request: SynapseRequest, preview: bool) -> Dict[str, Any]:
        try:
            requester = await self._auth.get_user_by_req(request)
        except (AuthError, InvalidClientTokenError, MissingClientTokenError) as e:
            logger.info("discussion_room: authentication failed: %s", e)
            raise _Refused(401, "Unauthorized", "M_UNAUTHORIZED") from e
        user_id = requester.user.to_string()
        if is_rate_limited(user_id, self._config, preview=preview):
            raise _Refused(429, "Rate limited")

        if preview:
            args: Any = {
                name: _query_string(request, name)
                for name in ("course_id", "activity_id")
            }
        else:
            args = await extract_body_json(request)
        if not isinstance(args, dict):
            args = {}
        course_id = _nonempty(args.get("course_id"))
        activity_id = _nonempty(args.get("activity_id"))
        if not (course_id and activity_id):
            raise _Refused(400, "'course_id' and 'activity_id' are required strings")

        membership, _ = await self._store.get_local_current_membership_for_user_in_room(
            user_id, course_id
        )
        if membership != "join":
            raise _Refused(403, "Not a member of this course")

        plan = await self._discussion_plan(course_id, activity_id)
        if preview:
            return await self._preview(user_id, course_id, activity_id)

        room_id, created = await self._place(
            user_id,
            course_id,
            activity_id,
            plan,
            _nonempty(args.get("avatar_url")),
        )
        logger.info(
            "discussion_room %s in %s: room=%s created=%s",
            activity_id,
            course_id,
            room_id,
            created,
        )
        return {"room_id": room_id, "created": created}

    async def _survey(
        self, user_id: str, course_id: str, activity_id: str
    ) -> Tuple[Optional[_RoomView], List[_RoomView], FrozenSet[str]]:
        """The discussion's rooms as placement sees them: the student's own
        room if they have one, else every room; and the course's admins.
        Shared by placing a student and previewing where they would go, so
        the two never disagree.

        A course admin is joined to every session room to read it
        (course_session_admins), so their own room is the one they hold a
        seat in, not any room they are in."""
        admins = await self._course_admins(course_id)
        is_admin = user_id in admins
        room_ids = await _activity_session_ids(
            await _child_room_ids([course_id], self._store), activity_id, self._store
        )
        rooms: List[_RoomView] = []
        for room_id in room_ids:
            room = await self._room_view(room_id, admins, with_created=is_admin)
            if user_id in room.occupants and (not is_admin or user_id in room.seated):
                return room, [], admins
            rooms.append(room)
        return None, rooms, admins

    async def _room_view(
        self, room_id: str, admins: FrozenSet[str], *, with_created: bool
    ) -> _RoomView:
        event_filter: List[Tuple[str, Optional[str]]] = [
            (EventTypes.Member, None),
            (ACTIVITY_ROLES_EVENT, ""),
        ]
        if with_created:
            event_filter.append((EventTypes.Create, ""))
        state = await self._api.get_room_state(room_id, event_filter=event_filter)
        memberships = {
            key: event.content.get("membership")
            for (event_type, key), event in state.items()
            if event_type == EventTypes.Member
        }
        joined = frozenset(k for k, m in memberships.items() if m == "join")
        roles = state.get((ACTIVITY_ROLES_EVENT, ""))
        seated = frozenset(seated_user_ids(roles.content if roles else None))
        occupants = joined | seated.difference(memberships)
        create = state.get((EventTypes.Create, ""))
        return _RoomView(
            room_id=room_id,
            joined=joined,
            seated=seated,
            occupants=occupants,
            students=sum(
                1
                for m in occupants
                if m not in admins and not is_probable_bot_user_id(m)
            ),
            created_ts=create.origin_server_ts if create is not None else 0,
        )

    def _choose(
        self, user_id: str, rooms: List[_RoomView], admins: FrozenSet[str]
    ) -> Optional[_RoomView]:
        """The room to put someone with no room of their own in: a student
        goes to the fullest room with space; a course admin, who never counts
        toward a room's students, to the newest room. None means a new room."""
        if user_id in admins:
            return max(rooms, key=lambda r: r.created_ts, default=None)
        by_id = {room.room_id: room for room in rooms}
        picked = pick_room(
            [(room.room_id, room.students) for room in rooms],
            self._config.discussion_room_capacity,
        )
        return by_id[picked] if picked is not None else None

    async def _preview(
        self, user_id: str, course_id: str, activity_id: str
    ) -> Dict[str, Any]:
        """Read-only and unlocked: a placement that runs between this and the
        student's tap places them by the rooms' state then, never by this
        answer."""
        own, rooms, admins = await self._survey(user_id, course_id, activity_id)
        room = own or self._choose(user_id, rooms, admins)
        if room is None:
            return {"room_id": None, "students": 0, "joined": False}
        return {
            "room_id": room.room_id,
            "students": room.students,
            "joined": own is not None and user_id in own.joined,
        }

    async def _place(
        self,
        user_id: str,
        course_id: str,
        activity_id: str,
        plan: Dict[str, Any],
        avatar_url: Optional[str],
    ) -> Tuple[str, bool]:
        # The join runs after the lock: it can wait out Synapse's join limit,
        # and the seat written under the lock already counts the student.
        async with self._locks.queue((course_id, activity_id)):
            room_id, created, joined = await self._reserve(
                user_id, course_id, activity_id, plan, avatar_url
            )
        if not joined:
            await self._join(room_id, user_id)
        return room_id, created

    async def _reserve(
        self,
        user_id: str,
        course_id: str,
        activity_id: str,
        plan: Dict[str, Any],
        avatar_url: Optional[str],
    ) -> Tuple[str, bool, bool]:
        """Chooses the student's room and writes their seat in it: (room,
        created, already joined). Their own room is chosen, ended or not."""
        own, rooms, admins = await self._survey(user_id, course_id, activity_id)
        room = own or self._choose(user_id, rooms, admins)
        if room is None:
            room_id = await self._create(
                user_id, course_id, activity_id, plan, admins, avatar_url
            )
            # Without the bot the student created the room, so is in it.
            return room_id, True, self._bot_user_id is None

        joined = user_id in room.joined
        if joined:
            sender = user_id
        elif self._bot_user_id in room.joined:
            sender = self._bot_user_id
        else:
            logger.warning(
                "discussion_room: bot not in %s, joining %s under the lock",
                room.room_id,
                user_id,
            )
            await self._join(room.room_id, user_id)
            sender, joined = user_id, True
        await self._ensure_seat(room.room_id, user_id, sender)
        return room.room_id, False, joined

    async def _join(self, room_id: str, user_id: str) -> None:
        """Join on the server's side, waiting out Synapse's per-room join
        limit: a class told to open the discussion at once arrives faster than
        it allows. Gives up after [JOIN_ATTEMPTS], raising its refusal, which
        the endpoint answers as busy."""
        for attempt in range(JOIN_ATTEMPTS):
            try:
                await server_initiated_join(self._api, room_id, user_id)
                return
            except LimitExceededError as e:
                if attempt == JOIN_ATTEMPTS - 1:
                    raise
                wait_ms = e.retry_after_ms or JOIN_WAIT_DEFAULT_MS
                wait_ms = min(max(wait_ms, JOIN_WAIT_MIN_MS), JOIN_WAIT_MAX_MS)
                await self._api.sleep(wait_ms / 1000)

    async def _create(
        self,
        user_id: str,
        course_id: str,
        activity_id: str,
        plan: Dict[str, Any],
        admins: FrozenSet[str],
        avatar_url: Optional[str],
    ) -> str:
        """A new room with the student's seat in it. The bot creates it and
        the student joins once it is set up, so their arrival comes after the
        activity's own state, which the chat draws as its top block. Without
        the bot the student creates it, as an activity's first player."""
        bot_user_id = self._bot_user_id
        creator = bot_user_id or user_id
        initial_state: List[Dict[str, Any]] = [
            {
                "type": ACTIVITY_PLAN_EVENT,
                "state_key": "",
                "content": {"activity_id": activity_id, "source_course_id": course_id},
            },
            {
                "type": ACTIVITY_ROLES_EVENT,
                "state_key": "",
                "content": with_seat(None, user_id),
            },
            {
                "type": EventTypes.JoinRules,
                "state_key": "",
                "content": {
                    "join_rule": "knock_restricted",
                    "allow": [{"type": "m.room_membership", "room_id": course_id}],
                },
            },
            # Shared, as every session room: a teacher or a later student
            # reads what was said before they joined.
            {
                "type": EventTypes.RoomHistoryVisibility,
                "state_key": "",
                "content": {"history_visibility": "shared"},
            },
            {
                "type": EventTypes.PowerLevels,
                "state_key": "",
                "content": discussion_power_levels(creator, admins, bot_user_id),
            },
        ]
        if avatar_url:
            initial_state.append(
                {
                    "type": EventTypes.RoomAvatar,
                    "state_key": "",
                    "content": {"url": avatar_url},
                }
            )
        config: Dict[str, Any] = {
            "preset": RoomCreationPreset.PRIVATE_CHAT,
            "visibility": "private",
            "name": plan.get("title") or "",
            "creation_content": {
                "type": f"{ACTIVITY_SESSION_TYPE_PREFIX}{activity_id}"
            },
            "initial_state": initial_state,
        }
        if plan.get("description"):
            config["topic"] = plan["description"]
        room_id, _, _ = await self._api._hs.get_room_creation_handler().create_room(
            requester=create_requester(
                creator, authenticated_entity=self._api.server_name
            ),
            config=config,
            ratelimit=False,
        )
        # Linked into the course by the student: the bot is no course member.
        await self._api.create_and_send_event_into_room(
            {
                "type": SPACE_CHILD_EVENT,
                "room_id": course_id,
                "sender": user_id,
                "state_key": room_id,
                "content": {"via": [self._api.server_name]},
            }
        )
        # Lower the creator: only the course's admins run the room. A course
        # admin opening their own course's discussion stays admin.
        power = discussion_power_levels(creator, admins, bot_user_id)
        if creator == bot_user_id:
            power["users"][creator] = 50
        elif creator not in admins:
            power["users"].pop(creator, None)
        await self._api.create_and_send_event_into_room(
            {
                "type": EventTypes.PowerLevels,
                "room_id": room_id,
                "sender": creator,
                "state_key": "",
                "content": power,
            }
        )
        if not bot_user_id:
            logger.error(
                "discussion_room: user_activity_notification_bot_user_id is not "
                "set, room %s has no bot",
                room_id,
            )
        return room_id

    async def _ensure_seat(self, room_id: str, user_id: str, sender: str) -> None:
        """Adds ``user_id``'s seat, sent by ``sender``, a member of the room.

        Read-modify-write: a client's roles write (a seat marked ended)
        landing between this read and the send is lost. The ModuleApi has no
        conditional state write, so the read is taken right before the send,
        and a write happens only on a student's first arrival."""
        state = await self._api.get_room_state(
            room_id, event_filter=[(ACTIVITY_ROLES_EVENT, "")]
        )
        current = state.get((ACTIVITY_ROLES_EVENT, ""))
        content = current.content if current is not None else None
        if user_id in seated_user_ids(content):
            return
        await self._api.create_and_send_event_into_room(
            {
                "type": ACTIVITY_ROLES_EVENT,
                "room_id": room_id,
                "sender": sender,
                "state_key": "",
                "content": with_seat(content, user_id),
            }
        )

    async def _course_admins(self, course_id: str) -> FrozenSet[str]:
        state = await self._api.get_room_state(
            course_id,
            event_filter=[(EventTypes.PowerLevels, ""), (EventTypes.Member, None)],
        )
        return frozenset(
            key
            for (event_type, key), _ in state.items()
            if event_type == EventTypes.Member
            and isinstance(key, str)
            and not is_probable_bot_user_id(key)
            and is_joined_instructor(state, key)
        )

    async def _discussion_plan(
        self, course_id: str, activity_id: str
    ) -> Dict[str, Any]:
        """The discussion's plan; raises ``DiscussionNotFound`` unless the
        activity is a discussion this course owns."""
        key = (course_id, activity_id)
        now = self._clock.time()
        cached = self._plans.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            plan = await make_deferred_yieldable(
                timeout_deferred(
                    deferred=run_in_background(
                        self._fetch_plan, course_id, activity_id
                    ),
                    timeout=CMS_TIMEOUT_SECONDS,
                    clock=self._clock,
                )
            )
        except Exception as e:
            raise CmsUnavailable(f"{type(e).__name__}: {e}") from e
        if plan is None:
            raise DiscussionNotFound()
        self._plans = {k: v for k, v in self._plans.items() if v[0] > now}
        self._plans[key] = (now + DISCUSSION_CACHE_SECONDS, plan)
        return plan

    async def _fetch_plan(
        self, course_id: str, activity_id: str
    ) -> Optional[Dict[str, Any]]:
        """The plan from the CMS, or None when the course has no such
        discussion; raises on any failure to ask."""
        base = self._config.cms_base_url
        api_key = self._config.cms_service_api_key
        if not base or not api_key:
            raise ValueError("cms_base_url or cms_service_api_key is not configured")
        query = urlencode(
            {
                "where[and][0][res.plan.activity_id][equals]": activity_id,
                "where[and][1][course_room_id][equals]": course_id,
                "where[and][2][res.plan.mode][equals]": DISCUSSION_MODE,
                "where[and][3][req.source_request_hash][exists]": "false",
                "limit": "1",
                "depth": "0",
            }
        )
        response = await self._http.request(
            "GET",
            f"{base.rstrip('/')}/api/activities-v2?{query}",
            headers=Headers(
                {"Authorization": [f"{CMS_AUTH_COLLECTION} API-Key {api_key}"]}
            ),
        )
        buffer = BytesIO()
        await make_deferred_yieldable(
            read_body_with_max_size(response, buffer, CMS_MAX_BODY_BYTES)
        )
        if response.code != 200:
            raise ValueError(f"CMS answered {response.code}")
        found = json.loads(buffer.getvalue())
        docs = found.get("docs") if isinstance(found, dict) else None
        if not isinstance(docs, list):
            raise ValueError("CMS answered without a docs list")
        if not docs:
            return None
        res = docs[0].get("res") if isinstance(docs[0], dict) else None
        plan = res.get("plan") if isinstance(res, dict) else None
        if not isinstance(plan, dict):
            logger.warning(
                "discussion_room: CMS row for %s has no plan, room left untitled",
                activity_id,
            )
            return {}
        return plan
