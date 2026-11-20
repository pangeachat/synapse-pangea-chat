"""Release on leave, kick or ban (SPEC §7, "Leaving"), and claim on join.

A join to a course space runs the sign-in claim for that course only
(``StudentClaims.claim_matching_in_room``): a student who joins by the class
code is claimed by a verified address, as if they had opened their link.

When the claimant of a joined invitation leaves the course space, or is
kicked (a ``leave`` sent by someone else) or banned from it, the invitation
becomes ``left`` and the managed record is deleted. Rejoining with the class
code restores nothing: a re-invite is needed.

The same callback watches the course's ``m.room.power_levels``: the managed
record exists exactly while a joined claimant is not a course admin there
(CONTRACTS C2.5). Runs as an ``on_new_event`` callback and never raises into
event persistence.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Tuple

from synapse_pangea_chat.student_invitations.claim import StudentClaims, now_ms
from synapse_pangea_chat.student_invitations.report import report_failure
from synapse_pangea_chat.student_invitations.store import StudentInvitationStore

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.student_invitations.membership_callback"
)

RELEASING_MEMBERSHIPS = frozenset({"leave", "ban"})
JOIN = "join"


class MembershipRelease:
    def __init__(self, store: StudentInvitationStore, claims: StudentClaims) -> None:
        self._store = store
        self._claims = claims

    async def on_new_event(
        self, event: Any, _state: Mapping[Tuple[str, str], Any]
    ) -> None:
        if event.type == "m.room.power_levels" and event.state_key == "":
            # A claimant who stops being a course admin becomes managed; one
            # who becomes a course admin stops being managed (C2.5).
            await self._claims.apply_managed_rule_in_room(event.room_id)
            return
        if event.type != "m.room.member" or not isinstance(event.state_key, str):
            return
        membership = event.content.get("membership")
        if membership == JOIN:
            # Never raises; only ``invited`` rows of this room are read.
            await self._claims.claim_matching_in_room(event.state_key, event.room_id)
            return
        if membership not in RELEASING_MEMBERSHIPS:
            return
        try:
            released = await self._store.release_on_leave(
                event.room_id, event.state_key, now_ms()
            )
        except Exception as error:
            report_failure(
                "student invitation release",
                error,
                room=event.room_id,
            )
            return
        if released is not None:
            logger.info(
                "Student invitation %s left: its claimant is no longer in %s",
                released,
                event.room_id,
            )
