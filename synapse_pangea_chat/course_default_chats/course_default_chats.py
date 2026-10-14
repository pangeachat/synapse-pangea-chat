"""Keep a space's default chats (Announcements, Introductions) complete.

Every member of the space is joined to each default chat, and the space's
admins are the chat's admins. Design, including what counts as a space, a
default chat, a member and an admin: course-default-chats.instructions.md.
"""

from __future__ import annotations

import logging
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    cast,
)

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

# The per-process memo of each space's default chat ids is small, but a
# server with many spaces should not grow it without bound.
MAX_CACHED_SPACES = 20000


def is_default_chat_alias(alias: Any) -> bool:
    """Whether a canonical alias such as ``#announcementsChat_x_1:server``
    marks a default chat - the client's test, on the alias localpart."""
    if not isinstance(alias, str) or not alias.startswith("#"):
        return False
    localpart = alias[1:].split(":", 1)[0]
    return localpart.startswith(DEFAULT_CHAT_ALIAS_PREFIXES)


def _power_of(users: Mapping[str, Any], user_id: str, users_default: int) -> int:
    return _coerce_int(users.get(user_id, users_default), users_default)


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
    """No joined member of the chat can do what the sync needs: send its power
    levels, or let a member in through its join rule. Nobody is promoted to
    fix that; it is reported for an operator."""


@attr.s(auto_attribs=True, frozen=True)
class SpaceRoster:
    members: FrozenSet[str]
    admins: FrozenSet[str]


@attr.s(auto_attribs=True, frozen=True)
class ChatState:
    power_content: Dict[str, Any]
    memberships: Dict[str, str]
    immutable: FrozenSet[str]
    creator: Optional[str] = None

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

    def power(self, user_id: str) -> int:
        if user_id in self.immutable:
            return ADMIN_POWER_LEVEL
        return _power_of(self.users, user_id, self.users_default)

    def can_admit_members(self) -> bool:
        """Whether a restricted join can be authorised: Synapse needs a joined
        local member at or above the chat's invite level to vouch for it."""
        invite_level = _coerce_int(self.power_content.get("invite"))
        return any(self.power(user_id) >= invite_level for user_id in self.joined)

    def joined_admins(self) -> FrozenSet[str]:
        return frozenset(
            user_id
            for user_id in self.joined
            if self.power(user_id) >= ADMIN_POWER_LEVEL
        )


def owned_by_space_admin(chat: ChatState, space_admins: FrozenSet[str]) -> bool:
    """Whether the chat is the space admins' own: its creator is one, or one
    already holds admin power in it. Any member can link a child to a space
    (activity sessions fan out into courses that way), so on the alias
    prefix alone a student could create a room and have the server put the
    whole class in it."""
    if chat.creator is not None and chat.creator in space_admins:
        return True
    return any(chat.power(admin) >= ADMIN_POWER_LEVEL for admin in space_admins)


def _coerce_int(value: Any, default: int = 0) -> int:
    """A power level as an int. Room versions before v10 let a non-integer
    through; it is read as the default and said so, never raised on."""
    if value is None:
        return default
    if not isinstance(value, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    logger.warning("Non-integer power level %r; using %d", value, default)
    return default


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
    # Set, with the reason, when the chat carries the mark but is not the
    # space admins' own; nothing else in the report is then filled in.
    ignored: Optional[str] = None

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
        # One sync per chat at a time in this process: the admin mirror is a
        # read-modify-write of the chat's power levels, and two triggers on
        # one space (a link and a join, say) can land together. Across
        # workers the sync is idempotent and converges on the next trigger.
        self._chat_locks = Linearizer("course_default_chats", clock=self._clock)
        # Each space's default chat ids, so a member join does not re-read
        # every child of a busy course (activity sessions are children too).
        # Invalidated by the m.space.child events this process's hook sees;
        # the repair endpoint bypasses it.
        self._default_chats: Dict[str, List[str]] = {}
        # A chat nobody can act for is reported once per process, not on
        # every join and profile change in its space.
        self._reported_no_admin: Set[str] = set()
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
                self._default_chats.pop(event.room_id, None)
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
                        "member",
                        self.sync_member,
                        event.room_id,
                        user_id,
                        event.unsigned.get("replaces_state"),
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
        # Through the reactor rather than from the notifier's own logcontext,
        # so the background process starts from the sentinel context and the
        # notifier's is never re-entered after it finishes (the pattern the
        # moderation dispatcher documents).
        self._clock.call_later(
            Duration(milliseconds=0),
            self._api.run_as_background_process,
            f"course_default_chats_{what}",
            func,
            *args,
        )

    # ------------------------------------------------------------------
    # Sync entry points (each returns None when the room is not a space)
    # ------------------------------------------------------------------

    async def is_space(self, room_id: str) -> bool:
        create = await self._state.get_current_state_event(
            room_id, EventTypes.Create, ""
        )
        return create is not None and create.content.get("type") == SPACE_ROOM_TYPE

    async def sync_space(
        self, space_id: str, *, apply: bool = True, use_cache: bool = True
    ) -> Optional[List[ChatSyncReport]]:
        """Bring every default chat of the space in line with the space.
        With ``apply`` false, report what would change and change nothing."""
        roster = await self._space_roster(space_id)
        if roster is None:
            return None
        return [
            await self._sync_chat(
                space_id, chat_id, roster, only_user=None, apply=apply
            )
            for chat_id in await self._default_chat_ids(space_id, use_cache)
        ]

    async def sync_chat(self, space_id: str, chat_id: str) -> Optional[ChatSyncReport]:
        """A child was linked to the space: a full sync if it is a default chat."""
        roster = await self._space_roster(space_id)
        if roster is None or not await self._is_default_chat(chat_id):
            return None
        return await self._sync_chat(
            space_id, chat_id, roster, only_user=None, apply=True
        )

    async def sync_member(
        self, space_id: str, user_id: str, replaces_state: Optional[str]
    ) -> Optional[List[ChatSyncReport]]:
        """A user joined the space: join them to its default chats.

        A profile change is also a join event (Synapse re-sends a member's
        join into every room on a displayname or avatar change). That is not
        a join, so a member who was removed from a chat is not put back by
        it; the previous member event tells the two apart."""
        if replaces_state is not None:
            previous = await self._store.get_event(replaces_state, allow_none=True)
            if (
                previous is not None
                and previous.content.get("membership") == MEMBERSHIP_JOIN
            ):
                return None
        roster = await self._space_roster(space_id)
        if roster is None or user_id not in roster.members:
            return None
        return [
            await self._sync_chat(
                space_id, chat_id, roster, only_user=user_id, apply=True
            )
            for chat_id in await self._default_chat_ids(space_id, use_cache=True)
        ]

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def _space_roster(self, room_id: str) -> Optional[SpaceRoster]:
        if not await self.is_space(room_id):
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

    async def _default_chat_ids(self, space_id: str, use_cache: bool) -> List[str]:
        if use_cache and space_id in self._default_chats:
            return self._default_chats[space_id]
        state = await self._api.get_room_state(
            space_id, event_filter=[(EventTypes.SpaceChild, None)]
        )
        chat_ids = []
        for (_, child_id), event in state.items():
            if not isinstance(child_id, str) or not has_valid_via(event.content):
                continue
            if await self._is_default_chat(child_id):
                chat_ids.append(child_id)
        chat_ids.sort()
        if len(self._default_chats) >= MAX_CACHED_SPACES:
            self._default_chats.clear()
        self._default_chats[space_id] = chat_ids
        return chat_ids

    async def _is_default_chat(self, room_id: str) -> bool:
        # The directory's aliases, not the canonical-alias state event: Synapse
        # drops a room's current state once every local member has left, and
        # a chat everyone left must still be seen, so it can be reported.
        # cast: mypy reads Synapse's @cached descriptor as the unbound function.
        aliases: Sequence[str] = await cast(Any, self._store).get_aliases_for_room(
            room_id
        )
        return any(is_default_chat_alias(alias) for alias in aliases)

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
            creator=create.sender if create is not None else None,
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
        apply: bool,
    ) -> ChatSyncReport:
        """Promote, then join, then demote. The order matters: a demotion can
        only ever follow the joins, so it never removes the one member who
        could still let the others in."""
        report = ChatSyncReport(chat_id=chat_id)
        if apply:
            async with self._chat_locks.queue(chat_id):
                await self._sync_chat_steps(
                    space_id, chat_id, roster, only_user, apply, report
                )
        else:
            # A dry run only reads, so it never waits behind a running sync of
            # the same chat: an operator polling a large class's repair would
            # otherwise sit on the lock longer than the reverse proxy allows a
            # request. Reporting a sync in progress is what the poll is for.
            await self._sync_chat_steps(
                space_id, chat_id, roster, only_user, apply, report
            )
        logger.info(
            "course_default_chats %s %s in %s: %s",
            "synced" if apply else "dry run of",
            chat_id,
            space_id,
            report,
        )
        return report

    async def _sync_chat_steps(
        self,
        space_id: str,
        chat_id: str,
        roster: SpaceRoster,
        only_user: Optional[str],
        apply: bool,
        report: ChatSyncReport,
    ) -> None:
        chat = await self._chat_state(chat_id)
        # A chat with no current state (every local member left) has no
        # creator to judge by; it falls through to the no-admin report.
        if chat.creator is not None and not owned_by_space_admin(chat, roster.admins):
            report.ignored = (
                "the space has no joined admin"
                if not roster.admins
                else "neither created by a space admin nor holding one at admin power"
            )
            logger.info(
                "course_default_chats ignoring %s in %s: %s",
                chat_id,
                space_id,
                report.ignored,
            )
            return
        chat = await self._promote_admins(chat_id, roster, report, apply, chat)
        targets = [only_user] if only_user else sorted(roster.members)
        if not chat.can_admit_members():
            self._report_no_admin(chat_id, "no joined member can let members in", apply)
            report.admin_sender_missing = True
        for user_id in targets:
            await self._join_member(chat_id, user_id, chat, report, apply)
        if apply and targets:
            chat = await self._chat_state(chat_id)
        await self._demote_admins(chat_id, roster, chat, report, apply)

    async def _promote_admins(
        self,
        chat_id: str,
        roster: SpaceRoster,
        report: ChatSyncReport,
        apply: bool,
        chat: ChatState,
    ) -> ChatState:
        promote = admins_to_promote(
            chat.users, chat.users_default, roster.admins, chat.immutable
        )
        if not promote:
            return chat
        # One event for every promotion: an admin may set others to their
        # own level, so any joined chat admin can send it.
        sender = await select_state_sender(self._api, chat_id, EventTypes.PowerLevels)
        if sender is None:
            report.admin_sender_missing = True
            self._report_no_admin(
                chat_id,
                f"no joined admin can mirror {len(promote)} space admins",
                apply,
            )
            return chat
        if not apply:
            report.promoted = promote
            return chat
        users = chat.users
        for user_id in promote:
            users[user_id] = ADMIN_POWER_LEVEL
        try:
            await self._send_power_levels(chat_id, sender, chat, users)
            report.promoted = promote
            return await self._chat_state(chat_id)
        except Exception as e:
            self._record_failure(report, sender, e, "promote")
            return chat

    async def _demote_admins(
        self,
        chat_id: str,
        roster: SpaceRoster,
        chat: ChatState,
        report: ChatSyncReport,
        apply: bool,
    ) -> None:
        demote = admins_to_demote(
            chat.users,
            chat.users_default,
            chat.joined,
            roster.members,
            roster.admins,
            chat.immutable,
        )
        if not demote:
            return
        # Never leave the chat without a joined space admin at admin power:
        # that is who promotes the next instructor and lets members in.
        if not (chat.joined_admins() & roster.admins):
            self._report_no_admin(
                chat_id, "no joined space admin would remain after a demotion", apply
            )
            for user_id in demote:
                report.failed.append(
                    {
                        "user_id": user_id,
                        "step": "demote",
                        "error": "NoChatAdminError: no joined space admin would remain",
                    }
                )
            return
        # Matrix lets an admin lower only their own level, never another
        # admin's, so each demotion is sent by the member being demoted.
        for user_id in demote:
            if not apply:
                report.demoted.append(user_id)
                continue
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
        self,
        chat_id: str,
        user_id: str,
        chat: ChatState,
        report: ChatSyncReport,
        apply: bool,
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
        if not chat.can_admit_members():
            # Reported once for the chat above; the dry run and the real run
            # agree on what this means for each member.
            report.failed.append(
                {
                    "user_id": user_id,
                    "step": "join",
                    "error": "NoChatAdminError: no joined member can let members in",
                }
            )
            return
        if not apply:
            report.joined.append(user_id)
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

    def _report_no_admin(self, chat_id: str, reason: str, apply: bool) -> None:
        """A chat nobody can act for: logged every time, captured to Sentry
        once per process, and only when the sync would have changed something
        (a dry run reports it, it does not raise an alarm)."""
        logger.warning("course_default_chats %s: %s", chat_id, reason)
        key = f"{chat_id}:{reason}"
        if apply and key not in self._reported_no_admin:
            self._reported_no_admin.add(key)
            _capture_exception(NoChatAdminError(f"{chat_id}: {reason}"))

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
    repair endpoint. Server admins only, exactly one space per call.

    ``{"room_id": ..., "dry_run": true}`` answers the per-chat report of what
    would change and changes nothing. ``{"room_id": ...}`` answers 202 at once
    and does the work in the background: joining a large class is paced by
    Synapse's per-room join limit and outlasts what the reverse proxy allows
    a request, so the dry run before and after is the report.

    One space per call, on purpose: which spaces to touch is decided by the
    operator running ``scripts/repair_course_default_chats.py``, not by an
    endpoint that could sweep the homeserver in one request."""

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
            dry_run = body.get("dry_run", False) if isinstance(body, dict) else False
            if not isinstance(dry_run, bool):
                respond_with_json(
                    request,
                    400,
                    {"error": "'dry_run' must be a boolean"},
                    send_cors=True,
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
            if not await self._syncer.is_space(room_id):
                respond_with_json(
                    request, 400, {"error": "'room_id' must be a space"}, send_cors=True
                )
                return

            if dry_run:
                reports = await self._syncer.sync_space(
                    room_id, apply=False, use_cache=False
                )
                respond_with_json(
                    request,
                    200,
                    {
                        "room_id": room_id,
                        "dry_run": True,
                        "chats": [r.as_json() for r in reports or []],
                    },
                    send_cors=True,
                )
                return

            self._api.run_as_background_process(
                "course_default_chats_repair",
                self._syncer.sync_space,
                room_id,
                apply=True,
                use_cache=False,
            )
            respond_with_json(
                request,
                202,
                {"room_id": room_id, "dry_run": False, "scheduled": True},
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
