"""Release on leave, kick or ban (SPEC §7, "Leaving").

When the claimant of a joined invitation leaves the course space, or is
kicked (a ``leave`` sent by someone else) or banned from it, the invitation
becomes ``left`` and the managed record is deleted, as the disclosure
promises. Rejoining with the class code restores nothing: a re-invite and a
fresh confirmation are needed. Runs as an ``on_new_event`` callback and never
raises into event persistence.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Tuple

from synapse_pangea_chat.student_invitations.report import report_failure
from synapse_pangea_chat.student_invitations.store import StudentInvitationStore

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.student_invitations.membership_callback"
)

RELEASING_MEMBERSHIPS = frozenset({"leave", "ban"})


class MembershipRelease:
    def __init__(self, store: StudentInvitationStore) -> None:
        self._store = store

    async def on_new_event(
        self, event: Any, _state: Mapping[Tuple[str, str], Any]
    ) -> None:
        if event.type != "m.room.member" or not isinstance(event.state_key, str):
            return
        if event.content.get("membership") not in RELEASING_MEMBERSHIPS:
            return
        try:
            released = await self._store.release_on_leave(
                event.room_id, event.state_key
            )
        except Exception as error:
            report_failure(
                "student invitation release",
                error,
                room=event.room_id,
                user=event.state_key,
            )
            return
        if released is not None:
            logger.info(
                "Student invitation %s left: %s is no longer in %s",
                released,
                event.state_key,
                event.room_id,
            )
