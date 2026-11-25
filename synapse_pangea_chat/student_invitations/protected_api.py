"""The Protected course routes (pangeachat/.github#799), served as the
student invitation routes are (``api.py``: token, rate limit, input, then
the caller's right).

- ``course_management/set`` (course admin): set the Protected flag and the
  controls. Making a course Protected makes it ``private`` (out of the room
  directory), then ``knock``, asks each member already in it, then writes
  the flag; the caller is the actor of every ledger row. Making a course
  Protected needs a verified educator (``is_verified_educator``); once
  Protected, a course stays Protected here.
- ``course_management/members`` (course admin): each record of the course,
  managed, asked or declined, for the roster.
- ``course_management/answer`` (the member asked): accept or decline.
  Declining leaves them in the course, unmanaged.
- ``course_management/manage_members`` (Pangea team, a Synapse server
  admin): manage asked or declined members without asking, at a verified
  educator's request.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, List, Optional, Tuple

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
from synapse_pangea_chat.student_invitations.managed_events import CONTROL_CHANGED
from synapse_pangea_chat.student_invitations.protected import (
    CONTROLS,
    COURSE_MANAGEMENT,
    DEFAULTS,
    KNOCK,
    how_joined,
    management,
)
from synapse_pangea_chat.student_invitations.store import (
    ASKED,
    DECLINED,
    StudentInvitationStore,
)

MAX_MEMBERS = 500
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
    ) -> None:
        self._api = api
        self._main = api._hs.get_datastores().main
        self._store = store
        self._admins = admins
        self._accounts = accounts
        self._unmanaged = unmanaged
        self._enabled = enabled

    async def _room(self, args: Any) -> Optional[str]:
        return valid_room_id(args.get("room_id")) if isinstance(args, dict) else None

    async def set(self, caller: str, body: Any) -> Result:
        room_id = await self._room(body)
        if room_id is None:
            return _bad("'room_id' must be a valid Matrix room ID")
        if not await self._admins.is_course_admin(room_id, caller):
            return 403, FORBIDDEN
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
            await self._knock(room_id, caller, state.get(("m.room.join_rules", "")))
        # Members are asked before the flag is written, so a failure before
        # it leaves the course not Protected and a retry asks again; records
        # made after this were made in a Protected course.
        asked = await self._ask(room_id, caller, now_ms()) if turning_on else []
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
        return 200, {"management": wanted, "asked": asked}

    async def _knock(self, room_id: str, caller: str, join_rules: Any) -> None:
        content = unfreeze(join_rules.content) if join_rules is not None else {}
        if content.get("join_rule") == KNOCK:
            return
        await self._api.create_and_send_event_into_room(
            {
                "type": "m.room.join_rules",
                "room_id": room_id,
                "sender": caller,
                "state_key": "",
                "content": {**content, "join_rule": KNOCK},
            }
        )

    async def _ask(self, room_id: str, caller: str, at: int) -> List[str]:
        """Ask each member already in the course (``store.ask``)."""
        state = await self._api.get_room_state(room_id, [("m.room.member", None)])
        members: List[Tuple[str, str, Optional[int]]] = []
        for (_, user_id), member in state.items():
            if member.content.get("membership") != "join":
                continue
            if await self._unmanaged(room_id, user_id):
                continue
            via, joined_at_ms = await how_joined(self._main, member)
            members.append((user_id, via, joined_at_ms))
        return await self._store.ask(room_id, members, caller, at)

    async def members(self, caller: str, query: Any) -> Result:
        room_id = await self._room(query)
        if room_id is None:
            return _bad("'room_id' must be a valid Matrix room ID")
        if not await self._admins.is_course_admin(room_id, caller):
            return 403, FORBIDDEN
        return 200, {"members": await self._store.managed_in_room(room_id)}

    async def answer(self, caller: str, body: Any) -> Result:
        room_id = await self._room(body)
        if room_id is None or not isinstance(body.get("accept"), bool):
            return _bad("'room_id' and 'accept' are required")
        status = await self._store.answer(
            room_id, caller, accept=body["accept"], actor=caller, now_ms=now_ms()
        )
        if status is None:
            return 404, NOT_FOUND
        return 200, {"status": status}

    async def manage(self, caller: str, body: Any) -> Result:
        room_id = await self._room(body)
        user_ids = body.get("user_ids") if isinstance(body, dict) else None
        if (
            room_id is None
            or not isinstance(user_ids, list)
            or not 1 <= len(user_ids) <= MAX_MEMBERS
            or not all(isinstance(u, str) for u in user_ids)
        ):
            return _bad(f"'room_id' and 1 to {MAX_MEMBERS} 'user_ids' are required")
        if not await self._accounts.is_server_admin(caller):
            return 403, {"error": "Forbidden", "errcode": "M_FORBIDDEN"}
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
        return 200, {"managed": managed}
