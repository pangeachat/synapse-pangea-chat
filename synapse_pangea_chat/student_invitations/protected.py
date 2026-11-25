"""Protected courses (pangeachat/.github#799; managed-accounts and
course-spaces docs): the course's ``pangea.course_management`` state event,
the rules that keep a Protected course private and ``knock``, and the
records and ledger rows a Protected course's memberships write.

The event holds the Protected flag and the course admin's controls: three
that reach outside the course, off until turned on, and the same three
between members of the course, on until turned off. Only the module writes
it (``protected_api.py``, under ``joined_via.module_write``). Enforcing the
controls is later work; this module records them.

A persisted join of a Protected course makes the member managed, recording
how they joined: the path the module named in the join or in the invitation
it follows (``joined_via.py``), else an approved request or another
invitation. Course admins, Synapse server admins and the paths that make an
admin are not managed; a claim records its own management in its
transaction. A managed student's leave, removal, ban and rejoin are ledger
rows.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, Tuple

from synapse_pangea_chat.joined_via import (
    JOINED_VIA,
    VIA_CLAIM,
    VIA_INSTRUCTOR,
    is_module_write,
)
from synapse_pangea_chat.student_invitations.claim import now_ms
from synapse_pangea_chat.student_invitations.events import SYSTEM
from synapse_pangea_chat.student_invitations.managed_events import (
    BANNED,
    LEFT,
    REJOINED,
    REMOVED,
    VIA_INVITE,
    VIA_REQUEST,
    VIA_UNKNOWN,
    VIAS,
)
from synapse_pangea_chat.student_invitations.report import report_failure
from synapse_pangea_chat.student_invitations.store import (
    MANAGED,
    StudentInvitationStore,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.student_invitations.protected"
)

COURSE_MANAGEMENT = "pangea.course_management"
CONTROLS = ("other_courses", "dms", "calls")
# Outside the course: off until turned on. Inside it: on until turned off.
DEFAULTS = {"outside": False, "in_course": True}
KNOCK = "knock"
StateMap = Mapping[Tuple[str, str], Any]
_ALLOW: Tuple[bool, Optional[dict]] = (True, None)
_REFUSE: Tuple[bool, Optional[dict]] = (False, None)
# How many profile changes back a member event is followed to its join.
_MAX_PRIOR = 20


def management(content: Any) -> Dict[str, Any]:
    """The event's content as the module reads it: each control a boolean,
    its default where missing or malformed."""
    content = content if isinstance(content, Mapping) else {}
    view: Dict[str, Any] = {"protected": content.get("protected") is True}
    for group, default in DEFAULTS.items():
        given = content.get(group)
        given = given if isinstance(given, Mapping) else {}
        view[group] = {
            name: given[name] if isinstance(given.get(name), bool) else default
            for name in CONTROLS
        }
    return view


def is_protected(state: StateMap) -> bool:
    event = state.get((COURSE_MANAGEMENT, ""))
    return event is not None and management(event.content)["protected"]


class ProtectedRules:
    """Refuses, before an event is stored: a ``pangea.course_management``
    event or a ``joined_via`` name the module did not write; the redaction of
    a ``pangea.course_management`` event; and in a Protected course, a join
    rule other than ``knock`` and listing it in the room directory."""

    def __init__(self, api: Any) -> None:
        self._main = api._hs.get_datastores().main
        api.register_third_party_rules_callbacks(
            check_event_allowed=self.check_event_allowed,
            check_visibility_can_be_modified=self.check_visibility_can_be_modified,
        )

    async def check_event_allowed(
        self, event: Any, state: StateMap
    ) -> Tuple[bool, Optional[dict]]:
        if is_module_write():
            return _ALLOW
        if event.type == COURSE_MANAGEMENT:
            return _REFUSE
        if event.type == "m.room.member" and JOINED_VIA in event.content:
            return _REFUSE
        if event.type == "m.room.join_rules" and event.get_state_key() == "":
            if is_protected(state) and event.content.get("join_rule") != KNOCK:
                return _REFUSE
        if event.type == "m.room.redaction" and event.redacts:
            target = await self._main.get_event(
                event.redacts, allow_none=True, allow_rejected=True
            )
            if target is not None and target.type == COURSE_MANAGEMENT:
                return _REFUSE
        return _ALLOW

    async def check_visibility_can_be_modified(
        self, room_id: str, state: StateMap, new_visibility: str
    ) -> bool:
        return new_visibility != "public" or not is_protected(state)


async def prior_member(main: Any, event: Any) -> Optional[Any]:
    """The member event this one replaced, with its own ``prev_content``."""
    prior_id = event.unsigned.get("replaces_state")
    if not isinstance(prior_id, str):
        return None
    return await main.get_event(prior_id, allow_none=True, get_prev_content=True)


async def how_joined(main: Any, member: Any) -> Tuple[str, int]:
    """How, and when, the member of this join event joined: the module path
    named in the join or in the invitation it follows, else an approved
    request (an invitation after a knock) or another invitation. A profile
    change is followed back to the join itself."""
    join = member
    for _ in range(_MAX_PRIOR):
        via = join.content.get(JOINED_VIA)
        if via in VIAS:
            return str(via), join.origin_server_ts
        prior = await prior_member(main, join)
        membership = prior.content.get("membership") if prior is not None else None
        if membership != "join":
            break
        join = prior
    if prior is None or membership != "invite":
        return VIA_UNKNOWN, join.origin_server_ts
    via = prior.content.get(JOINED_VIA)
    if via not in VIAS:
        before = prior.unsigned.get("prev_content") or {}
        via = VIA_REQUEST if before.get("membership") == KNOCK else VIA_INVITE
    return str(via), join.origin_server_ts


class ManagedMembership:
    """The ``on_new_event`` callback for member events of a Protected course.
    Never raises into event persistence: a failure is reported."""

    def __init__(self, api: Any, store: StudentInvitationStore, unmanaged: Any) -> None:
        self._main = api._hs.get_datastores().main
        self._store = store
        # (room_id, user_id) -> whether this member is never managed here.
        self._unmanaged = unmanaged

    async def on_new_event(self, event: Any, state: StateMap) -> None:
        if event.type != "m.room.member" or not isinstance(event.state_key, str):
            return
        if not is_protected(state):
            return
        try:
            await self._on_member(event)
        except Exception as error:
            report_failure(
                "protected course membership record", error, room=event.room_id
            )

    async def _on_member(self, event: Any) -> None:
        room_id, user_id = event.room_id, event.state_key
        membership = event.content.get("membership")
        prior = await prior_member(self._main, event)
        before = prior.content.get("membership") if prior is not None else None
        if membership == "join" and before != "join":
            record = await self._store.managed_record(user_id, room_id)
            # Managed before this join: a rejoin, however they came back. A
            # claim's own record is written after its join.
            if (
                record is not None
                and record["status"] == MANAGED
                and record["created_at_ms"] < event.origin_server_ts
            ):
                await self._store.record_managed(
                    room_id, REJOINED, user_id, now_ms(), user_id=user_id
                )
                return
            via, _ = await how_joined(self._main, event)
            if via in (VIA_CLAIM, VIA_INSTRUCTOR):
                return
            if await self._unmanaged(room_id, user_id):
                return
            if await self._store.start_managed(
                room_id,
                user_id,
                actor=SYSTEM,
                via=via,
                joined_at_ms=event.origin_server_ts,
                now_ms=now_ms(),
            ):
                logger.info("Managed record started in %s by a join", room_id)
            return
        if membership == "ban" and before != "ban":
            action = BANNED
        elif membership == "leave" and before == "join":
            action = LEFT if event.sender == user_id else REMOVED
        else:
            return
        await self._store.record_managed(
            room_id, action, event.sender, now_ms(), user_id=user_id
        )
