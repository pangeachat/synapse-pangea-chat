"""Join a course's admins to each new activity session linked into the course.

The client shares a new session into its courses by linking it as an
``m.space.child`` of each course space (and invites the bot itself). When that
first link lands, the course's admins are joined to the session as themselves,
exactly as the default chats join a space's members, and each admin's app is
told to keep the room out of their chats and badges: the room is added to
their ``pangea.admin_review`` account data and muted with the app's own mute
rule, the same two writes the dashboard's review join makes.

Design: course-session-admins.instructions.md.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, FrozenSet, List, Mapping, Optional

from synapse.api.constants import EventTypes
from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.events import EventBase
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from synapse.types import RoomID
from synapse.util.async_helpers import Linearizer
from synapse.util.duration import Duration
from twisted.web.resource import Resource

from synapse_pangea_chat.activity_session_previews.get_activity_session_previews import (
    has_valid_via,
)
from synapse_pangea_chat.blocked_join_gate import server_initiated_join
from synapse_pangea_chat.bot_user_ids import is_probable_bot_user_id
from synapse_pangea_chat.course_default_chats.course_default_chats import (
    SPACE_ROOM_TYPE,
    wait_out_rate_limits,
)
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.instructor_access import is_joined_instructor

try:
    import sentry_sdk  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - sentry is optional at runtime
    sentry_sdk = None

logger = logging.getLogger("synapse.module.synapse_pangea_chat.course_session_admins")

MEMBERSHIP_JOIN = "join"
MEMBERSHIP_INVITE = "invite"

#: The client's create type for a session room, followed by ``:<activity id>``
#: (client: ``PangeaRoomTypes.activitySession``).
ACTIVITY_SESSION_ROOM_TYPE = "p.activity.session"

#: The teacher's list of rooms the app hides (client:
#: ``PangeaEventTypes.adminReviewRooms``; admin-dash-api ``review_marker``).
ADMIN_REVIEW_ACCOUNT_DATA_TYPE = "pangea.admin_review"
ROOM_IDS_KEY = "room_ids"

#: Synapse's priority class for ``override`` push rules
#: (``synapse.push.rulekinds.PRIORITY_CLASS_MAP``).
OVERRIDE_PRIORITY_CLASS = 5


def is_activity_session_type(create_type: Any) -> bool:
    """Whether an ``m.room.create`` ``type`` marks an activity session room."""
    return isinstance(create_type, str) and (
        create_type == ACTIVITY_SESSION_ROOM_TYPE
        or create_type.startswith(ACTIVITY_SESSION_ROOM_TYPE + ":")
    )


def is_first_link(event: EventBase) -> bool:
    """A live ``m.space.child`` link that replaces no earlier link event for the
    same child: the session being shared into the course, not an old link sent
    again. Only new sessions get their admins joined; older ones are reviewed
    through the dashboard."""
    return (
        event.is_state()
        and event.type == EventTypes.SpaceChild
        and bool(event.state_key)
        and has_valid_via(event.content)
        and event.unsigned.get("replaces_state") is None
    )


def admits_course(join_rules: Optional[Mapping[str, Any]], space_id: str) -> bool:
    """Whether a room's ``m.room.join_rules`` content admits the members of
    ``space_id`` by restricted join: the shape the client gives a session it
    shares into a course. A public, invite-only or plain-knock room, or one
    restricted to another space, does not, so linking it into the course
    never pulls the course's admins in."""
    if not join_rules or join_rules.get("join_rule") not in (
        "restricted",
        "knock_restricted",
    ):
        return False
    allow = join_rules.get("allow")
    if not isinstance(allow, (list, tuple)):
        return False
    return any(
        isinstance(entry, Mapping)
        and entry.get("type") == "m.room_membership"
        and entry.get("room_id") == space_id
        for entry in allow
    )


def with_room_listed(
    content: Optional[Mapping[str, Any]], room_id: str
) -> Optional[Dict[str, Any]]:
    """The ``pangea.admin_review`` content with ``room_id`` on its list, sorted
    and de-duplicated, every other key kept as read; ``None`` when the room is
    already listed, so nothing is written."""
    content = dict(content or {})
    raw = content.get(ROOM_IDS_KEY)
    # Synapse answers account data frozen, so the list may arrive as a tuple.
    listed = (
        [r for r in raw if isinstance(r, str)] if isinstance(raw, (list, tuple)) else []
    )
    if room_id in listed:
        return None
    content[ROOM_IDS_KEY] = sorted(set(listed) | {room_id})
    return content


def mute_rule_conditions(room_id: str) -> List[Dict[str, str]]:
    """The conditions of the app's own mute: an override rule matching the room."""
    return [{"kind": "event_match", "key": "room_id", "pattern": room_id}]


def _capture_exception(e: Exception) -> None:
    # Background work never reaches Synapse's request-level Sentry capture.
    if sentry_sdk is not None:
        sentry_sdk.capture_exception(e)


class CourseSessionAdmins:
    """The hook and the joins. One instance per Synapse process."""

    def __init__(self, api: ModuleApi):
        self._api = api
        self._state = api._hs.get_storage_controllers().state
        self._store = api._hs.get_datastores().main
        self._clock = api._hs.get_clock()
        self._push_rules = api._hs.get_push_rules_handler()
        # The account-data list is a read-modify-write with no compare-and-set,
        # so two sessions linked together must not each drop the other's id.
        self._user_locks = Linearizer("course_session_admins", clock=self._clock)
        api.register_third_party_rules_callbacks(on_new_event=self.on_new_event)

    async def on_new_event(
        self, event: EventBase, _state: Mapping[Any, EventBase]
    ) -> None:
        """Awaited inline for every event on the homeserver, so this decides
        from the event alone and hands the work to a background task: the
        student's session is never held up by the admins' joins."""
        try:
            if is_first_link(event):
                self._clock.call_later(
                    Duration(milliseconds=0),
                    self._api.run_as_background_process,
                    "course_session_admins_link",
                    self.join_admins,
                    event.room_id,
                    event.state_key,
                )
            elif (
                event.type == EventTypes.SpaceChild
                and event.is_state()
                and event.unsigned.get("replaces_state") is not None
            ):
                logger.info(
                    "course_session_admins %s linked into %s again: not a new session",
                    event.state_key,
                    event.room_id,
                )
        except Exception as e:
            # A failure here must never reject or delay the event itself.
            logger.exception(
                "course_session_admins hook failed for %s in %s",
                event.event_id,
                event.room_id,
            )
            _capture_exception(e)

    async def join_admins(self, space_id: str, session_id: str) -> List[str]:
        """Join every admin of the course ``space_id`` to ``session_id`` when it
        is an activity session, hidden and muted. Answers who was joined; a
        failure for one admin is logged and does not stop the others."""
        refusal = await self._not_a_course_session(space_id, session_id)
        if refusal is not None:
            logger.info(
                "course_session_admins %s linked into %s: %s",
                session_id,
                space_id,
                refusal,
            )
            return []
        admins = await self._course_admins(space_id)
        joined: List[str] = []
        for user_id in sorted(admins):
            try:
                if await self._join_admin(space_id, session_id, user_id):
                    joined.append(user_id)
            except Exception as e:
                logger.warning(
                    "course_session_admins join failed for %s in %s: %s",
                    user_id,
                    session_id,
                    e,
                )
                _capture_exception(e)
        logger.info(
            "course_session_admins %s linked into %s: joined %s",
            session_id,
            space_id,
            joined,
        )
        return joined

    async def _not_a_course_session(
        self, space_id: str, session_id: str
    ) -> Optional[str]:
        """Why ``session_id`` is not a session of the course ``space_id`` whose
        admins may be joined to it, or ``None`` when it is one: an activity
        session whose join rule admits the course."""
        if not await self._has_create_type(session_id, is_activity_session_type):
            return "not an activity session"
        join_rules = await self._state.get_current_state_event(
            session_id, EventTypes.JoinRules, ""
        )
        if not admits_course(
            join_rules.content if join_rules is not None else None, space_id
        ):
            return "its join rule does not admit the course"
        return None

    async def is_space(self, room_id: str) -> bool:
        return await self._has_create_type(room_id, lambda t: t == SPACE_ROOM_TYPE)

    async def course_sessions(self, space_id: str) -> List[str]:
        """The sessions the course ``space_id`` links today whose admins may be
        joined to them, sorted: the rooms a backfill walks."""
        state = await self._api.get_room_state(
            space_id, event_filter=[(EventTypes.SpaceChild, None)]
        )
        sessions = []
        for (_, child_id), event in state.items():
            if (
                isinstance(child_id, str)
                and has_valid_via(event.content)
                and await self._not_a_course_session(space_id, child_id) is None
            ):
                sessions.append(child_id)
        return sorted(sessions)

    async def backfill_course(self, space_id: str, session_ids: List[str]) -> int:
        """Join the course's admins to each of its existing sessions, one room
        after another, with the same steps and checks as a new session. Safe to
        run again: an admin already in a room is skipped. Answers how many
        admin joins were made."""
        logger.info(
            "course_session_admins backfill of %s: %d sessions",
            space_id,
            len(session_ids),
        )
        total = 0
        for done, session_id in enumerate(session_ids, start=1):
            try:
                total += len(await self.join_admins(space_id, session_id))
            except Exception as e:
                logger.warning(
                    "course_session_admins backfill failed for %s in %s: %s",
                    session_id,
                    space_id,
                    e,
                )
                _capture_exception(e)
            logger.info(
                "course_session_admins backfill of %s: %d/%d sessions, %d joins",
                space_id,
                done,
                len(session_ids),
                total,
            )
        logger.info(
            "course_session_admins backfill of %s finished: %d joins",
            space_id,
            total,
        )
        return total

    async def _has_create_type(self, room_id: str, test: Any) -> bool:
        create = await self._state.get_current_state_event(
            room_id, EventTypes.Create, ""
        )
        return create is not None and bool(test(create.content.get("type")))

    async def _course_admins(self, space_id: str) -> FrozenSet[str]:
        """Joined, local, non-bot members of the space at admin power."""
        if not await self.is_space(space_id):
            return frozenset()
        state = await self._api.get_room_state(
            space_id,
            event_filter=[
                (EventTypes.Create, ""),
                (EventTypes.PowerLevels, ""),
                (EventTypes.Member, None),
            ],
        )
        return frozenset(
            user_id
            for (event_type, user_id), _ in state.items()
            if event_type == EventTypes.Member
            and isinstance(user_id, str)
            and self._api.is_mine(user_id)
            and not is_probable_bot_user_id(user_id)
            and is_joined_instructor(state, user_id)
        )

    async def _join_admin(self, space_id: str, room_id: str, user_id: str) -> bool:
        """List, join, then mute. Whether the admin may still be joined is read
        again right before every attempt, since an attempt can wait out a rate
        limit: she is not already in the room, has not left it or been banned,
        is still a course admin, and the course still links the room. A room
        that could not be listed is not joined, so it never lands in her
        chats."""
        joined = False

        async def attempt() -> None:
            nonlocal joined
            if not await self._still_eligible(space_id, room_id, user_id):
                return
            await self._list(user_id, room_id)
            await server_initiated_join(self._api, room_id, user_id)
            joined = True

        await wait_out_rate_limits(self._clock, attempt, "course_session_admins")
        if joined:
            await self._mute(user_id, room_id)
        return joined

    async def _still_eligible(self, space_id: str, room_id: str, user_id: str) -> bool:
        membership, _ = await self._store.get_local_current_membership_for_user_in_room(
            user_id, room_id
        )
        if membership not in (None, MEMBERSHIP_INVITE):
            return False
        state = await self._api.get_room_state(
            space_id,
            event_filter=[
                (EventTypes.Create, ""),
                (EventTypes.PowerLevels, ""),
                (EventTypes.Member, user_id),
                (EventTypes.SpaceChild, room_id),
            ],
        )
        link = state.get((EventTypes.SpaceChild, room_id))
        return (
            link is not None
            and has_valid_via(link.content)
            and is_joined_instructor(state, user_id)
        )

    async def _list(self, user_id: str, room_id: str) -> None:
        async with self._user_locks.queue(user_id):
            manager = self._api.account_data_manager
            current = await manager.get_global(user_id, ADMIN_REVIEW_ACCOUNT_DATA_TYPE)
            content = with_room_listed(current, room_id)
            if content is not None:
                await manager.put_global(
                    user_id, ADMIN_REVIEW_ACCOUNT_DATA_TYPE, content
                )

    async def _mute(self, user_id: str, room_id: str) -> None:
        """The app's own mute: an override rule matching the room, with no
        actions. The app also drops a mentions-only rule for the room first;
        a room created moments ago cannot have one yet."""
        await self._store.add_push_rule(
            user_id=user_id,
            rule_id=f"global/override/{room_id}",
            priority_class=OVERRIDE_PRIORITY_CLASS,
            conditions=mute_rule_conditions(room_id),
            actions=[],
        )
        self._push_rules.notify_user(user_id)


class BackfillCourseSessionAdmins(Resource):
    """``POST /_synapse/client/pangea/v1/backfill_course_session_admins``: joins
    one course's admins to the sessions it already links, the way a new
    session is handled. Server admins only, exactly one course per call.

    ``{"room_id": <course space>}`` answers 202 at once with how many sessions
    will be walked, and does the joins in the background, paced by Synapse's
    rate limits; progress is logged per session. Running it again skips every
    admin already in a room. One course per call, like the default-chats
    repair endpoint: which courses to touch is the operator's decision."""

    isLeaf = True

    def __init__(self, api: ModuleApi, admins: CourseSessionAdmins):
        super().__init__()
        self._api = api
        self._auth = api._hs.get_auth()
        self._admins = admins

    def render_POST(self, request: SynapseRequest) -> int:
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        try:
            requester = await self._auth.get_user_by_req(request)
            if not await self._api.is_user_admin(requester.user.to_string()):
                respond_with_json(
                    request, 403, {"error": "Admin access required"}, send_cors=True
                )
                return

            body = await extract_body_json(request)
            room_id = body.get("room_id") if isinstance(body, dict) else None
            if not isinstance(room_id, str) or not _is_valid_room_id(room_id):
                respond_with_json(
                    request,
                    400,
                    {"error": "'room_id' must be a valid room ID"},
                    send_cors=True,
                )
                return
            if not await self._admins.is_space(room_id):
                respond_with_json(
                    request, 400, {"error": "'room_id' must be a space"}, send_cors=True
                )
                return

            sessions = await self._admins.course_sessions(room_id)
            self._api.run_as_background_process(
                "course_session_admins_backfill",
                self._admins.backfill_course,
                room_id,
                sessions,
            )
            respond_with_json(
                request,
                202,
                {"room_id": room_id, "sessions": len(sessions), "scheduled": True},
                send_cors=True,
            )
        # silent-ok: the caller's auth failure, answered 401 (logged at INFO)
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ) as e:
            logger.info("Authentication failed: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
        except Exception as e:
            logger.exception("Error backfilling course session admins")
            _capture_exception(e)
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )


def _is_valid_room_id(room_id: str) -> bool:
    try:
        RoomID.from_string(room_id)
    # silent-ok: validation only - the caller answers 400 for an invalid id
    except Exception:
        return False
    return True
