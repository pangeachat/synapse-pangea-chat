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
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Optional, Tuple
from urllib.parse import urlencode

from synapse.api.constants import EventTypes, RoomCreationPreset
from synapse.api.errors import (
    AuthError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from synapse.types import create_requester
from synapse.util.async_helpers import Linearizer
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
    admins at 100 and students at 0. The creator starts at 100 so the server
    can finish setting the room up as them, then is lowered to a student."""
    users: Dict[str, int] = {admin: 100 for admin in admins}
    users[creator] = 100
    if bot_user_id:
        users[bot_user_id] = 50
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


class DiscussionRoom(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = api._hs.get_auth()
        self._store = api._hs.get_datastores().main
        # One request at a time per course and discussion, so a room is
        # counted and filled by one request before the next looks at it.
        # Per process: a deploy that runs this endpoint on more than one
        # worker must route it to one, or move to the worker locks handler.
        self._locks = Linearizer("discussion_room", clock=api._hs.get_clock())

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

    async def _place(
        self,
        user_id: str,
        course_id: str,
        activity_id: str,
        plan: Dict[str, Any],
        avatar_url: Optional[str],
    ) -> Tuple[str, bool]:
        admins = await self._course_admins(course_id)
        rooms = await _activity_session_ids(
            await _child_room_ids([course_id], self._store), activity_id, self._store
        )
        counts: List[Tuple[str, int]] = []
        for room_id in rooms:
            members = await self._joined_members(room_id)
            if user_id in members:
                # Their own room: back to it, ended or not.
                await self._ensure_seat(room_id, user_id)
                return room_id, False
            students = [
                m for m in members if m not in admins and not is_probable_bot_user_id(m)
            ]
            counts.append((room_id, len(students)))

        room_id = pick_room(counts, self._config.discussion_room_capacity)
        if room_id is not None:
            await server_initiated_join(self._api, room_id, user_id)
            await self._ensure_seat(room_id, user_id)
            return room_id, False

        room_id = await self._create(
            user_id, course_id, activity_id, plan, admins, avatar_url
        )
        return room_id, True

    async def _create(
        self,
        user_id: str,
        course_id: str,
        activity_id: str,
        plan: Dict[str, Any],
        admins: FrozenSet[str],
        avatar_url: Optional[str],
    ) -> str:
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
                "content": discussion_power_levels(
                    user_id, admins, self._config.user_activity_notification_bot_user_id
                ),
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
                user_id, authenticated_entity=self._api.server_name
            ),
            config=config,
            ratelimit=False,
        )
        await self._api.create_and_send_event_into_room(
            {
                "type": SPACE_CHILD_EVENT,
                "room_id": course_id,
                "sender": user_id,
                "state_key": room_id,
                "content": {"via": [self._api.server_name]},
            }
        )
        # Lower the creator to a student: only the course's admins run the room.
        # A course admin opening their own course's discussion stays admin.
        power = discussion_power_levels(
            user_id, admins, self._config.user_activity_notification_bot_user_id
        )
        if user_id not in admins:
            power["users"].pop(user_id, None)
        await self._api.create_and_send_event_into_room(
            {
                "type": EventTypes.PowerLevels,
                "room_id": room_id,
                "sender": user_id,
                "state_key": "",
                "content": power,
            }
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
        """The discussion's plan, read from the CMS; raises ``DiscussionNotFound``
        unless the activity is a discussion this course owns."""
        from synapse.logging.context import make_deferred_yieldable
        from twisted.internet import reactor
        from twisted.web.client import Agent, readBody
        from twisted.web.http_headers import Headers

        base = self._config.cms_base_url
        key = self._config.cms_service_api_key
        if not base or not key:
            raise CmsUnavailable(
                "cms_base_url or cms_service_api_key is not configured"
            )
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
        try:
            response = await make_deferred_yieldable(
                Agent(reactor).request(
                    b"GET",
                    f"{base}/api/activities-v2?{query}".encode("utf-8"),
                    Headers(
                        {
                            b"Authorization": [
                                f"{CMS_AUTH_COLLECTION} API-Key {key}".encode("utf-8")
                            ]
                        }
                    ),
                    None,
                )
            )
            body = await make_deferred_yieldable(readBody(response))
        except Exception as e:
            raise CmsUnavailable(str(e)) from e
        if response.code >= 400:
            raise CmsUnavailable(f"CMS answered {response.code}")
        docs = json.loads(body).get("docs") or []
        if not docs:
            raise DiscussionNotFound()
        return (docs[0].get("res") or {}).get("plan") or {}
