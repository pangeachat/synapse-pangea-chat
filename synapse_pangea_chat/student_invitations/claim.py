"""The claim (CONTRACTS C2.4), used by every claim path.

Paths: the student opening their invitation, the teacher's Grant and Approve
all, the ``ClaimByEmail`` hook on every sign-in and verified-email addition,
and the Canvas link step and later launches (a row imported with exactly the
account's own linked Canvas identity). No student confirmation is needed:
managed-account consent lives in Pangea's Terms (seats amendment 2026-10-10).

Order: (1) force-join the account to the course with the shared
``assign_room_membership`` join (a no-op when already joined; joining is
harmless if step 2 then refuses, since a class-code join is allowed
unmanaged); (2) one transaction that claims the row only if it is ``invited``
and the account's verified email matches, its request is granted, or the
Canvas identity matches, and the account holds no other joined invitation in
the course (``StudentInvitationStore.claim_txn``). The claim records a managed account,
except when the claimant is a course admin (power level 100) of that course:
a teacher is never managed by a course they administer. Two concurrent claims of one row: one
wins, the other sees it no longer ``invited``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Protocol, Tuple

from synapse_pangea_chat.student_invitations.accounts import Accounts
from synapse_pangea_chat.student_invitations.events import SYSTEM
from synapse_pangea_chat.student_invitations.report import report_failure
from synapse_pangea_chat.student_invitations.store import (
    CLAIMED,
    DECISION_GRANTED,
    NOT_ELIGIBLE,
    NOT_LIVE,
    STATE_INVITED,
    STATE_JOINED,
    CanvasIdentity,
    StudentInvitationStore,
    canvas_match,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.student_invitations.claim"
)

MEMBERSHIP_JOIN = "join"


class Joiner(Protocol):
    async def force_join(self, room_id: str, user_id: str) -> Dict[str, Any]:
        ...


class CourseAdmins(Protocol):
    async def is_course_admin(self, room_id: str, user_id: str) -> bool:
        ...


class ClaimJoinFailed(Exception):
    """The account could not be joined to the course (banned, no inviter)."""


def now_ms() -> int:
    return int(time.time() * 1000)


class StudentClaims:
    def __init__(
        self,
        store: StudentInvitationStore,
        accounts: Accounts,
        joiner: Joiner,
        admins: CourseAdmins,
    ) -> None:
        self._store = store
        self._accounts = accounts
        self._joiner = joiner
        self._admins = admins

    async def claim(
        self,
        invitation_id: str,
        user_id: str,
        *,
        grant: bool = False,
        canvas_identity: Optional[CanvasIdentity] = None,
        actor: str = SYSTEM,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Claim ``invitation_id`` for ``user_id``. ``grant`` is the teacher
        ``actor``'s Grant of the account's request, recorded only if the claim
        it allows happens.
        ``canvas_identity`` is the (issuer, Canvas course, Canvas user id) of
        the account's own Canvas link, from a validated launch.

        Returns (outcome, row): ``claimed`` (also when already joined by this
        account), ``not_live``, ``not_eligible`` (no email match, no granted
        request and no Canvas match) or ``already_claimed_in_course``. Raises
        ``ClaimJoinFailed`` if the account cannot be joined to the course.
        """
        row = await self._store.get(invitation_id)
        if row is None:
            return NOT_LIVE, None
        if row["state"] == STATE_JOINED and row["claimant"] == user_id:
            return CLAIMED, row
        if row["state"] != STATE_INVITED:
            return NOT_LIVE, row
        email_match = row["email_key"] in await self._accounts.verified_email_keys(
            user_id
        )
        request = await self._store.get_request(invitation_id, user_id)
        granted = request is not None and (
            grant or request["decision"] == DECISION_GRANTED
        )
        if not (email_match or granted or canvas_match(row, canvas_identity)):
            return NOT_ELIGIBLE, row

        room_id = row["course_room_id"]
        joined = await self._joiner.force_join(room_id, user_id)
        if not joined.get("success"):
            raise ClaimJoinFailed(joined.get("action", "failed"))

        # A course admin claiming an invitation in their own course is
        # joined but never managed by it (owner amendment 2026-10-09).
        is_admin = await self._admins.is_course_admin(room_id, user_id)
        outcome, claimed = await self._store.claim_txn(
            invitation_id,
            user_id,
            email_match=email_match,
            grant=grant,
            now_ms=now_ms(),
            managed=not is_admin,
            canvas_identity=canvas_identity,
            actor=actor,
        )
        if outcome == CLAIMED and claimed is not None:
            logger.info("Claimed student invitation %s", invitation_id)
            await self._release_if_gone(room_id, user_id)
            await self._recheck_managed(claimed)
        return outcome, claimed if claimed is not None else row

    async def _recheck_managed(self, row: Dict[str, Any]) -> None:
        # The admin check ran before the claim transaction, and a power-level
        # event in between found no joined row to fix. Re-reading now that the
        # row is joined closes that window; a later event finds the row. A
        # failure leaves the claim standing and the next sign-in repairs it.
        try:
            await self.apply_managed_rule(row)
        except Exception as error:
            report_failure(
                "managed record check after claim",
                error,
                invitation=row["id"],
            )

    async def _release_if_gone(self, room_id: str, user_id: str) -> None:
        # A leave between the join and the claim transaction would otherwise
        # leave a managed record for someone no longer in the course: the
        # membership callback ran before the row was joined.
        if await self._accounts.membership(room_id, user_id) != MEMBERSHIP_JOIN:
            released = await self._store.release_on_leave(room_id, user_id, now_ms())
            if released is not None:
                logger.info(
                    "Released student invitation %s: its claimant left during the claim",
                    released,
                )

    async def apply_managed_rule(self, row: Dict[str, Any]) -> None:
        """C2.5 for one joined invitation: a managed record exactly while its
        claimant is not a course admin (power level 100, creators count) of
        the course. Raises on failure; callers report it."""
        claimant = row["claimant"]
        room_id = row["course_room_id"]
        is_admin = await self._admins.is_course_admin(room_id, claimant)
        result = await self._store.set_managed(
            row["id"], claimant, not is_admin, now_ms()
        )
        if result in ("inserted", "deleted"):
            logger.info(
                "Managed record %s in %s (invitation %s)",
                result,
                room_id,
                row["id"],
            )

    async def apply_managed_rule_in_room(self, room_id: str) -> None:
        """After a power-level change in a course. Never raises."""
        try:
            rows = [
                r
                for r in await self._store.list_room(room_id)
                if r["state"] == STATE_JOINED
            ]
        except Exception as error:
            report_failure("managed record power-level lookup", error, room=room_id)
            return
        for row in rows:
            try:
                await self.apply_managed_rule(row)
            except Exception as error:
                report_failure(
                    "managed record power-level update",
                    error,
                    invitation=row["id"],
                )

    async def repair_managed_for(self, user_id: str) -> None:
        """At sign-in: re-apply C2.5 to the account's joined invitations, so a
        missed power-level event is repaired. Never raises."""
        try:
            rows = await self._store.joined_by(user_id)
        except Exception as error:
            report_failure("managed record repair lookup", error)
            return
        for row in rows:
            try:
                await self.apply_managed_rule(row)
            except Exception as error:
                report_failure("managed record repair", error, invitation=row["id"])

    async def claim_matching_for(self, user_id: str) -> None:
        """The ``ClaimByEmail`` path, at sign-in and when an address is newly
        verified: claim every live ``invited`` row whose address this account
        has verified. Oldest first, so of several rows in one course the
        oldest is claimed and the rest stay ``invited`` (one claim per course;
        the roster marks them "same student as"). Never raises: a failed claim
        must not fail the sign-in."""
        try:
            keys = await self._accounts.verified_email_keys(user_id)
            rows = await self._store.invited_for_keys(keys)
        except Exception as error:
            report_failure("student invitation lookup at sign-in", error)
            return
        for row in rows:
            try:
                outcome, _ = await self.claim(row["id"], user_id)
            except Exception as error:
                report_failure(
                    "student invitation claim at sign-in",
                    error,
                    invitation=row["id"],
                )
                continue
            if outcome != CLAIMED:
                logger.info(
                    "Student invitation %s not claimed at sign-in: %s",
                    row["id"],
                    outcome,
                )
