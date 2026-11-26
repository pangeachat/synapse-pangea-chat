"""The Protected course routes (pangeachat/.github#799), served as the
student invitation routes are: ``course_management/set`` and ``/members``
(course admin), ``/answer`` (the member asked), ``/mine`` (the caller's
managing courses, effective controls and unseen graduations),
``/graduate`` (course admin, one or all), ``/graduation_seen`` (the student)
and the Pangea team's, a Synapse server admin's: ``/manage_members``
(manages without asking) and ``/move`` (moves a student's management to
another Protected course)."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, List, Optional, Tuple

from synapse.util.async_helpers import Linearizer
from synapse.util.frozenutils import unfreeze

from synapse_pangea_chat.joined_via import module_write
from synapse_pangea_chat.student_invitations.accounts import Accounts
from synapse_pangea_chat.student_invitations.api import (
    FORBIDDEN,
    NOT_FOUND,
    Result,
    _bad,
    _conflict,
    valid_room_id,
)
from synapse_pangea_chat.student_invitations.claim import CourseAdmins, now_ms
from synapse_pangea_chat.student_invitations.controls import OTHER_COURSES, Controls
from synapse_pangea_chat.student_invitations.graduation import Graduation
from synapse_pangea_chat.student_invitations.managed_events import CONTROL_CHANGED
from synapse_pangea_chat.student_invitations.protected import (
    CONTROLS,
    COURSE_MANAGEMENT,
    DEFAULTS,
    KNOCK,
    how_joined,
    management,
    protected_since,
)
from synapse_pangea_chat.student_invitations.store import (
    ASKED,
    DECLINED,
    MANAGED,
    StudentInvitationStore,
)

MAX_MEMBERS = 500
TEAM_ONLY = (403, {"error": "Forbidden", "errcode": "M_FORBIDDEN"})
NOT_PROTECTED = ("ORG.PANGEA.NOT_PROTECTED", "The course is not Protected")
NOT_VERIFIED = (
    403,
    {
        "error": "Only a verified educator can make a course Protected",
        "errcode": "ORG.PANGEA.NOT_VERIFIED_EDUCATOR",
    },
)


async def is_verified_educator(user_id: str, enabled: bool) -> bool:
    """Whether ``user_id`` may make a course Protected: a verified educator
    (pangeachat/.github#793). That record does not exist yet, so until it
    does, the ``protected_courses_enabled`` config flag, off by default,
    stands in for it, and no course can be made Protected."""
    return enabled


def _wanted(current: dict, body: dict) -> Optional[dict]:
    """The event content the body asks for, over the current one; None when
    the body is malformed."""
    wanted = {"protected": current["protected"]}
    if "protected" in body:
        if not isinstance(body["protected"], bool):
            return None
        wanted["protected"] = body["protected"]
    for group in DEFAULTS:
        given = body.get(group, {})
        if not isinstance(given, dict) or not all(
            name in CONTROLS and isinstance(on, bool) for name, on in given.items()
        ):
            return None
        wanted[group] = {**current[group], **given}
    return wanted


class ProtectedCourseHandlers:
    def __init__(
        self,
        api: Any,
        store: StudentInvitationStore,
        admins: CourseAdmins,
        accounts: Accounts,
        unmanaged: Callable[[str, str], Awaitable[bool]],
        enabled: bool,
        controls: Controls,
        graduation: Graduation,
    ) -> None:
        self._api = api
        self._main = api._hs.get_datastores().main
        self._store = store
        self._admins = admins
        self._accounts = accounts
        self._unmanaged = unmanaged
        self._enabled = enabled
        self._controls = controls
        self._graduation = graduation
        self._locks = Linearizer("pangea_course_management", clock=api._hs.get_clock())

    def _room(self, args: Any) -> Optional[str]:
        return valid_room_id(args.get("room_id")) if isinstance(args, dict) else None

    async def set(self, caller: str, body: Any) -> Result:
        room_id = self._room(body)
        if room_id is None:
            return _bad("'room_id' must be a valid Matrix room ID")
        if not await self._admins.is_course_admin(room_id, caller):
            return 403, FORBIDDEN
        # One update of a course at a time in this process, read inside the
        # lock: a stale read never overwrites a newer write. Across processes
        # the rules refuse turning protection off (``ProtectedRules``).
        async with self._locks.queue(room_id):
            return await self._set(room_id, caller, body)

    async def _set(self, room_id: str, caller: str, body: Any) -> Result:
        state = await self._api.get_room_state(
            room_id, [(COURSE_MANAGEMENT, ""), ("m.room.join_rules", "")]
        )
        event = state.get((COURSE_MANAGEMENT, ""))
        current = management(event.content if event is not None else None)
        wanted = _wanted(current, body)
        if wanted is None:
            return _bad("'protected' and each control must be booleans")
        if current["protected"] and not wanted["protected"]:
            return _conflict("ORG.PANGEA.PROTECTED_STAYS", "A course stays Protected")
        turning_on = wanted["protected"] and not current["protected"]
        if turning_on and not await is_verified_educator(caller, self._enabled):
            return NOT_VERIFIED
        if wanted["protected"]:
            await self._api.public_room_list_manager.remove_room_from_public_room_list(
                room_id
            )
            rules = state.get(("m.room.join_rules", ""))
            content = unfreeze(rules.content) if rules is not None else {}
            if content.get("join_rule") != KNOCK:
                await self._api.create_and_send_event_into_room(
                    {
                        "type": "m.room.join_rules",
                        "room_id": room_id,
                        "sender": caller,
                        "state_key": "",
                        "content": {**content, "join_rule": KNOCK},
                    }
                )
        if wanted != current:
            with module_write():
                await self._api.create_and_send_event_into_room(
                    {
                        "type": COURSE_MANAGEMENT,
                        "room_id": room_id,
                        "sender": caller,
                        "state_key": "",
                        "content": wanted,
                    }
                )
        # On every update of a Protected course, not only the one turning it
        # on, so a retry finishes a setup that failed after the flag.
        asked = await self._ask(room_id, caller) if wanted["protected"] else []
        changes = [
            f"{group}.{name}={'on' if wanted[group][name] else 'off'}"
            for group in DEFAULTS
            for name in CONTROLS
            if wanted[group][name] != current[group][name]
        ]
        if changes:
            await self._store.record_managed(
                room_id, CONTROL_CHANGED, caller, now_ms(), details=changes
            )
        moved = any(
            wanted[g][OTHER_COURSES] != current[g][OTHER_COURSES] for g in DEFAULTS
        )
        if wanted["protected"] and moved:
            # Take the students out of, or put them back into, course rooms.
            records = await self._store.managed_in_room(room_id)
            self._controls.reconcile_later(
                r["user_id"] for r in records if r["status"] == MANAGED
            )
        return 200, {"management": wanted, "asked": asked}

    async def _ask(self, room_id: str, caller: str) -> List[str]:
        """Ask each member already in the course when it became Protected
        (``store.ask``). Members are asked after the flag, so the join hook
        covers anyone joining from then on; records made since are the
        hook's or an answer's, and are left alone."""
        state = await self._api.get_room_state(
            room_id, [("m.room.member", None), (COURSE_MANAGEMENT, "")]
        )
        since = await protected_since(self._main, state.get((COURSE_MANAGEMENT, "")))
        members: List[Tuple[str, str, Optional[int]]] = []
        for (kind, user_id), member in state.items():
            if kind != "m.room.member" or member.content.get("membership") != "join":
                continue
            if await self._unmanaged(room_id, user_id):
                continue
            via, joined_at_ms = await how_joined(self._main, member)
            members.append((user_id, via, joined_at_ms))
        return await self._store.ask(room_id, members, caller, now_ms(), since)

    async def members(self, caller: str, query: Any) -> Result:
        room_id = self._room(query)
        if room_id is None:
            return _bad("'room_id' must be a valid Matrix room ID")
        if not await self._admins.is_course_admin(room_id, caller):
            return 403, FORBIDDEN
        return 200, {"members": await self._store.managed_in_room(room_id)}

    async def answer(self, caller: str, body: Any) -> Result:
        room_id = self._room(body)
        if room_id is None or not isinstance(body.get("accept"), bool):
            return _bad("'room_id' and 'accept' are required")
        status = await self._store.answer(
            room_id, caller, accept=body["accept"], actor=caller, now_ms=now_ms()
        )
        if status is None:
            return 404, NOT_FOUND
        if status == MANAGED:
            self._controls.reconcile_later([caller])
        return 200, {"status": status}

    async def mine(self, caller: str) -> Result:
        status = await self._controls.status(caller)
        status["graduations"] = await self._store.unseen_graduations(caller)
        return 200, status

    async def graduate(self, caller: str, body: Any) -> Result:
        """Graduate ``user_ids``, or with ``all`` every managed student."""
        room_id = self._room(body)
        user_ids = body.get("user_ids") if isinstance(body, dict) else None
        everyone = isinstance(body, dict) and body.get("all") is True
        listed = (
            isinstance(user_ids, list)
            and 1 <= len(user_ids) <= MAX_MEMBERS
            and all(isinstance(u, str) for u in user_ids)
        )
        if room_id is None or everyone == listed:
            return _bad("'room_id' and either 'user_ids' or 'all' are required")
        if not await self._admins.is_course_admin(room_id, caller):
            return 403, FORBIDDEN
        if not await self._admins.is_protected(room_id):
            return _conflict(*NOT_PROTECTED)
        wanted = None if everyone else user_ids
        graduated = await self._store.graduate(room_id, wanted, caller, now_ms())
        # The effects run on every graduated student the student has not yet
        # seen graduate, so a retry repairs effects that failed; they are
        # idempotent. Not once seen: the student may have shared their
        # analytics again, which a repeat would undo.
        repair = await self._store.unseen_graduates(room_id, wanted)
        self._graduation.later(room_id, repair)
        return 200, {"graduated": graduated}

    async def graduation_seen(self, caller: str, body: Any) -> Result:
        room_id = self._room(body)
        if room_id is None:
            return _bad("'room_id' must be a valid Matrix room ID")
        if not await self._store.see_graduation(caller, room_id, now_ms()):
            return 404, NOT_FOUND
        return 200, {}

    async def move(self, caller: str, body: Any) -> Result:
        """The team moves a student's management to another Protected course
        in one step; their memberships are moved with the existing tools."""
        args = body if isinstance(body, dict) else {}
        source = valid_room_id(args.get("from_room_id"))
        target = valid_room_id(args.get("to_room_id"))
        user_id = args.get("user_id")
        if source is None or target is None or source == target:
            return _bad("'from_room_id' and a different 'to_room_id' are required")
        if not isinstance(user_id, str):
            return _bad("'user_id' is required")
        if not await self._accounts.is_server_admin(caller):
            return TEAM_ONLY
        # A record only manages in a Protected course (``Controls.managing``).
        for room_id in (source, target):
            if not await self._admins.is_protected(room_id):
                return _conflict(*NOT_PROTECTED)
        if not await self._store.move_managed(
            user_id, source, target, caller, now_ms()
        ):
            return 404, NOT_FOUND
        self._controls.reconcile_later([user_id])
        return 200, {"moved": True}

    async def manage(self, caller: str, body: Any) -> Result:
        room_id = self._room(body)
        user_ids = body.get("user_ids") if isinstance(body, dict) else None
        if (
            room_id is None
            or not isinstance(user_ids, list)
            or not 1 <= len(user_ids) <= MAX_MEMBERS
            or not all(isinstance(u, str) for u in user_ids)
        ):
            return _bad(f"'room_id' and 1 to {MAX_MEMBERS} 'user_ids' are required")
        if not await self._accounts.is_server_admin(caller):
            return TEAM_ONLY
        managed = []
        for user_id in user_ids:
            status = await self._store.answer(
                room_id,
                user_id,
                accept=True,
                actor=caller,
                now_ms=now_ms(),
                from_statuses=(ASKED, DECLINED),
            )
            if status is not None:
                managed.append(user_id)
        self._controls.reconcile_later(managed)
        return 200, {"managed": managed}
