"""``POST /_synapse/client/pangea/v1/discussion_room``: put a student in a room
for one of their course's discussions.

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
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Optional, Tuple
from urllib.parse import urlencode

from synapse.api.constants import EventTypes, RoomCreationPreset
from synapse.api.errors import (
    AuthError,
    LimitExceededError,
    InvalidClientTokenError,
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
# as long as Synapse asks (capped): about ten seconds at its defaults.
JOIN_ATTEMPTS = 6


class DiscussionNotFound(Exception):
    """The activity is not a discussion of this course."""


class CmsUnavailable(Exception):
    """The CMS could not be asked whether the activity is a discussion."""


def seat_key(user_id: str) -> str:
    """A student's seat in ``pangea.activity_roles``: keyed by student, since
    every student holds the same role."""
    return f"{DISCUSSION_ROLE_ID}:{user_id}"


def with_seat(roles_content: Optional[Dict[str, Any]], user_id: str) -> Dict[str, Any]:
    """The roles content with ``user_id``'s seat added; an existing seat for
    them (finished or not) is kept as it is."""
    content = dict(roles_content or {})
    roles = dict(content.get("roles") or {})
    for seat in roles.values():
        if isinstance(seat, dict) and seat.get("user_id") == user_id:
            content["roles"] = roles
            return content
    roles[seat_key(user_id)] = {
        "id": DISCUSSION_ROLE_ID,
        "user_id": user_id,
        "role": DISCUSSION_ROLE_NAME,
    }
    content["roles"] = roles
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


def _query_arg(request: SynapseRequest, name: str) -> Optional[str]:
    values = request.args.get(name.encode()) if request.args else None
    if not values:
        return None
    value = values[0].decode("utf-8", "replace").strip()
    return value or None


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
        # (course_id, activity_id) -> (expires at, plan)
        self._plans: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
        # One request at a time per course and discussion, so a room is
        # counted and filled by one request before the next looks at it.
        # Per process: a deploy that runs this endpoint on more than one
        # worker must route it to one, or move to the worker locks handler.
        self._locks = Linearizer("discussion_room", clock=self._clock)

    def render_GET(self, request: SynapseRequest):
        run_in_background(self._async_render_GET, request)
        return server.NOT_DONE_YET

    async def _async_render_GET(self, request: SynapseRequest):
        """Preview: which room opening the discussion would put the student
        in and how many students it has, so the start page counts the room
        they will land in. Read-only and unlocked: a placement that runs
        between this and the student's tap places them by the room's state
        then, never by this answer."""
        try:
            requester = await self._auth.get_user_by_req(request)
            user_id = requester.user.to_string()
            if is_rate_limited(user_id, self._config, preview=True):
                respond_with_json(
                    request, 429, {"error": "Rate limited"}, send_cors=True
                )
                return
            course_id = _query_arg(request, "course_id")
            activity_id = _query_arg(request, "activity_id")
            if not (course_id and activity_id):
                respond_with_json(
                    request,
                    400,
                    {"error": "'course_id' and 'activity_id' are required"},
                    send_cors=True,
                )
                return
            (
                membership,
                _,
            ) = await self._store.get_local_current_membership_for_user_in_room(
                user_id, course_id
            )
            if membership != "join":
                respond_with_json(
                    request,
                    403,
                    {"error": "Not a member of this course"},
                    send_cors=True,
                )
                return
            await self._discussion_plan(course_id, activity_id)
            respond_with_json(
                request,
                200,
                await self._preview(user_id, course_id, activity_id),
                send_cors=True,
            )
        # silent-ok: answered 404, a caller asking for an activity that is not
        # one of this course's discussions
        except DiscussionNotFound:
            respond_with_json(
                request,
                404,
                {"error": "Not a discussion of this course"},
                send_cors=True,
            )
        except CmsUnavailable as e:
            logger.error("discussion_room preview: CMS unavailable: %s", e)
            respond_with_json(
                request,
                502,
                {"error": "Could not check the discussion"},
                send_cors=True,
            )
        # silent-ok: the caller's auth failure, answered 401 (logged at INFO)
        except (AuthError, InvalidClientTokenError, MissingClientTokenError) as e:
            logger.info("discussion_room preview: authentication failed: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
        except Exception:
            logger.exception("discussion_room preview failed")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest):
        try:
            requester = await self._auth.get_user_by_req(request)
            user_id = requester.user.to_string()
            if is_rate_limited(user_id, self._config):
                respond_with_json(
                    request, 429, {"error": "Rate limited"}, send_cors=True
                )
                return

            body = await extract_body_json(request)
            course_id = body.get("course_id") if isinstance(body, dict) else None
            activity_id = body.get("activity_id") if isinstance(body, dict) else None
            avatar_url = body.get("avatar_url") if isinstance(body, dict) else None
            if not (
                isinstance(course_id, str)
                and course_id
                and isinstance(activity_id, str)
                and activity_id
            ):
                respond_with_json(
                    request,
                    400,
                    {"error": "'course_id' and 'activity_id' are required strings"},
                    send_cors=True,
                )
                return

            (
                membership,
                _,
            ) = await self._store.get_local_current_membership_for_user_in_room(
                user_id, course_id
            )
            if membership != "join":
                respond_with_json(
                    request,
                    403,
                    {"error": "Not a member of this course"},
                    send_cors=True,
                )
                return

            plan = await self._discussion_plan(course_id, activity_id)
            async with self._locks.queue((course_id, activity_id)):
                room_id, created = await self._place(
                    user_id,
                    course_id,
                    activity_id,
                    plan,
                    avatar_url if isinstance(avatar_url, str) and avatar_url else None,
                )
            logger.info(
                "discussion_room %s in %s: room=%s created=%s",
                activity_id,
                course_id,
                room_id,
                created,
            )
            respond_with_json(
                request, 200, {"room_id": room_id, "created": created}, send_cors=True
            )
        # silent-ok: answered 429, the room still refusing joins after every
        # retry; the student is asked to try again
        except LimitExceededError:
            logger.warning("discussion_room: joins still rate limited, answered busy")
            respond_with_json(
                request,
                429,
                {"error": "Busy, try again", "errcode": "M_LIMIT_EXCEEDED"},
                send_cors=True,
            )
        # silent-ok: answered 404, a caller asking for an activity that is not
        # one of this course's discussions
        except DiscussionNotFound:
            respond_with_json(
                request,
                404,
                {"error": "Not a discussion of this course"},
                send_cors=True,
            )
        except CmsUnavailable as e:
            logger.error("discussion_room: CMS unavailable: %s", e)
            respond_with_json(
                request,
                502,
                {"error": "Could not check the discussion"},
                send_cors=True,
            )
        # silent-ok: the caller's auth failure, answered 401 (logged at INFO)
        except (AuthError, InvalidClientTokenError, MissingClientTokenError) as e:
            logger.info("discussion_room: authentication failed: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
        except Exception:
            logger.exception("discussion_room failed")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    async def _survey(
        self, user_id: str, course_id: str, activity_id: str
    ) -> Tuple[Optional[str], List[Tuple[str, int, int]], FrozenSet[str]]:
        """The discussion's rooms as placement sees them: the student's own
        room if they are in one, else each room with its student count
        (course admins and the bot never count) and when it was created, and
        the course's admins. Shared by placing a student and previewing where
        they would go, so the two never disagree.

        A course admin is joined to every session room to read it
        (course_session_admins), so their own room is the one they hold a seat
        in, not any room they are in."""
        admins = await self._course_admins(course_id)
        rooms = await _activity_session_ids(
            await _child_room_ids([course_id], self._store), activity_id, self._store
        )
        is_admin = user_id in admins
        rooms_info: List[Tuple[str, int, int]] = []
        for room_id in rooms:
            members = await self._joined_members(room_id)
            students = self._student_count(members, admins)
            if user_id in members and (
                not is_admin or await self._has_seat(room_id, user_id)
            ):
                return room_id, [(room_id, students, 0)], admins
            rooms_info.append((room_id, students, await self._created_ts(room_id)))
        return None, rooms_info, admins

    @staticmethod
    def _student_count(members: List[str], admins: FrozenSet[str]) -> int:
        return len(
            [m for m in members if m not in admins and not is_probable_bot_user_id(m)]
        )

    def _choose(
        self, user_id: str, rooms: List[Tuple[str, int, int]], admins: FrozenSet[str]
    ) -> Optional[str]:
        """The room to put someone with no room of their own in: a student
        goes to the fullest room with space; a course admin, who never counts
        toward a room's students, to the newest room. None means a new room."""
        if user_id in admins:
            if not rooms:
                return None
            return max(rooms, key=lambda r: r[2])[0]
        return pick_room(
            [(room_id, students) for room_id, students, _ in rooms],
            self._config.discussion_room_capacity,
        )

    async def _place(
        self,
        user_id: str,
        course_id: str,
        activity_id: str,
        plan: Dict[str, Any],
        avatar_url: Optional[str],
    ) -> Tuple[str, bool]:
        own, rooms, admins = await self._survey(user_id, course_id, activity_id)
        if own is not None:
            # Their own room: back to it, ended or not.
            await self._ensure_seat(own, user_id)
            return own, False

        room_id = self._choose(user_id, rooms, admins)
        if room_id is not None:
            await self._join(room_id, user_id)
            await self._ensure_seat(room_id, user_id)
            return room_id, False

        room_id = await self._create(
            user_id, course_id, activity_id, plan, admins, avatar_url
        )
        return room_id, True

    async def _preview(
        self, user_id: str, course_id: str, activity_id: str
    ) -> Dict[str, Any]:
        """Where the student would be placed now, without placing them: their
        own room, else the room placement would pick, else none (a new room).
        `students` counts that room's students; 0 for a new room."""
        own, rooms, admins = await self._survey(user_id, course_id, activity_id)
        if own is not None:
            return {"room_id": own, "students": rooms[0][1], "joined": True}
        room_id = self._choose(user_id, rooms, admins)
        students = (
            next(n for r, n, _ in rooms if r == room_id) if room_id is not None else 0
        )
        return {"room_id": room_id, "students": students, "joined": False}

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
                wait_ms = e.retry_after_ms or 1000
                await self._api.sleep(min(max(wait_ms, 200), 5000) / 1000)

    async def _has_seat(self, room_id: str, user_id: str) -> bool:
        state = await self._api.get_room_state(
            room_id, event_filter=[(ACTIVITY_ROLES_EVENT, "")]
        )
        current = state.get((ACTIVITY_ROLES_EVENT, ""))
        roles = (current.content.get("roles") if current is not None else None) or {}
        return any(
            isinstance(seat, dict) and seat.get("user_id") == user_id
            for seat in roles.values()
        )

    async def _created_ts(self, room_id: str) -> int:
        state = await self._api.get_room_state(
            room_id, event_filter=[(EventTypes.Create, "")]
        )
        create = state.get((EventTypes.Create, ""))
        return create.origin_server_ts if create is not None else 0

    async def _create(
        self,
        user_id: str,
        course_id: str,
        activity_id: str,
        plan: Dict[str, Any],
        admins: FrozenSet[str],
        avatar_url: Optional[str],
    ) -> str:
        bot_user_id = self._config.user_activity_notification_bot_user_id
        # The bot creates the room and the student joins once it is set up,
        # so the student's arrival comes after the activity's own state, which
        # the chat draws as its top block: a creator's join comes before it.
        # Without a bot the student creates it, as an activity's first player.
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
        if bot_user_id:
            # The course's members are admitted by the room's restricted join.
            await self._join(room_id, user_id)
        else:
            logger.error(
                "discussion_room: no bot configured, room %s has no facilitator",
                room_id,
            )
        return room_id

    async def _ensure_seat(self, room_id: str, user_id: str) -> None:
        state = await self._api.get_room_state(
            room_id, event_filter=[(ACTIVITY_ROLES_EVENT, "")]
        )
        current = state.get((ACTIVITY_ROLES_EVENT, ""))
        content = current.content if current is not None else None
        updated = with_seat(content, user_id)
        if updated == (content or {}):
            return
        await self._api.create_and_send_event_into_room(
            {
                "type": ACTIVITY_ROLES_EVENT,
                "room_id": room_id,
                "sender": user_id,
                "state_key": "",
                "content": updated,
            }
        )

    async def _joined_members(self, room_id: str) -> List[str]:
        state = await self._api.get_room_state(
            room_id, event_filter=[(EventTypes.Member, None)]
        )
        return [
            key
            for (event_type, key), event in state.items()
            if event_type == EventTypes.Member
            and event.content.get("membership") == "join"
        ]

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
