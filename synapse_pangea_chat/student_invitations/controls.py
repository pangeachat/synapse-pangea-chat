"""What a managed student may do (pangeachat/.github#799, managed-accounts
"What the course admin controls"), answered from their managed records and
each managing course's ``pangea.course_management`` event, and enforced from
Synapse's spam-checker callbacks.

Every managing course must allow an action (the stricter wins); a course's
controls skip the student while they are its admin. A room a course owns
(``moderation.courses``) needs ``other_courses``; a direct message is a room no
course owns whose joined and invited members are two people, and needs
``dms`` both ways; a call event needs ``calls`` in any room, both ways in a
direct message. The admins of every managing course and the configured
always-reachable accounts can always be messaged and called. A state that
cannot be read raises, and the request fails: never allowed by default.
"""

from __future__ import annotations

import logging
from typing import (
    Any,
    Collection,
    Dict,
    Iterable,
    List,
    Literal,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from synapse.api.errors import Codes
from synapse.module_api import NOT_SPAM

from synapse_pangea_chat.joined_via import REMOVED_BY, module_write
from synapse_pangea_chat.moderation.courses import ChatCourses
from synapse_pangea_chat.student_invitations.protected import (
    CONTROLS,
    COURSE_MANAGEMENT,
    DEFAULTS,
    is_protected,
    management,
)
from synapse_pangea_chat.student_invitations.report import report_failure

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.student_invitations.controls"
)

OTHER_COURSES, DMS, CALLS = CONTROLS
OUTSIDE, IN_COURSE = DEFAULTS
# The refusing control, as "<group>.<control>", in the error body.
CONTROL_FIELD = "org.pangea.control"
CALL_PREFIXES = (
    "m.call.",
    "org.matrix.call.",
    "com.famedly.call.",
    "org.matrix.msc3401.call",
)
SPACE = "m.space"

Answer = Union[Literal["NOT_SPAM"], Tuple[Codes, dict]]


class Managing(NamedTuple):
    """One course managing the student: its controls (``management``) and
    whether the student is its admin."""

    room_id: str
    controls: Dict[str, Any]
    is_admin: bool


def _need(course: Managing, inside: bool, name: str) -> Optional[str]:
    group = IN_COURSE if inside else OUTSIDE
    return None if course.controls[group][name] else f"{group}.{name}"


def _first(courses: Sequence[Managing], inside: Any, name: str) -> Optional[str]:
    """The first refusal among the courses that are not the student's to
    admin; ``inside(course)`` says whether the action is within it."""
    for course in courses:
        if not course.is_admin:
            refusal = _need(course, inside(course), name)
            if refusal is not None:
                return refusal
    return None


def in_room(
    courses: Sequence[Managing], room_id: str, owners: Collection[str]
) -> Optional[str]:
    """Joining, or being and sending in, a room the courses ``owners`` own:
    inside a managing course when it owns the room, and always allowed in
    that course's own space."""
    if not owners:
        return None
    others = [course for course in courses if course.room_id != room_id]
    return _first(others, lambda c: c.room_id in owners, OTHER_COURSES)


def with_person(
    courses: Sequence[Managing], name: str, classmate_in: Collection[str]
) -> Optional[str]:
    """A direct message or call with someone joined to ``classmate_in``."""
    return _first(courses, lambda c: c.room_id in classmate_in, name)


def call_in_room(courses: Sequence[Managing], owners: Collection[str]) -> Optional[str]:
    return _first(courses, lambda c: c.room_id in owners, CALLS)


def outside(courses: Sequence[Managing], name: str) -> Optional[str]:
    """Browsing listed courses or creating one: outside every course."""
    return _first(courses, lambda _: False, name)


def effective(courses: Sequence[Managing]) -> Dict[str, Dict[str, bool]]:
    active = [course for course in courses if not course.is_admin]
    return {
        group: {name: all(c.controls[group][name] for c in active) for name in CONTROLS}
        for group in DEFAULTS
    }


def is_call(event_type: str) -> bool:
    return event_type.startswith(CALL_PREFIXES)


def refused(control: Optional[str]) -> Answer:
    if control is None:
        return NOT_SPAM
    return Codes.FORBIDDEN, {CONTROL_FIELD: control}


class Controls:
    """The spam-checker callbacks, the caller's status, and ``reconcile``,
    which takes a student out of the course rooms the controls refuse and
    puts them back into the ones they allow."""

    def __init__(
        self,
        api: Any,
        store: Any,
        admins: Any,
        joiner: Any,
        reachable: Collection[str],
    ) -> None:
        self._api = api
        self._main = api._hs.get_datastores().main
        self._store = store
        self._admins = admins
        self._joiner = joiner
        self._reachable = frozenset(reachable)
        self._courses = ChatCourses.from_homeserver(api._hs)

    def register(self) -> None:
        self._api.register_spam_checker_callbacks(
            check_event_for_spam=self.check_event_for_spam,
            user_may_join_room=self.user_may_join_room,
            user_may_invite=self.user_may_invite,
            user_may_send_3pid_invite=self.user_may_send_3pid_invite,
            user_may_create_room=self.user_may_create_room,
        )

    # --- reads ---

    async def managing(self, user_ids: Iterable[str]) -> Dict[str, List[Managing]]:
        """Each managed user's managing courses; users not managed are left
        out. Every seat claim writes a managed record, in any course: only a
        Protected course's manages."""
        rows = await self._store.managing_courses(sorted(set(user_ids)))
        result: Dict[str, List[Managing]] = {}
        for user_id, room_ids in rows.items():
            for room_id in room_ids:
                state = await self._api.get_room_state(
                    room_id, [(COURSE_MANAGEMENT, "")]
                )
                if not is_protected(state):
                    continue
                result.setdefault(user_id, []).append(
                    Managing(
                        room_id,
                        management(state[(COURSE_MANAGEMENT, "")].content),
                        await self._admins.is_course_admin(room_id, user_id),
                    )
                )
        return result

    async def _owners(self, room_id: str) -> Tuple[str, ...]:
        return await self._courses.for_room(room_id, self._courses.position_now())

    async def _members(self, room_id: str) -> Set[str]:
        """Joined and invited members."""
        state = await self._api.get_room_state(room_id, [("m.room.member", None)])
        return {
            user_id
            for (_, user_id), event in state.items()
            if event.content.get("membership") in ("join", "invite")
        }

    async def _dm_other(self, room_id: str, user_id: str) -> Optional[str]:
        """The other person when the room's joined and invited members are
        ``user_id`` and one other; whether a course owns it is checked
        later."""
        if len(await self._main.get_users_in_room(room_id)) > 2:
            return None
        others = await self._members(room_id) - {user_id}
        return next(iter(others)) if len(others) == 1 else None

    async def _with(
        self, courses: Sequence[Managing], other: str, names: Sequence[str]
    ) -> Optional[str]:
        """A direct message (and, for a call, a call) with ``other``."""
        if other in self._reachable:
            return None
        classmate_in = set()
        for course in courses:
            if await self._admins.is_course_admin(course.room_id, other):
                return None
            (
                membership,
                _,
            ) = await self._main.get_local_current_membership_for_user_in_room(
                other, course.room_id
            )
            if membership == "join":
                classmate_in.add(course.room_id)
        for name in names:
            refusal = with_person(courses, name, classmate_in)
            if refusal is not None:
                return refusal
        return None

    async def _both_ways(
        self,
        managed: Dict[str, List[Managing]],
        pair: Tuple[str, str],
        names: Sequence[str],
    ) -> Optional[str]:
        for user_id, other in (pair, pair[::-1]):
            if user_id in managed:
                refusal = await self._with(managed[user_id], other, names)
                if refusal is not None:
                    return refusal
        return None

    @staticmethod
    def _present(
        managed: Dict[str, List[Managing]],
        room_id: str,
        owners: Collection[str],
        parties: Iterable[str],
    ) -> Answer:
        """Each managed party's being in a course room."""
        for user_id in parties:
            if user_id in managed:
                refusal = in_room(managed[user_id], room_id, owners)
                if refusal is not None:
                    return refused(refusal)
        return NOT_SPAM

    # --- spam-checker callbacks ---

    async def check_event_for_spam(self, event: Any) -> Answer:
        sender, room_id = event.sender, event.room_id
        other = await self._dm_other(room_id, sender)
        managed = await self.managing([sender] + ([other] if other else []))
        if not managed:
            return NOT_SPAM
        call = is_call(event.type)
        if event.type == "m.room.redaction" and event.redacts:
            target = await self._main.get_event(event.redacts, allow_none=True)
            call = target is not None and is_call(target.type)
        owners = await self._owners(room_id)
        courses = managed.get(sender)
        if owners:
            if courses is None:
                return NOT_SPAM
            return refused(
                in_room(courses, room_id, owners)
                or (call_in_room(courses, owners) if call else None)
            )
        if other is not None:
            names = (DMS, CALLS) if call else (DMS,)
            return refused(await self._both_ways(managed, (sender, other), names))
        return refused(call_in_room(courses, ()) if call and courses else None)

    async def user_may_join_room(
        self, user_id: str, room_id: str, is_invited: bool
    ) -> Answer:
        other = await self._dm_other(room_id, user_id)
        managed = await self.managing([user_id] + ([other] if other else []))
        if not managed:
            return NOT_SPAM
        owners = await self._owners(room_id)
        if owners:
            return self._present(managed, room_id, owners, [user_id])
        if other is None:
            return NOT_SPAM
        return refused(await self._both_ways(managed, (user_id, other), (DMS,)))

    async def user_may_invite(self, inviter: str, invitee: str, room_id: str) -> Answer:
        managed = await self.managing([inviter, invitee])
        if not managed:
            return NOT_SPAM
        owners = await self._owners(room_id)
        if owners:
            return self._present(managed, room_id, owners, [invitee, inviter])
        members = await self._members(room_id)
        # A room with only its creator: ``user_may_create_room`` judged its
        # invitations as a whole, which go out one at a time; a later join or
        # send in a DM is checked either way.
        if members == {inviter} or members | {invitee} != {inviter, invitee}:
            return NOT_SPAM
        return refused(await self._both_ways(managed, (inviter, invitee), (DMS,)))

    async def user_may_send_3pid_invite(
        self, inviter: str, medium: str, address: str, room_id: str
    ) -> Answer:
        """An email invitation no account answers yet: someone outside."""
        managed = await self.managing([inviter])
        if not managed:
            return NOT_SPAM
        owners = await self._owners(room_id)
        if owners:
            return self._present(managed, room_id, owners, [inviter])
        if await self._members(room_id) != {inviter}:
            return NOT_SPAM
        return refused(with_person(managed[inviter], DMS, ()))

    async def user_may_create_room(self, user_id: str, config: dict) -> Answer:
        invites = [u for u in config.get("invite") or [] if isinstance(u, str)]
        emails = config.get("invite_3pid") or []
        managed = await self.managing([user_id] + invites[:1])
        if not managed:
            return NOT_SPAM
        creation = config.get("creation_content") or {}
        if user_id in managed and creation.get("type") == SPACE:
            return refused(outside(managed[user_id], OTHER_COURSES))
        if len(invites) + len(emails) != 1:
            return NOT_SPAM
        if invites:
            pair = (user_id, invites[0])
            return refused(await self._both_ways(managed, pair, (DMS,)))
        if user_id not in managed:
            return NOT_SPAM
        return refused(with_person(managed[user_id], DMS, ()))

    # --- the app and Browse ---

    async def status(self, user_id: str) -> Dict[str, Any]:
        """The caller's managing courses and effective controls."""
        courses = (await self.managing([user_id])).get(user_id, [])
        return {
            "courses": [
                {"room_id": c.room_id, "management": c.controls, "admin": c.is_admin}
                for c in courses
            ],
            "controls": effective(courses),
        }

    async def browse_refusal(self, user_id: str) -> Optional[str]:
        courses = (await self.managing([user_id])).get(user_id, [])
        return outside(courses, OTHER_COURSES)

    # --- taking out and putting back ---

    def reconcile_later(self, user_ids: Iterable[str]) -> None:
        self._api.run_as_background_process(
            "pangea_protected_reconcile", self._reconcile_all, list(user_ids)
        )

    async def _reconcile_all(self, user_ids: List[str]) -> None:
        for user_id in user_ids:
            try:
                await self.reconcile(user_id)
            except Exception as error:
                report_failure("protected controls reconcile", error, user=user_id)

    async def reconcile(self, user_id: str) -> None:
        """Leave the course rooms the controls refuse, marked ``REMOVED_BY``;
        rejoin the marked ones they allow (and every one once the student is
        no longer managed, as at graduation)."""
        courses = (await self.managing([user_id])).get(user_id, [])
        for room_id in sorted(await self._main.get_rooms_for_user(user_id)):
            owners = await self._owners(room_id)
            control = in_room(courses, room_id, owners)
            if control is not None:
                with module_write():
                    await self._api.update_room_membership(
                        user_id, user_id, room_id, "leave", {REMOVED_BY: control}
                    )
        left = await self._main.get_rooms_for_local_user_where_membership_is(
            user_id, ["leave"]
        )
        for room in left:
            event = await self._main.get_event(room.event_id, allow_none=True)
            if event is None or REMOVED_BY not in event.content:
                continue
            owners = await self._owners(room.room_id)
            if in_room(courses, room.room_id, owners) is None:
                result = await self._joiner.force_join(room.room_id, user_id, via=None)
                if not result.get("success"):
                    raise RuntimeError(f"restore into {room.room_id} failed")
