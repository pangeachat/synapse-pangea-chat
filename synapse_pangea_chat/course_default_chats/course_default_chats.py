"""Keep a space's default chats (Announcements, Introductions) complete.

Every member of the space is joined to each default chat, and the space's
admins are the chat's admins. Design, including what counts as a space, a
default chat, a member and an admin: course-default-chats.instructions.md.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, FrozenSet, List, Mapping, Optional

import attr
from synapse.api.constants import EventTypes
from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    LimitExceededError,
    MissingClientTokenError,
    SynapseError,
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
from synapse_pangea_chat.public_courses.select_state_sender import (
    select_state_sender,
)
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.get_inviter_user import get_inviter_user
from synapse_pangea_chat.room_code.instructor_access import is_joined_instructor

try:
    import sentry_sdk  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - sentry is optional at runtime
    sentry_sdk = None

logger = logging.getLogger("synapse.module.synapse_pangea_chat.course_default_chats")

SPACE_ROOM_TYPE = "m.space"
MEMBERSHIP_JOIN = "join"
MEMBERSHIP_BAN = "ban"
MEMBERSHIP_INVITE = "invite"
ADMIN_POWER_LEVEL = 100

# The client's own marks for a default chat, on the alias localpart
# (client: SpaceConstants.introductionChatAlias / announcementsChatAlias).
INTRODUCTIONS_ALIAS_PREFIX = "introductionChat"
ANNOUNCEMENTS_ALIAS_PREFIX = "announcementsChat"
DEFAULT_CHAT_ALIAS_PREFIXES = (INTRODUCTIONS_ALIAS_PREFIX, ANNOUNCEMENTS_ALIAS_PREFIX)

# A join or invite refused by a rate limiter is retried after the delay the
# limiter asks for; this bounds how long one member can hold up the rest.
MAX_RATE_LIMIT_WAITS = 60


def is_default_chat_alias(alias: Any) -> bool:
    """Whether a canonical alias such as ``#announcementsChat_x_1:server``
    marks a default chat - the client's test, on the alias localpart."""
    if not isinstance(alias, str) or not alias.startswith("#"):
        return False
    localpart = alias[1:].split(":", 1)[0]
    return localpart.startswith(DEFAULT_CHAT_ALIAS_PREFIXES)


def _power_of(users: Mapping[str, Any], user_id: str, users_default: int) -> int:
    value = users.get(user_id, users_default)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return users_default
    try:
        return int(value)
    except ValueError:
        return users_default


def admins_to_promote(
    chat_users: Mapping[str, Any],
    users_default: int,
    space_admins: FrozenSet[str],
    immutable: FrozenSet[str],
) -> List[str]:
    """Space admins below admin power in the chat, sorted."""
    return sorted(
        user_id
        for user_id in space_admins
        if user_id not in immutable
        and _power_of(chat_users, user_id, users_default) < ADMIN_POWER_LEVEL
    )


def admins_to_demote(
    chat_users: Mapping[str, Any],
    users_default: int,
    chat_joined: FrozenSet[str],
    space_members: FrozenSet[str],
    space_admins: FrozenSet[str],
    immutable: FrozenSet[str],
) -> List[str]:
    """Joined chat members at admin power who are space members but not space
    admins, sorted. A member who left the chat is not demoted: only a joined
    member can send the power-levels event that lowers their own entry."""
    return sorted(
        user_id
        for user_id in chat_joined & space_members
        if user_id not in space_admins
        and user_id not in immutable
        and _power_of(chat_users, user_id, users_default) >= ADMIN_POWER_LEVEL
    )


class NoChatAdminError(Exception):
    """No joined member of the chat can send its power levels, so the
    space's admins cannot be mirrored into it. Nobody is promoted to fix
    that; it is reported for an operator."""


@attr.s(auto_attribs=True, frozen=True)
class SpaceRoster:
    members: FrozenSet[str]
    admins: FrozenSet[str]


@attr.s(auto_attribs=True, frozen=True)
class ChatState:
    power_content: Dict[str, Any]
    memberships: Dict[str, str]
    immutable: FrozenSet[str]

    @property
    def users(self) -> Dict[str, Any]:
        users = self.power_content.get("users")
        return dict(users) if isinstance(users, Mapping) else {}

    @property
    def users_default(self) -> int:
        return _coerce_int(self.power_content.get("users_default"))

    @property
    def joined(self) -> FrozenSet[str]:
        return frozenset(
            user_id
            for user_id, membership in self.memberships.items()
            if membership == MEMBERSHIP_JOIN
        )


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@attr.s(auto_attribs=True)
class ChatSyncReport:
    chat_id: str
    promoted: List[str] = attr.Factory(list)
    demoted: List[str] = attr.Factory(list)
    admin_sender_missing: bool = False
    joined: List[str] = attr.Factory(list)
    already_joined: int = 0
    skipped_banned: List[str] = attr.Factory(list)
    failed: List[Dict[str, str]] = attr.Factory(list)

    def as_json(self) -> Dict[str, Any]:
        return attr.asdict(self)


def _capture_exception(e: Exception) -> None:
    # Background tasks and respond_with_json paths never reach Synapse's
    # request-level Sentry capture, so a failure absorbed here is invisible
    # unless captured explicitly.
    if sentry_sdk is not None:
        sentry_sdk.capture_exception(e)


class CourseDefaultChats:
    """The rule's hook and sync. One instance per Synapse process."""

    def __init__(self, api: ModuleApi):
        self._api = api
        self._state = api._hs.get_storage_controllers().state
        self._store = api._hs.get_datastores().main
        self._clock = api._hs.get_clock()
        # One sync per chat at a time: the admin mirror is a read-modify-write
        # of the chat's power levels, and two triggers on one space (a link
        # and a join, say) can land together.
        self._chat_locks = Linearizer("course_default_chats", clock=self._clock)
        api.register_third_party_rules_callbacks(on_new_event=self.on_new_event)

    # ------------------------------------------------------------------
    # Hook
    # ------------------------------------------------------------------

    async def on_new_event(
        self, event: EventBase, _state: Mapping[Any, EventBase]
    ) -> None:
        """Awaited inline by the notifier for every event on the homeserver,
        so this does no I/O: it decides from the event alone and hands the
        work to a background task, which checks the room is a space."""
        try:
            if not event.is_state():
                return
            if event.type == EventTypes.SpaceChild:
                if event.state_key and has_valid_via(event.content):
                    self._in_background(
                        "link", self.sync_chat, event.room_id, event.state_key
                    )
            elif event.type == EventTypes.Member:
                user_id = event.state_key
                if (
                    event.content.get("membership") == MEMBERSHIP_JOIN
                    and isinstance(user_id, str)
                    and self._api.is_mine(user_id)
                    and not is_probable_bot_user_id(user_id)
                ):
                    self._in_background(
                        "member", self.sync_member, event.room_id, user_id
                    )
            elif event.type == EventTypes.PowerLevels:
                self._in_background("power", self.sync_space, event.room_id)
        except Exception as e:
            # A failure here must never reject or delay the event itself.
            logger.exception(
                "course_default_chats hook failed for %s in %s",
                event.event_id,
                event.room_id,
            )
            _capture_exception(e)

    def _in_background(
        self, what: str, func: Callable[..., Awaitable[Any]], *args: Any
    ) -> None:
        self._api.run_as_background_process(f"course_default_chats_{what}", func, *args)

    # ------------------------------------------------------------------
    # Sync entry points (each returns None when the room is not a space)
    # ------------------------------------------------------------------

    async def sync_space(self, space_id: str) -> Optional[List[ChatSyncReport]]:
        """Bring every default chat of the space in line with the space."""
        roster = await self._space_roster(space_id)
        if roster is None:
            return None
        return [
            await self._sync_chat(space_id, chat_id, roster, only_user=None)
            for chat_id in await self._default_chat_ids(space_id)
        ]

    async def sync_chat(self, space_id: str, chat_id: str) -> Optional[ChatSyncReport]:
        """A child was linked to the space: a full sync if it is a default chat."""
        roster = await self._space_roster(space_id)
        if roster is None or not await self._is_default_chat(chat_id):
            return None
        return await self._sync_chat(space_id, chat_id, roster, only_user=None)

    async def sync_member(
        self, space_id: str, user_id: str
    ) -> Optional[List[ChatSyncReport]]:
        """A user joined the space: join them to its default chats."""
        roster = await self._space_roster(space_id)
        if roster is None or user_id not in roster.members:
            return None
        return [
            await self._sync_chat(space_id, chat_id, roster, only_user=user_id)
            for chat_id in await self._default_chat_ids(space_id)
        ]

    async def sync_spaces(self, space_ids: List[str]) -> None:
        """The repair run over many spaces, one at a time, outcomes in the log."""
        synced = failed = 0
        for space_id in space_ids:
            try:
                reports = await self.sync_space(space_id)
            except Exception as e:
                failed += 1
                logger.warning(
                    "course_default_chats repair failed for %s: %s", space_id, e
                )
                _capture_exception(e)
                continue
            if reports:
                synced += 1
        logger.info(
            "course_default_chats repair done: %d spaces, %d with default chats, %d failed",
            len(space_ids),
            synced,
            failed,
        )

    async def all_space_ids(self) -> List[str]:
        return await self._store.db_pool.simple_select_onecol(
            table="room_stats_state",
            keyvalues={"room_type": SPACE_ROOM_TYPE},
            retcol="room_id",
            desc="course_default_chats_all_spaces",
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def _space_roster(self, room_id: str) -> Optional[SpaceRoster]:
        create = await self._state.get_current_state_event(
            room_id, EventTypes.Create, ""
        )
        if create is None or create.content.get("type") != SPACE_ROOM_TYPE:
            return None
        state = await self._api.get_room_state(
            room_id,
            event_filter=[
                (EventTypes.Create, ""),
                (EventTypes.PowerLevels, ""),
                (EventTypes.Member, None),
            ],
        )
        members = frozenset(
            user_id
            for (event_type, user_id), event in state.items()
            if event_type == EventTypes.Member
            and isinstance(user_id, str)
            and event.content.get("membership") == MEMBERSHIP_JOIN
            and self._api.is_mine(user_id)
            and not is_probable_bot_user_id(user_id)
        )
        admins = frozenset(
            user_id for user_id in members if is_joined_instructor(state, user_id)
        )
        return SpaceRoster(members=members, admins=admins)

    async def _default_chat_ids(self, space_id: str) -> List[str]:
        state = await self._api.get_room_state(
            space_id, event_filter=[(EventTypes.SpaceChild, None)]
        )
        chat_ids = []
        for (_, child_id), event in state.items():
            if not isinstance(child_id, str) or not has_valid_via(event.content):
                continue
            if await self._is_default_chat(child_id):
                chat_ids.append(child_id)
        return sorted(chat_ids)

    async def _is_default_chat(self, room_id: str) -> bool:
        alias_event = await self._state.get_current_state_event(
            room_id, EventTypes.CanonicalAlias, ""
        )
        return alias_event is not None and is_default_chat_alias(
            alias_event.content.get("alias")
        )

    async def _chat_state(self, chat_id: str) -> ChatState:
        state = await self._api.get_room_state(
            chat_id,
            event_filter=[
                (EventTypes.Create, ""),
                (EventTypes.PowerLevels, ""),
                (EventTypes.Member, None),
            ],
        )
        power = state.get((EventTypes.PowerLevels, ""))
        memberships = {
            user_id: str(event.content.get("membership"))
            for (event_type, user_id), event in state.items()
            if event_type == EventTypes.Member and isinstance(user_id, str)
        }
        # Room versions with creator power (MSC4289) give the creators a level
        # no power-levels event can set or lower.
        immutable: FrozenSet[str] = frozenset()
        create = state.get((EventTypes.Create, ""))
        if create is not None and getattr(
            create.room_version, "msc4289_creator_power_enabled", False
        ):
            immutable = frozenset(
                [create.sender, *create.content.get("additional_creators", [])]
            )
        return ChatState(
            power_content=dict(power.content) if power is not None else {},
            memberships=memberships,
            immutable=immutable,
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def _sync_chat(
        self,
        space_id: str,
        chat_id: str,
        roster: SpaceRoster,
        only_user: Optional[str],
    ) -> ChatSyncReport:
        report = ChatSyncReport(chat_id=chat_id)
        async with self._chat_locks.queue(chat_id):
            await self._mirror_admins(chat_id, roster, report)
            targets = [only_user] if only_user else sorted(roster.members)
            for user_id in targets:
                await self._join_member(chat_id, user_id, report)
        logger.info(
            "course_default_chats synced %s in %s: %s", chat_id, space_id, report
        )
        return report

    async def _mirror_admins(
        self, chat_id: str, roster: SpaceRoster, report: ChatSyncReport
    ) -> None:
        chat = await self._chat_state(chat_id)
        promote = admins_to_promote(
            chat.users, chat.users_default, roster.admins, chat.immutable
        )
        if promote:
            # One event for every promotion: an admin may set others to their
            # own level, so any joined chat admin can send it.
            sender = await select_state_sender(
                self._api, chat_id, EventTypes.PowerLevels
            )
            if sender is None:
                report.admin_sender_missing = True
                error = NoChatAdminError(
                    f"No joined admin can mirror {len(promote)} space admins into {chat_id}"
                )
                logger.warning("%s", error)
                _capture_exception(error)
            else:
                users = chat.users
                for user_id in promote:
                    users[user_id] = ADMIN_POWER_LEVEL
                try:
                    await self._send_power_levels(chat_id, sender, chat, users)
                    report.promoted = promote
                    chat = await self._chat_state(chat_id)
                except Exception as e:
                    self._record_failure(report, sender, e, "promote")
        # Matrix lets an admin lower only their own level, never another
        # admin's, so each demotion is sent by the member being demoted.
        for user_id in admins_to_demote(
            chat.users,
            chat.users_default,
            chat.joined,
            roster.members,
            roster.admins,
            chat.immutable,
        ):
            users = chat.users
            users.pop(user_id, None)
            try:
                await self._send_power_levels(chat_id, user_id, chat, users)
                report.demoted.append(user_id)
                chat = await self._chat_state(chat_id)
            except Exception as e:
                self._record_failure(report, user_id, e, "demote")

    async def _send_power_levels(
        self, chat_id: str, sender: str, chat: ChatState, users: Dict[str, Any]
    ) -> None:
        content = dict(chat.power_content)
        content["users"] = users
        await self._paced(
            lambda: self._api.create_and_send_event_into_room(
                {
                    "type": EventTypes.PowerLevels,
                    "room_id": chat_id,
                    "sender": sender,
                    "state_key": "",
                    "content": content,
                }
            )
        )

    async def _join_member(
        self, chat_id: str, user_id: str, report: ChatSyncReport
    ) -> None:
        membership, _ = await self._store.get_local_current_membership_for_user_in_room(
            user_id, chat_id
        )
        if membership == MEMBERSHIP_JOIN:
            report.already_joined += 1
            return
        if membership == MEMBERSHIP_BAN:
            report.skipped_banned.append(user_id)
            return
        try:
            await self._join(chat_id, user_id, invited=membership == MEMBERSHIP_INVITE)
            report.joined.append(user_id)
        except Exception as e:
            self._record_failure(report, user_id, e, "join")

    async def _join(self, chat_id: str, user_id: str, invited: bool) -> None:
        """Join as the member: a default chat admits space members by its
        join rule. A chat whose rule does not (one created before the
        restricted rule) refuses with 403; then invite first, as the
        membership endpoint does, and join again."""
        try:
            await self._paced(
                lambda: server_initiated_join(self._api, chat_id, user_id)
            )
            return
        except SynapseError as e:
            if e.code != 403 or invited:
                raise
        inviter = await get_inviter_user(self._api, chat_id)
        if inviter is None:
            raise NoChatAdminError(f"No joined member can invite into {chat_id}")
        inviter_id = inviter.to_string()
        await self._paced(
            lambda: self._api.update_room_membership(
                sender=inviter_id,
                target=user_id,
                room_id=chat_id,
                new_membership=MEMBERSHIP_INVITE,
            )
        )
        await self._paced(lambda: server_initiated_join(self._api, chat_id, user_id))

    async def _paced(self, action: Callable[[], Awaitable[Any]]) -> None:
        """Run a membership or state write, waiting out Synapse's rate limits.

        Module writes are rate-limited like a client's, and the per-room join
        limit (burst 10, then one a second by default) is what a class joining
        one chat hits. Waiting is correct here: this runs in the background,
        and a dropped member is the failure this module exists to prevent.
        """
        for _ in range(MAX_RATE_LIMIT_WAITS):
            try:
                await action()
                return
            except LimitExceededError as e:
                wait_ms = e.retry_after_ms or 1000
                logger.info(
                    "course_default_chats waiting %d ms for %s", wait_ms, e.limiter_name
                )
                await self._clock.sleep(Duration(milliseconds=wait_ms))
        raise LimitExceededError(
            limiter_name="course_default_chats", retry_after_ms=None
        )

    def _record_failure(
        self, report: ChatSyncReport, user_id: str, e: Exception, step: str
    ) -> None:
        logger.warning(
            "course_default_chats %s failed for %s in %s: %s",
            step,
            user_id,
            report.chat_id,
            e,
        )
        _capture_exception(e)
        report.failed.append(
            {"user_id": user_id, "step": step, "error": f"{type(e).__name__}: {e}"}
        )


class SyncCourseDefaultChats(Resource):
    """``POST /_synapse/client/pangea/v1/sync_course_default_chats``: the
    repair endpoint. Server admins only. ``{"room_id": ...}`` syncs one
    space and answers with its per-chat report; an empty body schedules every
    space in the background and answers 202 with the count."""

    isLeaf = True

    def __init__(self, api: ModuleApi, syncer: CourseDefaultChats):
        super().__init__()
        self._api = api
        self._auth = api._hs.get_auth()
        self._syncer = syncer

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
            if room_id is None:
                space_ids = await self._syncer.all_space_ids()
                self._api.run_as_background_process(
                    "course_default_chats_repair", self._syncer.sync_spaces, space_ids
                )
                respond_with_json(
                    request, 202, {"scheduled": len(space_ids)}, send_cors=True
                )
                return

            if not isinstance(room_id, str) or not _is_valid_room_id(room_id):
                respond_with_json(
                    request,
                    400,
                    {"error": "'room_id' must be a valid room ID"},
                    send_cors=True,
                )
                return

            reports = await self._syncer.sync_space(room_id)
            if reports is None:
                respond_with_json(
                    request, 400, {"error": "'room_id' must be a space"}, send_cors=True
                )
                return
            respond_with_json(
                request,
                200,
                {"room_id": room_id, "chats": [r.as_json() for r in reports]},
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
            logger.exception("Error syncing course default chats")
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
