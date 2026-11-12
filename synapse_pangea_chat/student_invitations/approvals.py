"""Pending approvals: requests a teacher grants or denies (SPEC §7, seats
amendment 2026-10-10).

A request is written when an account opens an invitation whose address it
does not have verified. It is pending while its invitation is ``invited``, it
has no decision, and the account still has no verified email matching the
invitation. Grant claims (the claim records the decision only together with
the claim); Deny records ``denied``. A denied account that later verifies the
invited email is still claimed by the email match.

"Same student as" is read here, not stored: an ``invited`` row is marked with
an account that already holds a joined invitation in the course and either
has the row's address verified or has a request on it.

``signed_up_as_email`` (the requesting account's first bound email) is shown
only to the course's admins, and never logged.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from synapse_pangea_chat.student_invitations.accounts import Accounts
from synapse_pangea_chat.student_invitations.claim import StudentClaims
from synapse_pangea_chat.student_invitations.store import (
    ALREADY_CLAIMED_IN_COURSE,
    CLAIMED,
    NOT_LIVE,
    StudentInvitationStore,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.student_invitations.approvals"
)


class RoomApprovals:
    """One course's requests, read once per request."""

    def __init__(
        self,
        pending: Dict[str, List[Dict[str, Any]]],
        same_student_as: Dict[str, str],
    ) -> None:
        #: invitation id -> its pending requests, oldest first
        self.pending = pending
        #: invitation id -> an account that already holds another joined
        #: invitation in this course and has this row's address verified or
        #: a request on it
        self.same_student_as = same_student_as


class Approvals:
    def __init__(
        self,
        store: StudentInvitationStore,
        claims: StudentClaims,
        accounts: Accounts,
    ) -> None:
        self._store = store
        self._claims = claims
        self._accounts = accounts

    async def for_room(self, room_id: str) -> RoomApprovals:
        rows = {r["id"]: r for r in await self._store.list_room(room_id)}
        requests = await self._store.requests_in_room(room_id)
        claimants = await self._store.joined_claimants(room_id)
        keys: Dict[str, set] = {}
        pending: Dict[str, List[Dict[str, Any]]] = {}
        same: Dict[str, str] = {}
        for invited in rows.values():
            if invited["state"] != "invited" or not claimants:
                continue
            # The account that has this row's address verified (Synapse binds
            # an address to one account), if it holds another joined row here.
            holder = await self._accounts.user_for_email_key(invited["email_key"])
            if holder is not None and claimants.get(holder) not in (
                None,
                invited["id"],
            ):
                same[invited["id"]] = holder
        for request in requests:
            row = rows.get(request["invitation_id"])
            if row is None or row["state"] != "invited":
                continue
            user_id = request["user_id"]
            holds_other = claimants.get(user_id) not in (None, row["id"])
            if holds_other and row["id"] not in same:
                same[row["id"]] = user_id
            if request["decision"] is not None:
                continue
            if user_id not in keys:
                keys[user_id] = await self._accounts.verified_email_keys(user_id)
            if row["email_key"] in keys[user_id]:
                continue
            pending.setdefault(row["id"], []).append(
                {**request, "same_student": holds_other}
            )
        return RoomApprovals(pending, same)

    async def pending_rows(self, room_id: str) -> List[Dict[str, Any]]:
        """The T5 rows: each pending request with who made it. (The T5 field
        ``acked_at_ms`` keeps its contract name: it is the request time.)"""
        view = await self.for_room(room_id)
        result: List[Dict[str, Any]] = []
        for invitation_id, requests in view.pending.items():
            for request in requests:
                user_id = request["user_id"]
                result.append(
                    {
                        "invitation_id": invitation_id,
                        "user_id": user_id,
                        "display_name": await self._accounts.display_name(user_id),
                        "signed_up_as_email": await self._accounts.first_email(user_id),
                        "acked_at_ms": request["requested_at_ms"],
                        "same_student": request["same_student"],
                    }
                )
        result.sort(key=lambda r: (r["acked_at_ms"], r["invitation_id"], r["user_id"]))
        return result

    async def grant(
        self, invitation_id: str, user_id: str, actor: str
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Returns (outcome, row) as ``StudentClaims.claim`` does."""
        return await self._claims.claim(invitation_id, user_id, grant=True, actor=actor)

    async def approve_all(self, room_id: str, actor: str) -> Dict[str, Any]:
        view = await self.for_room(room_id)
        granted: List[str] = []
        skipped: List[str] = []
        refused: List[Dict[str, str]] = []
        for invitation_id in sorted(view.pending):
            requests = view.pending[invitation_id]
            if len(requests) != 1:
                skipped.append(invitation_id)
                continue
            outcome, _ = await self.grant(invitation_id, requests[0]["user_id"], actor)
            if outcome == CLAIMED:
                granted.append(invitation_id)
            elif outcome == ALREADY_CLAIMED_IN_COURSE:
                refused.append(
                    {
                        "invitation_id": invitation_id,
                        "reason": "already_claimed_in_course",
                    }
                )
            elif outcome == NOT_LIVE:
                refused.append({"invitation_id": invitation_id, "reason": "not_live"})
            else:
                # The request vanished since the read (a re-invite reset the
                # row): nothing is pending any more, so nothing to report.
                logger.info(
                    "approve_all: %s no longer has a pending request",
                    invitation_id,
                )
        return {"granted": granted, "skipped_multiple": skipped, "refused": refused}
