"""Accepting a teacher's consented analytics invite, on the student's behalf.

A student who presses Allow on a teacher's analytics request INVITES the
teacher into their analytics room; nothing in the app completes the join, so
until the teacher joins every read of that room is refused. admin-dash-api
completed the join on the teacher's login and Refresh
(``app/analytics/composition.py::_accept_consented_analytics_invites``,
admin-dash-api ``origin/main`` aa1dd067). With the dashboard's inputs pushed
from here, those paths no longer read Matrix, so the module completes it.

Consent is proven by the same FIVE facts, read from this homeserver's current
state. Dropping any one of them is a consent bypass:

1. The teacher currently HOLDS an invite into the room.
2. The room is the analytics room a student of a course the teacher
   administers declares in their profile (``pangea.analytics_profile``), and no
   other student of that course claims it. A room claimed by two students is
   ambiguous and proves nothing.
3. The invite was SENT BY that student. Any joined member may invite, so a
   teacher the student approved could otherwise invite a colleague the student
   never approved.
4. That student CREATED the room (the ``m.room.create`` sender).
5. The room DECLARES itself an analytics room (``m.room.create`` content
   ``type`` is ``p.analytics``).

A teacher who has only KNOCKED is never touched: a knock is not an invite.

:func:`consent_verdict` is the proof, kept pure so each fact has its own
accept and reject case in the tests; :class:`ConsentAcceptor` reads the facts
and makes the join.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import attr

from synapse_pangea_chat.bot_user_ids import is_probable_bot_user_id

logger = logging.getLogger("synapse.modules.synapse_pangea_chat.analytics_forward")

ANALYTICS_ROOM_TYPE = "p.analytics"
PROFILE_KEY = "pangea.analytics_profile"
COURSE_PLAN_TYPE = "pangea.course_plan"
#: The power level at or above which a course member is an instructor
#: (admin-dash-api ``reader._COURSE_ADMIN_POWER_LEVEL``).
COURSE_ADMIN_POWER_LEVEL = 100
#: admin-dash-api ``reader.BOT_MXIDS``, kept so the roster here is the one the
#: dashboard enumerates. The module's own naming rule is applied as well.
_DASHBOARD_BOT_MXIDS = frozenset(
    {
        "@bot:pangea.chat",
        "@bot:staging.pangea.chat",
        "@matrix-bot-test:staging.pangea.chat",
    }
)

ACCEPT = "accept"
REFUSED_NOT_INVITED = "not_invited"
REFUSED_NO_CLAIM = "no_course_claim"
REFUSED_OWNER = "inviter_or_creator_not_student"
REFUSED_ROOM_TYPE = "not_analytics_room"
REFUSALS = (REFUSED_NOT_INVITED, REFUSED_NO_CLAIM, REFUSED_OWNER, REFUSED_ROOM_TYPE)


@attr.s(auto_attribs=True, frozen=True)
class InviteFacts:
    """What the homeserver says about one (room, teacher) pair right now."""

    room_id: str
    invitee: str
    #: The invitee's current membership in the room (``None`` when none).
    membership: Optional[str]
    #: The sender of that membership event.
    inviter: Optional[str]
    #: The ``m.room.create`` sender and ``content.type``.
    creator: Optional[str]
    room_type: Optional[str]


@attr.s(auto_attribs=True, frozen=True)
class CourseFacts:
    """One course both people are joined to, as the dashboard would read it."""

    course_id: str
    #: Whether the invitee administers the course (joined, power >= 100).
    invitee_is_admin: bool
    #: Each enrolled student and the analytics rooms their profile declares
    #: for the course language (all languages when it is unknown).
    student_rooms: Mapping[str, Sequence[str]]


def consent_verdict(invite: InviteFacts, courses: Iterable[CourseFacts]) -> str:
    """``ACCEPT``, or the first fact that failed (one of ``REFUSALS``)."""
    # Fact 1.
    if invite.membership != "invite":
        return REFUSED_NOT_INVITED
    # Fact 2: the student who claims the room in a course the teacher runs.
    owner: Optional[str] = None
    for course in courses:
        if not course.invitee_is_admin:
            continue
        claimants = {
            student
            for student, rooms in course.student_rooms.items()
            if invite.room_id in rooms
        }
        if len(claimants) == 1:
            owner = next(iter(claimants))
            # The claimant must be the inviter (fact 3); a course where it is
            # someone else is not proof, but another course still may be.
            if invite.inviter == owner:
                break
    if owner is None:
        return REFUSED_NO_CLAIM
    # Facts 3 and 4.
    if invite.inviter != owner or invite.creator != owner:
        return REFUSED_OWNER
    # Fact 5.
    if invite.room_type != ANALYTICS_ROOM_TYPE:
        return REFUSED_ROOM_TYPE
    return ACCEPT


# ---------------------------------------------------------------------------
# Profile and power-level reading, ported from admin-dash-api ``reader.py``.
# ---------------------------------------------------------------------------


def _primary_subtag(lang: str) -> str:
    return lang.split("-", 1)[0].strip().lower()


def _entry_room_id(entry: Any) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    room_id = entry.get("analytics_room_id")
    return room_id if isinstance(room_id, str) and room_id else None


def profile_analytics_rooms(profile: Any, language: str) -> List[str]:
    """The analytics rooms a profile declares (``_analytics_room_ids_for_languages``).

    With a course language: the one room for it, matched on the primary
    subtag, the exact key winning over a legacy per-locale key. Without one:
    every room in the profile.
    """
    if not isinstance(profile, dict):
        return []
    block = profile.get(PROFILE_KEY)
    analytics = block.get("analytics") if isinstance(block, dict) else None
    if not isinstance(analytics, dict):
        return []
    if language:
        want = _primary_subtag(language)
        variants = sorted(
            (key, entry)
            for key, entry in analytics.items()
            if isinstance(key, str) and _primary_subtag(key) == want
        )
        chosen: Optional[str] = None
        for key, entry in variants:
            candidate = _entry_room_id(entry)
            if candidate is not None and (key == want or chosen is None):
                chosen = candidate
        return [chosen] if chosen is not None else []
    out: List[str] = []
    for entry in analytics.values():
        room_id = _entry_room_id(entry)
        if room_id is not None and room_id not in out:
            out.append(room_id)
    return out


def caller_power_level(powers: Mapping[str, Any], user_id: str) -> int:
    """Effective power for ADMISSION; a malformed entry is 0 (fails closed)."""
    if "users" in powers:
        users = powers["users"]
        if not isinstance(users, dict):
            return 0
        if user_id in users:
            explicit = users[user_id]
            return explicit if isinstance(explicit, int) else 0
    default = powers.get("users_default", 0)
    return default if isinstance(default, int) else 0


def confirmed_below_course_admin(powers: Mapping[str, Any], user_id: str) -> bool:
    """Whether a member is POSITIVELY a student; malformed power is not."""
    if "users" in powers:
        users = powers["users"]
        if not isinstance(users, dict):
            return False
        if user_id in users:
            explicit = users[user_id]
            if isinstance(explicit, bool) or not isinstance(explicit, int):
                return False
            return explicit < COURSE_ADMIN_POWER_LEVEL
    default = powers.get("users_default", 0)
    if isinstance(default, bool) or not isinstance(default, int):
        return False
    return default < COURSE_ADMIN_POWER_LEVEL


def is_bot(user_id: str) -> bool:
    return user_id in _DASHBOARD_BOT_MXIDS or is_probable_bot_user_id(user_id)


def course_language(course_plan_content: Any) -> str:
    """The course language the ``pangea.course_plan`` event names, or ``""``."""
    if not isinstance(course_plan_content, dict):
        return ""
    l2 = course_plan_content.get("l2")
    return l2 if isinstance(l2, str) else ""


# ---------------------------------------------------------------------------
# Reading the facts and making the join.
# ---------------------------------------------------------------------------


class ConsentAcceptor:
    """Reads the five facts from this homeserver and joins on ``ACCEPT``."""

    def __init__(self, api: Any) -> None:
        from synapse.types.state import StateFilter

        self._api = api
        self._store = api._hs.get_datastores().main
        self._state = api._hs.get_storage_controllers().state
        self._course_filter = StateFilter.from_types(
            [
                ("m.room.create", ""),
                ("m.room.power_levels", ""),
                (COURSE_PLAN_TYPE, ""),
            ]
        )

    async def invite_facts(self, room_id: str, invitee: str) -> InviteFacts:
        (
            membership,
            event_id,
        ) = await self._store.get_local_current_membership_for_user_in_room(
            invitee, room_id
        )
        inviter: Optional[str] = None
        if event_id is not None:
            event = await self._store.get_event(event_id, allow_none=True)
            inviter = event.sender if event is not None else None
        create = await self._state.get_current_state_event(room_id, "m.room.create", "")
        creator = create.sender if create is not None else None
        room_type = create.content.get("type") if create is not None else None
        return InviteFacts(
            room_id=room_id,
            invitee=invitee,
            membership=membership,
            inviter=inviter,
            creator=creator,
            room_type=room_type if isinstance(room_type, str) else None,
        )

    async def course_facts(self, invitee: str, inviter: str) -> List[CourseFacts]:
        """The courses both are joined to.

        Only courses where the inviter is a member can prove consent: fact 3
        needs the claiming student to BE the inviter, and a claiming student
        is a joined member. So the shared rooms are all that is read.
        """
        shared = set(await self._store.get_rooms_for_user(invitee)) & set(
            await self._store.get_rooms_for_user(inviter)
        )
        out: List[CourseFacts] = []
        for course_id in sorted(shared):
            state = await self._state.get_current_state(
                course_id, state_filter=self._course_filter
            )
            plan = state.get((COURSE_PLAN_TYPE, ""))
            if plan is None:
                continue
            power_event = state.get(("m.room.power_levels", ""))
            powers: Dict[str, Any] = (
                dict(power_event.content) if power_event is not None else {}
            )
            members = await self._store.get_users_in_room(course_id)
            invitee_is_admin = (
                invitee in members
                and caller_power_level(powers, invitee) >= COURSE_ADMIN_POWER_LEVEL
            )
            students = [
                uid
                for uid in members
                if not is_bot(uid) and confirmed_below_course_admin(powers, uid)
            ]
            profiles = await self._store.get_profile_data_for_users(students)
            language = course_language(plan.content)
            out.append(
                CourseFacts(
                    course_id=course_id,
                    invitee_is_admin=invitee_is_admin,
                    student_rooms={
                        uid: profile_analytics_rooms(profiles.get(uid), language)
                        for uid in students
                    },
                )
            )
        return out

    async def accept_if_consented(self, room_id: str, invitee: str) -> Tuple[str, int]:
        """Run the proof and join on ``ACCEPT``.

        Returns the verdict and the stream position of the teacher's CURRENT
        join in the room (the one just made, or an earlier one), or 0 when the
        teacher is not joined. That position is the delivery fence: the
        ingest must not report a Refresh complete before it is delivered.
        """
        facts = await self.invite_facts(room_id, invitee)
        if facts.room_type != ANALYTICS_ROOM_TYPE:
            # Fact 5 first here: an invite into any other room (most of them)
            # costs no course reads and never raises the fence.
            return REFUSED_ROOM_TYPE, 0
        if facts.membership == "join":
            return REFUSED_NOT_INVITED, await self._join_position(room_id, invitee)
        if facts.inviter is None:
            return REFUSED_NOT_INVITED, 0
        courses = await self.course_facts(invitee, facts.inviter)
        verdict = consent_verdict(facts, courses)
        if verdict != ACCEPT:
            logger.info("consented-invite refused: %s", verdict)
            return verdict, 0
        # The course reads awaited; the invite may have been replaced since
        # (revoked, then re-sent by someone else). Join only on the invite
        # that was proven.
        if await self.invite_facts(room_id, invitee) != facts:
            logger.info("consented-invite refused: the invite changed")
            return REFUSED_NOT_INVITED, 0
        from synapse_pangea_chat.blocked_join_gate import server_initiated_join

        await server_initiated_join(self._api, room_id, invitee)
        return ACCEPT, await self._join_position(room_id, invitee)

    async def _join_position(self, room_id: str, invitee: str) -> int:
        (
            membership,
            event_id,
        ) = await self._store.get_local_current_membership_for_user_in_room(
            invitee, room_id
        )
        if membership != "join" or event_id is None:
            return 0
        event = await self._store.get_event(event_id, allow_none=True)
        if event is None:
            return 0
        position = event.internal_metadata.stream_ordering
        return position if isinstance(position, int) and position > 0 else 0
