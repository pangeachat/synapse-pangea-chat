"""``/_synapse/client/pangea/v1/discussion_room``: a student's room for one of
their course's discussions, and their seat in it.

- ``POST {course_id, activity_id}`` places the caller and answers
  ``{room_id, created}``.
- ``GET ?course_id=&activity_id=`` answers where a placement would put them
  now, without placing them: ``{room_id, students, joined}``, ``room_id``
  null meaning a new room.
- ``POST {room_id, action: "end" | "continue"}`` ends or reopens the
  caller's own seat and answers ``{room_id, seat}``.

The server is the only writer of a discussion room's seats, under one lock
per discussion, so two requests never overfill a room or drop a seat.

Design: discussion-room.instructions.md; product rules: org
discussions.instructions.md.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
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
from urllib.parse import quote, urlencode, urljoin, urlparse

from synapse.api.constants import EventTypes, RoomCreationPreset
from synapse.api.errors import (
    AuthError,
    InvalidClientTokenError,
    LimitExceededError,
    MissingClientTokenError,
    SynapseError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.servlet import parse_string
from synapse.http.site import SynapseRequest
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.module_api import ModuleApi
from synapse.types import create_requester
from synapse.util.async_helpers import Linearizer, timeout_deferred
from twisted.web.resource import Resource

from synapse_pangea_chat.blocked_join_gate.blocked_join_gate import (
    server_initiated_join,
)
from synapse_pangea_chat.cms_http import (
    canonical_activity_where,
    cms_request,
    service_headers,
)
from synapse_pangea_chat.course_default_chats.course_default_chats import (
    wait_out_rate_limits,
)
from synapse_pangea_chat.discussion_room.rooms import (
    ACTIVITY_PLAN_EVENT,
    ACTIVITY_ROLES_EVENT,
    DISCUSSION_MODE,
    RoomView,
    continued_seat,
    course_discussion_rooms,
    creation_content,
    discussion_marker,
    discussion_power_levels,
    ended_seat,
    find_seat,
    is_discussion_plan,
    pick_room,
    room_view,
    utc_timestamp,
    with_seat,
    with_seat_changed,
    without_seat,
)
from synapse_pangea_chat.email_invite.course_missions import (
    COURSE_MISSIONS_STATE_EVENT_TYPE,
    CourseMissionsReader,
    activity_lo_ids,
    course_copy_lo_ids,
)
from synapse_pangea_chat.notice_delivery.rate_limit import SlidingWindowRateLimiter
from synapse_pangea_chat.public_courses.get_public_courses import extract_plan_id
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.instructor_access import course_admins
from synapse_pangea_chat.room_preview.constants import (
    PANGEA_COURSE_PLAN_STATE_EVENT_TYPE as COURSE_PLAN_EVENT,
)
from synapse_pangea_chat.sentry_capture import capture_exception
from synapse_pangea_chat.student_invitations.rate_limits import limit_for

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.discussion_room")

CMS_TIMEOUT_SECONDS = 5
CMS_MAX_BODY_BYTES = 1_000_000
# A start page polls its preview every few seconds; a discussion's plan is
# read from the CMS at most this often per course and discussion.
DISCUSSION_CACHE_SECONDS = 60
# Previews reuse the rooms' state this long; a placement never does, and
# clears it for the discussion it changed.
PREVIEW_CACHE_SECONDS = 3

# Joins wait out Synapse's per-room join limit at most this many times, each
# wait clamped, so a request is answered busy after about ten seconds.
JOIN_ATTEMPTS = 6
JOIN_WAIT_MAX_MS = 2000
# A join the chosen room refuses sends placement to another room this many
# times before the caller is refused.
PLACEMENT_ATTEMPTS = 3

ROUTE_PLACE = "discussion_room_place"
ROUTE_PREVIEW = "discussion_room_preview"
ROUTE_SEAT = "discussion_room_seat"

ACTION_END = "end"
ACTION_CONTINUE = "continue"

# The room version every discussion room is created in: version 12 forbids
# listing the creator in power levels, which these rooms do for the bot.
DISCUSSION_ROOM_VERSION = "11"

# Choreo's content signature (canonicalize.compute_activity_signature): 32 hex.
VERSION_ID_PATTERN = re.compile(r"[0-9a-f]{32}")

YOUTUBE_POSTER = "https://img.youtube.com/vi/{}/mqdefault.jpg"


class DiscussionNotFound(Exception):
    """The activity is not a discussion of this course."""


class CmsUnavailable(Exception):
    """The CMS could not be asked whether the activity is a discussion."""


class _Refused(Exception):
    """A request answered with a 4xx or 503, with no placement work done."""

    def __init__(self, status: int, error: str, errcode: Optional[str] = None):
        super().__init__(error)
        self.status = status
        self.body: Dict[str, Any] = {"error": error}
        if errcode:
            self.body["errcode"] = errcode


@dataclass(frozen=True)
class _Reservation:
    room_id: str
    created: bool
    joined: bool
    # This request wrote the seat, so a failed join releases it.
    wrote_seat: bool
    own: bool


def _nonempty(value: Any) -> Optional[str]:
    return (value.strip() or None) if isinstance(value, str) else None


def _youtube_id(url: Any) -> Optional[str]:
    """The video id of a YouTube watch, youtu.be, embed or shorts URL, the
    client's own test (ActivityMediaBlock.youtubeId)."""
    if not isinstance(url, str):
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/", 1)[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        parts = parsed.path.strip("/").split("/")
        if parts and parts[0] in ("embed", "shorts", "v") and len(parts) > 1:
            candidate = parts[1]
        else:
            query = dict(
                pair.split("=", 1) for pair in parsed.query.split("&") if "=" in pair
            )
            candidate = query.get("v", "")
    else:
        return None
    return candidate if len(candidate) == 11 else None


def _version_id(value: Any, activity_id: str) -> Optional[str]:
    """The activity version the start page read from choreo (its content
    signature), which a new room pins as a roleplay session does. Only that
    shape is pinned; anything else creates the room on the latest version,
    which every reader also handles."""
    if isinstance(value, str) and VERSION_ID_PATTERN.fullmatch(value):
        return value
    logger.warning(
        "discussion_room: no usable version_id for %s (%s); a new room is unpinned",
        activity_id,
        "missing" if value is None else "malformed",
    )
    return None


def _web_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value if urlparse(value).scheme in ("http", "https") else None


class DiscussionRoom(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = api._hs.get_auth()
        self._store = api._hs.get_datastores().main
        self._state = api._hs.get_storage_controllers().state
        self._clock = api._hs.get_clock()
        self._http = api.http_client
        # The course's quest and its Missions, read as the course copy reads them.
        self._quests = CourseMissionsReader(
            api, config.cms_base_url, config.cms_service_api_key
        )
        # The facilitator bot creates every room and writes every seat. Its id
        # is user_activity_notification_bot_user_id, shared with the
        # user-activity filter.
        self._bot_user_id = config.user_activity_notification_bot_user_id or None
        if self._bot_user_id is None:
            logger.error(
                "discussion_room: user_activity_notification_bot_user_id is not "
                "set; the endpoint answers 503"
            )
        self._limiters: Dict[str, SlidingWindowRateLimiter] = {}
        for route in (ROUTE_PLACE, ROUTE_PREVIEW, ROUTE_SEAT):
            burst, seconds = limit_for(config.student_invitation_rate_limits, route)
            self._limiters[route] = SlidingWindowRateLimiter(
                requests_per_burst=burst, burst_duration_seconds=seconds
            )
        # (course_id, activity_id) -> (expires at, plan)
        self._plans: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
        # (course_id, activity_id) -> (expires at, rooms), for previews only
        self._views: Dict[Tuple[str, str], Tuple[float, List[RoomView]]] = {}
        # Discussion rooms found without their bot, reported once per process.
        self._reported_botless: Set[str] = set()
        # One seat write at a time per course and discussion, so a room is
        # counted and its seats written by one request before the next looks.
        # Per process: the endpoint is routed to the main process only
        # (discussion-room.instructions.md, "Concurrency").
        self._locks = Linearizer("discussion_room", clock=self._clock)

    def render_GET(self, request: SynapseRequest):
        run_in_background(self._handle, request, preview=True)
        return server.NOT_DONE_YET

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._handle, request, preview=False)
        return server.NOT_DONE_YET

    async def _handle(self, request: SynapseRequest, *, preview: bool) -> None:
        try:
            status, body = 200, await self._answer(request, preview)
        # silent-ok: the caller's own error, or the endpoint unconfigured (logged at startup), answered with its status
        except _Refused as e:
            status, body = e.status, e.body
        # silent-ok: answered 404, a caller asking for an activity that is not one of this course's discussions
        except DiscussionNotFound:
            status, body = 404, {"error": "Not a discussion of this course"}
        # silent-ok: answered 429, the room still refusing joins after every
        # retry; the seat was released and the student is asked to try again
        except LimitExceededError:
            logger.warning("discussion_room: joins still rate limited, answered busy")
            status = 429
            body = {"error": "Busy, try again", "errcode": "M_LIMIT_EXCEEDED"}
        except CmsUnavailable as e:
            logger.error("discussion_room: CMS unavailable: %s", e)
            capture_exception(e)
            status, body = 502, {"error": "Could not check the discussion"}
        except Exception as e:
            logger.exception("discussion_room request failed")
            capture_exception(e)
            status, body = 500, {"error": "Internal server error"}
        respond_with_json(request, status, body, send_cors=True)

    async def _answer(self, request: SynapseRequest, preview: bool) -> Dict[str, Any]:
        try:
            requester = await self._auth.get_user_by_req(request)
        except (AuthError, InvalidClientTokenError, MissingClientTokenError) as e:
            logger.info("discussion_room: authentication failed: %s", e)
            raise _Refused(401, "Unauthorized", "M_UNAUTHORIZED") from e
        user_id = requester.user.to_string()
        if self._bot_user_id is None:
            logger.warning(
                "discussion_room: refused, the discussion bot is not configured"
            )
            raise _Refused(503, "Discussions are not available")
        bot_user_id = self._bot_user_id

        args: Any
        try:
            if preview:
                args = {
                    name: parse_string(request, name, required=False, encoding="utf-8")
                    for name in ("course_id", "activity_id")
                }
            else:
                args = await extract_body_json(request)
        except SynapseError as e:
            raise _Refused(e.code, e.msg, e.errcode) from e
        if not isinstance(args, dict):
            args = {}

        if not preview and "action" in args:
            self._check_rate(user_id, ROUTE_SEAT)
            room_id = _nonempty(args.get("room_id"))
            action = args.get("action")
            if not room_id or action not in (ACTION_END, ACTION_CONTINUE):
                raise _Refused(
                    400, "'room_id' and an 'action' of 'end' or 'continue' are required"
                )
            return await self._change_seat(user_id, room_id, action, bot_user_id)

        self._check_rate(user_id, ROUTE_PREVIEW if preview else ROUTE_PLACE)
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
            return await self._preview(user_id, course_id, activity_id, bot_user_id)

        room_id, created = await self._place(
            user_id,
            course_id,
            activity_id,
            plan,
            _version_id(args.get("version_id"), activity_id),
            bot_user_id,
        )
        logger.info(
            "discussion_room %s in %s: room=%s created=%s",
            activity_id,
            course_id,
            room_id,
            created,
        )
        return {"room_id": room_id, "created": created}

    def _check_rate(self, user_id: str, route: str) -> None:
        if self._limiters[route].is_rate_limited(user_id):
            raise _Refused(429, "Rate limited", "M_LIMIT_EXCEEDED")

    # --- Rooms ------------------------------------------------------------

    async def _room_views(
        self, course_id: str, activity_id: str, bot_user_id: str
    ) -> List[RoomView]:
        refs = await course_discussion_rooms(
            self._store, course_id, bot_user_id, activity_id
        )
        views: List[RoomView] = []
        for ref in refs:
            state = await self._api.get_room_state(
                ref.room_id,
                event_filter=[(EventTypes.Member, None), (ACTIVITY_ROLES_EVENT, "")],
            )
            view = room_view(ref.room_id, state, bot_user_id, ref.created_ts)
            if not view.bot_joined and ref.room_id not in self._reported_botless:
                # The bot can't be removed, so this is a bug or a manual change.
                self._reported_botless.add(ref.room_id)
                error = RuntimeError(f"discussion room {ref.room_id} has no bot")
                logger.error("discussion_room: %s", error)
                capture_exception(error)
            views.append(view)
        return views

    def _choose(
        self, user_id: str, views: List[RoomView], excluded: Set[str]
    ) -> Optional[RoomView]:
        """The fullest room with space that will take the caller; None means a
        new room. Course admins are placed the same way as students."""
        candidates = [
            view
            for view in views
            if view.bot_joined
            and not view.refuses(user_id)
            and view.room_id not in excluded
        ]
        picked = pick_room(
            [(view.room_id, view.students) for view in candidates],
            self._config.discussion_room_capacity,
        )
        return next((view for view in candidates if view.room_id == picked), None)

    async def _preview(
        self, user_id: str, course_id: str, activity_id: str, bot_user_id: str
    ) -> Dict[str, Any]:
        """Read-only and unlocked: a placement that runs between this and the
        student's tap places them by the rooms' state then, never by this
        answer."""
        key = (course_id, activity_id)
        now = self._clock.time()
        cached = self._views.get(key)
        if cached is not None and cached[0] > now:
            views = cached[1]
        else:
            views = await self._room_views(course_id, activity_id, bot_user_id)
            self._views = {k: v for k, v in self._views.items() if v[0] > now}
            self._views[key] = (now + PREVIEW_CACHE_SECONDS, views)
        own = next((view for view in views if view.is_own(user_id)), None)
        if own is not None:
            return {
                "room_id": own.room_id,
                "students": own.students,
                "joined": own.joined(user_id),
            }
        room = self._choose(user_id, views, set())
        if room is None:
            return {"room_id": None, "students": 0, "joined": False}
        return {"room_id": room.room_id, "students": room.students, "joined": False}

    # --- Placement ----------------------------------------------------------

    async def _place(
        self,
        user_id: str,
        course_id: str,
        activity_id: str,
        plan: Dict[str, Any],
        version_id: Optional[str],
        bot_user_id: str,
    ) -> Tuple[str, bool]:
        """Seat the caller under the lock, then join them after it: the join
        can wait out Synapse's join limit, and the seat already counts them.
        A join the room refuses releases the seat this request wrote and
        tries another room."""
        key = (course_id, activity_id)
        excluded: Set[str] = set()
        for _ in range(PLACEMENT_ATTEMPTS):
            async with self._locks.queue(key):
                reservation = await self._reserve(
                    user_id,
                    course_id,
                    activity_id,
                    plan,
                    version_id,
                    bot_user_id,
                    excluded,
                )
                self._views.pop(key, None)
            if reservation.joined:
                return reservation.room_id, reservation.created
            try:
                await self._join(reservation.room_id, user_id)
                return reservation.room_id, reservation.created
            except LimitExceededError:
                await self._release(key, reservation, user_id, bot_user_id)
                raise
            except Exception as e:
                if reservation.own:
                    # Their own room, with their awards in it: never a second seat.
                    raise
                logger.warning(
                    "discussion_room: %s refused %s (%s), trying another room",
                    reservation.room_id,
                    user_id,
                    type(e).__name__,
                )
                capture_exception(e)
                await self._release(key, reservation, user_id, bot_user_id)
                excluded.add(reservation.room_id)
        raise _Refused(403, "No room for this discussion can take you")

    async def _reserve(
        self,
        user_id: str,
        course_id: str,
        activity_id: str,
        plan: Dict[str, Any],
        version_id: Optional[str],
        bot_user_id: str,
        excluded: Set[str],
    ) -> _Reservation:
        """Chooses the caller's room and writes their seat in it. Their own
        room is chosen, ended or not, and even when it is full."""
        views = await self._room_views(course_id, activity_id, bot_user_id)
        own = next((view for view in views if view.is_own(user_id)), None)
        if own is not None:
            if not own.bot_joined:
                raise RuntimeError(f"discussion room {own.room_id} has no bot")
            wrote = await self._ensure_seat(own.room_id, user_id, bot_user_id)
            return _Reservation(
                own.room_id, False, own.joined(user_id), wrote, own=True
            )
        room = self._choose(user_id, views, excluded)
        if room is None:
            room_id = await self._create(
                user_id, course_id, activity_id, plan, version_id, bot_user_id
            )
            return _Reservation(room_id, True, False, True, own=False)
        wrote = await self._ensure_seat(room.room_id, user_id, bot_user_id)
        return _Reservation(room.room_id, False, room.joined(user_id), wrote, own=False)

    async def _join(self, room_id: str, user_id: str) -> None:
        """Join on the server's side, waiting out Synapse's per-room join
        limit: a class told to open the discussion at once arrives faster
        than it allows. Gives up after ``JOIN_ATTEMPTS``, raising the limit,
        which the endpoint answers as busy."""

        async def join() -> None:
            await server_initiated_join(self._api, room_id, user_id)

        await wait_out_rate_limits(
            self._clock,
            join,
            "discussion_room",
            max_waits=JOIN_ATTEMPTS,
            max_wait_ms=JOIN_WAIT_MAX_MS,
        )

    async def _release(
        self,
        key: Tuple[str, str],
        reservation: _Reservation,
        user_id: str,
        bot_user_id: str,
    ) -> None:
        """Removes the seat this request wrote for a join that never
        happened, so it stops counting. A failure here is captured and never
        hides the join's own failure."""
        if not reservation.wrote_seat:
            return
        try:
            async with self._locks.queue(key):
                state = await self._api.get_room_state(
                    reservation.room_id,
                    event_filter=[
                        (ACTIVITY_ROLES_EVENT, ""),
                        (EventTypes.Member, user_id),
                    ],
                )
                if (EventTypes.Member, user_id) in state:
                    return
                current = state.get((ACTIVITY_ROLES_EVENT, ""))
                content = current.content if current is not None else None
                if find_seat(content, user_id) is None:
                    return
                await self._send_roles(
                    reservation.room_id, without_seat(content, user_id), bot_user_id
                )
                self._views.pop(key, None)
        except Exception as e:
            logger.exception(
                "discussion_room: could not release %s's seat in %s",
                user_id,
                reservation.room_id,
            )
            capture_exception(e)

    async def _ensure_seat(self, room_id: str, user_id: str, bot_user_id: str) -> bool:
        """Adds ``user_id``'s seat, written by the bot; whether it wrote one.
        Called under the lock, and only the server writes seats, so the read
        and the write cannot interleave with another writer."""
        state = await self._api.get_room_state(
            room_id, event_filter=[(ACTIVITY_ROLES_EVENT, "")]
        )
        current = state.get((ACTIVITY_ROLES_EVENT, ""))
        content = current.content if current is not None else None
        if find_seat(content, user_id) is not None:
            return False
        await self._send_roles(room_id, with_seat(content, user_id), bot_user_id)
        return True

    async def _send_roles(
        self, room_id: str, content: Mapping[str, Any], bot_user_id: str
    ) -> None:
        await self._api.create_and_send_event_into_room(
            {
                "type": ACTIVITY_ROLES_EVENT,
                "room_id": room_id,
                "sender": bot_user_id,
                "state_key": "",
                "content": dict(content),
            }
        )

    # --- Ending and reopening a seat ------------------------------------------

    async def _change_seat(
        self, user_id: str, room_id: str, action: str, bot_user_id: str
    ) -> Dict[str, Any]:
        create = await self._state.get_current_state_event(
            room_id, EventTypes.Create, ""
        )
        if create is None:
            raise _Refused(404, "Unknown room")
        marker = discussion_marker(create.sender, create.content, bot_user_id)
        if marker is None:
            raise _Refused(403, "Not a discussion room")
        async with self._locks.queue((marker.course_id, marker.activity_id)):
            state = await self._api.get_room_state(
                room_id, event_filter=[(ACTIVITY_ROLES_EVENT, "")]
            )
            current = state.get((ACTIVITY_ROLES_EVENT, ""))
            content = current.content if current is not None else None
            found = find_seat(content, user_id)
            if found is None:
                raise _Refused(403, "No seat in this room")
            key, seat = found
            if action == ACTION_END:
                changed = ended_seat(seat, utc_timestamp(self._clock.time()))
            else:
                changed = continued_seat(seat)
            if changed != seat:
                await self._send_roles(
                    room_id, with_seat_changed(content, key, changed), bot_user_id
                )
        logger.info("discussion_room %s: %s %s their seat", room_id, user_id, action)
        return {"room_id": room_id, "seat": changed}

    # --- Creating a room ------------------------------------------------------

    async def _create(
        self,
        user_id: str,
        course_id: str,
        activity_id: str,
        plan: Dict[str, Any],
        version_id: Optional[str],
        bot_user_id: str,
    ) -> str:
        """A new room with the caller's seat in it. The bot creates it and the
        caller joins once it is set up, so their arrival comes after the
        activity's own state, which the chat draws as its top block."""
        admins = await course_admins(self._api, course_id)
        initial_state: List[Dict[str, Any]] = [
            {
                "type": ACTIVITY_PLAN_EVENT,
                "state_key": "",
                # The reference a client-made roleplay session carries, with
                # the mode: the room keeps the version it started with, so an
                # edit never changes the questions under awards already given.
                "content": {
                    "activity_id": activity_id,
                    **({"version_id": version_id} if version_id else {}),
                    "source_course_id": course_id,
                    "mode": DISCUSSION_MODE,
                },
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
                "content": discussion_power_levels(bot_user_id, admins),
            },
        ]
        avatar_url = await self._avatar_url(plan, activity_id)
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
            "room_version": DISCUSSION_ROOM_VERSION,
            "name": plan.get("title") or "",
            "creation_content": creation_content(course_id, activity_id),
            "initial_state": initial_state,
        }
        if plan.get("description"):
            config["topic"] = plan["description"]
        room_id, _, _ = await self._api._hs.get_room_creation_handler().create_room(
            requester=create_requester(
                bot_user_id, authenticated_entity=self._api.server_name
            ),
            config=config,
            ratelimit=False,
        )
        await self._link(room_id, course_id, user_id, admins, bot_user_id)
        return room_id

    async def _link(
        self,
        room_id: str,
        course_id: str,
        user_id: str,
        admins: FrozenSet[str],
        bot_user_id: str,
    ) -> None:
        """Lists the room under the course, sent by a course admin (a course
        may keep ``m.space.child`` above a student's level), else by the
        student. A room that cannot be listed is unreachable, so the bot
        leaves it and the request fails."""
        senders = [admin for admin in sorted(admins) if self._api.is_mine(admin)]
        senders.append(user_id)
        failure: Optional[Exception] = None
        for sender in senders:
            try:
                await self._api.create_and_send_event_into_room(
                    {
                        "type": EventTypes.SpaceChild,
                        "room_id": course_id,
                        "sender": sender,
                        "state_key": room_id,
                        "content": {"via": [self._api.server_name]},
                    }
                )
                return
            # silent-ok: the next sender is tried; the last failure is raised
            except Exception as e:
                failure = e
        logger.error(
            "discussion_room: could not list %s under %s; abandoning it",
            room_id,
            course_id,
        )
        try:
            await self._api.update_room_membership(
                sender=bot_user_id,
                target=bot_user_id,
                room_id=room_id,
                new_membership="leave",
            )
        except Exception as e:
            logger.exception("discussion_room: bot could not leave %s", room_id)
            capture_exception(e)
        raise RuntimeError(f"could not list {room_id} under {course_id}") from failure

    async def _avatar_url(
        self, plan: Mapping[str, Any], activity_id: str
    ) -> Optional[str]:
        """The room's image, from the plan rather than the caller: the first
        visible media block's image or poster, as the client's start page
        picks it, else the plan's single legacy image. None when it has none
        or the image cannot be resolved (captured: the room is still made)."""
        try:
            media = plan.get("media")
            for block in media if isinstance(media, list) else []:
                if not isinstance(block, Mapping):
                    continue
                kind = block.get("blockType") or block.get("block_type")
                if kind == "youtube":
                    video_id = _youtube_id(block.get("url"))
                    return _web_url(block.get("thumbnail_url")) or (
                        YOUTUBE_POSTER.format(video_id) if video_id else None
                    )
                if kind in ("image", "video"):
                    return await self._media_image(block.get("upload_id"), kind)
            return _web_url(plan.get("image_url"))
        except Exception as e:
            logger.warning(
                "discussion_room: no room image for %s (%s)",
                activity_id,
                type(e).__name__,
            )
            capture_exception(e)
            return None

    async def _media_image(self, upload_id: Any, kind: str) -> Optional[str]:
        """An upload's CDN image: for an image its medium size or original, for
        a video only a poster size, never the video file itself."""
        if not isinstance(upload_id, str) or not upload_id:
            return None
        base = self._config.cms_base_url.rstrip("/")
        code, raw = await cms_request(
            self._http,
            "GET",
            f"{base}/api/media/{quote(upload_id, safe='')}?depth=0",
            service_headers(self._config.cms_service_api_key),
            max_bytes=CMS_MAX_BODY_BYTES,
        )
        if code != 200:
            raise ValueError(f"CMS media answered {code}")
        doc = json.loads(raw)
        if not isinstance(doc, Mapping):
            raise ValueError("CMS media answered a non-object")
        sizes = doc.get("sizes")
        candidates: List[Any] = []
        if isinstance(sizes, Mapping):
            for size in ("medium", "thumbnail"):
                variant = sizes.get(size)
                if isinstance(variant, Mapping):
                    candidates.append(variant.get("url"))
        if kind == "image":
            candidates.append(doc.get("url"))
        for url in candidates:
            if isinstance(url, str) and url:
                return _web_url(urljoin(base + "/", url))
        return None

    # --- The plan -------------------------------------------------------------

    async def _discussion_plan(
        self, course_id: str, activity_id: str
    ) -> Dict[str, Any]:
        """The discussion's plan; raises ``DiscussionNotFound`` unless the
        activity is a discussion this course may use."""
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
        """The plan, or None when the activity is not a discussion this course
        may use; raises on any failure to ask, and on two rows for one
        activity.

        A discussion is a catalog activity filed under a Mission, as a
        roleplay is. A course may use it when the discussion is the course's
        own copy, or when its Mission is one of the course's: in the course's
        quest, or in the course's own Can-Do list. Another course's copy is
        never used. Rooms stay per course either way."""
        base = self._config.cms_base_url
        api_key = self._config.cms_service_api_key
        if not base or not api_key:
            raise ValueError("cms_base_url or cms_service_api_key is not configured")
        query = urlencode(
            {**canonical_activity_where(activity_id), "limit": "2", "depth": "0"}
        )
        code, raw = await cms_request(
            self._http,
            "GET",
            f"{base.rstrip('/')}/api/activities-v2?{query}",
            service_headers(api_key),
            max_bytes=CMS_MAX_BODY_BYTES,
        )
        if code != 200:
            raise ValueError(f"CMS answered {code}")
        found = json.loads(raw)
        docs = found.get("docs") if isinstance(found, dict) else None
        if not isinstance(docs, list):
            raise ValueError("CMS answered without a docs list")
        if not docs:
            return None
        if len(docs) > 1:
            raise ValueError(f"CMS has more than one row for {activity_id}")
        doc = docs[0] if isinstance(docs[0], dict) else {}
        res = doc.get("res")
        plan = res.get("plan") if isinstance(res, dict) else None
        if not isinstance(plan, dict):
            raise ValueError(f"CMS row for {activity_id} has no plan")
        if not is_discussion_plan(plan):
            logger.info(
                "discussion_room: %s is not shaped as a discussion", activity_id
            )
            return None
        copy_of = doc.get("course_room_id")
        if copy_of:
            if copy_of == course_id:
                return plan
            logger.info(
                "discussion_room: %s is another course's copy, not %s",
                activity_id,
                course_id,
            )
            return None
        if not await self._in_course_missions(course_id, activity_lo_ids(doc)):
            logger.info(
                "discussion_room: %s is not in a Mission of %s",
                activity_id,
                course_id,
            )
            return None
        return plan

    async def _in_course_missions(self, course_id: str, lo_ids: List[str]) -> bool:
        """Whether one of these Missions is one of the course's: in the course's
        own Can-Do list (``pangea.course_missions``, read as the client reads
        it, so only a course admin's list counts), or in the quest the course
        follows (``pangea.course_plan``). The copy can add Missions the quest
        does not carry, and the client lists their activities. A course with
        neither uses none."""
        if not lo_ids:
            return False
        state = await self._api.get_room_state(
            course_id,
            event_filter=[
                (COURSE_PLAN_EVENT, ""),
                (COURSE_MISSIONS_STATE_EVENT_TYPE, ""),
                (EventTypes.Create, ""),
                (EventTypes.PowerLevels, ""),
            ],
        )
        wanted = set(lo_ids)
        if not wanted.isdisjoint(course_copy_lo_ids(state)):
            return True
        course_plan = state.get((COURSE_PLAN_EVENT, ""))
        # The quest id under either key the course plan has carried, by the
        # catalog's own rule, as the client reads it.
        quest_id = (
            extract_plan_id(course_plan.content) if course_plan is not None else None
        )
        if quest_id is None:
            return False
        quest_lo_ids = await self._quests.quest_lo_ids(quest_id)
        return not wanted.isdisjoint(quest_lo_ids)
