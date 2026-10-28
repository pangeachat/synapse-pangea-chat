"""Join a course's admins to each new activity session linked into the course.

The client shares a new session into its courses by linking it as an
``m.space.child`` of each course space (and invites the bot itself). When that
first link lands, the course's admins are joined to the session as themselves,
exactly as the default chats join a space's members, and each admin's app is
told to keep the room out of their chats and badges: the room is added to
their ``pangea.admin_review`` account data and muted with the app's own mute
rule, the same two writes the dashboard's review join makes.

Design: pangeachat/synapse-pangea-chat#315 (instructions doc pending review).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, FrozenSet, List, Mapping, Optional

from synapse.api.constants import EventTypes
from synapse.events import EventBase
from synapse.module_api import ModuleApi
from synapse.util.async_helpers import Linearizer
from synapse.util.duration import Duration

from synapse_pangea_chat.activity_session_previews.get_activity_session_previews import (
    has_valid_via,
)
from synapse_pangea_chat.blocked_join_gate import server_initiated_join
from synapse_pangea_chat.bot_user_ids import is_probable_bot_user_id
from synapse_pangea_chat.course_default_chats.course_default_chats import (
    SPACE_ROOM_TYPE,
    wait_out_rate_limits,
)
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
        if not await self._has_create_type(session_id, is_activity_session_type):
            logger.info(
                "course_session_admins %s linked into %s: not an activity session",
                session_id,
                space_id,
            )
            return []
        join_rules = await self._state.get_current_state_event(
            session_id, EventTypes.JoinRules, ""
        )
        if not admits_course(
            join_rules.content if join_rules is not None else None, space_id
        ):
            logger.info(
                "course_session_admins %s linked into %s: its join rule does not "
                "admit the course",
                session_id,
                space_id,
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

    async def _has_create_type(self, room_id: str, test: Any) -> bool:
        create = await self._state.get_current_state_event(
            room_id, EventTypes.Create, ""
        )
        return create is not None and bool(test(create.content.get("type")))

    async def _course_admins(self, space_id: str) -> FrozenSet[str]:
        """Joined, local, non-bot members of the space at admin power."""
        if not await self._has_create_type(space_id, lambda t: t == SPACE_ROOM_TYPE):
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
