"""Student invitations: the claim rules, pending approvals and the routes'
handlers (CONTRACTS C2), over the real store on an in-memory database.

Synapse's surfaces the handlers read (threepids, profiles, room state, the
force-join) are doubles; the HTTP layer, real power levels, the membership
callback on real events and the sent email are covered end to end in
``test_student_invitations_e2e.py``.
"""

from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest.mock import MagicMock, patch

from synapse_pangea_chat.config import (
    MANAGED_DISCLOSURE_TEXT,
    MANAGED_DISCLOSURE_VERSION,
    PangeaChatConfig,
)
from synapse_pangea_chat.email_invite.build_join_url import build_join_url
from synapse_pangea_chat.student_invitations import report
from synapse_pangea_chat.student_invitations.accounts import Accounts
from synapse_pangea_chat.student_invitations.api import (
    StudentInvitationHandlers,
    guarded,
)
from synapse_pangea_chat.student_invitations.approvals import Approvals
from synapse_pangea_chat.student_invitations.claim import StudentClaims
from synapse_pangea_chat.student_invitations.hint_lookup import mask_email
from synapse_pangea_chat.student_invitations.invite_email import InviteMailer
from synapse_pangea_chat.student_invitations.membership_callback import (
    MembershipRelease,
)
from synapse_pangea_chat.student_invitations.store import StudentInvitationStore
from tests.moderation_doubles import DbPoolDouble

ROOM = "!course:x"
OTHER_ROOM = "!other:x"
TEACHER = "@teacher:x"
STUDENT = "@student:x"
OTHER = "@other:x"
THIRD = "@third:x"
INVITED = "Student@School.example"
INVITED_KEY = "student@school.example"
V = MANAGED_DISCLOSURE_VERSION
ADDRESSES = (
    "student@school.example",
    "other@gmail.example",
    "second@school.example",
    "third@school.example",
    "member@school.example",
)


class RollbackPool(DbPoolDouble):
    """The shared double, plus the rollback a real transaction does when its
    function raises."""

    async def runInteraction(self, desc: str, func: Any, *args: Any, **kw: Any):
        try:
            return await super().runInteraction(desc, func, *args, **kw)
        except BaseException:
            self.connection.rollback()
            raise


class FakeMain:
    def __init__(self) -> None:
        self.db_pool = RollbackPool()
        self.threepids: Dict[str, List[Tuple[str, int]]] = {}
        self.displaynames: Dict[str, str] = {}
        self.membership: Dict[Tuple[str, str], str] = {}

    async def user_get_threepids(self, user_id: str) -> List[Any]:
        return [
            SimpleNamespace(medium="email", address=a, added_at=t, validated_at=t)
            for a, t in self.threepids.get(user_id, [])
        ]

    async def get_profile_displayname(self, user: Any) -> Optional[str]:
        return self.displaynames.get(user.to_string())

    async def get_local_current_membership_for_user_in_room(
        self, user_id: str, room_id: str
    ) -> Tuple[Optional[str], Optional[str]]:
        return self.membership.get((room_id, user_id)), None


class FakeJoiner:
    def __init__(self, main: FakeMain) -> None:
        self.main = main
        self.joins: List[Tuple[str, str]] = []
        self.refuse: Set[str] = set()

    async def force_join(self, room_id: str, user_id: str) -> Dict[str, Any]:
        self.joins.append((room_id, user_id))
        if user_id in self.refuse:
            return {"user_id": user_id, "success": False, "action": "failed"}
        self.main.membership[(room_id, user_id)] = "join"
        return {"user_id": user_id, "success": True, "action": "joined"}


class FakeAdmins:
    def __init__(self) -> None:
        self.admins: Set[Tuple[str, str]] = {(ROOM, TEACHER), (OTHER_ROOM, TEACHER)}
        self.checks: List[Tuple[str, str]] = []

    async def is_course_admin(self, room_id: str, user_id: str) -> bool:
        self.checks.append((room_id, user_id))
        return (room_id, user_id) in self.admins


class FakeRooms:
    def __init__(self, main: FakeMain) -> None:
        self.main = main
        self.names = {ROOM: "Spanish 101", OTHER_ROOM: "French 201"}
        self.codes: Dict[str, Optional[str]] = {ROOM: "cl4sscd", OTHER_ROOM: "fr3nchc"}

    async def course_name(self, room_id: str) -> Optional[str]:
        return self.names.get(room_id)

    async def course_topic(self, room_id: str) -> Optional[str]:
        return "Learn Spanish" if room_id == ROOM else None

    async def access_code(self, room_id: str) -> Optional[str]:
        return self.codes.get(room_id)

    async def membership(self, room_id: str, user_id: str) -> Optional[str]:
        return self.main.membership.get((room_id, user_id))


class FakeMailer:
    def __init__(self) -> None:
        self.sent: List[Tuple[str, str]] = []
        self.fail_with: Optional[Exception] = None

    async def send_invite(self, row: Dict[str, Any], access_code: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((row["id"], access_code))


class Harness:
    def __init__(self) -> None:
        self.main = FakeMain()
        self.api = MagicMock()
        self.api._hs.get_datastores.return_value.main = self.main
        self.store = StudentInvitationStore(self.api._hs)
        self.accounts = Accounts(self.api)
        self.joiner = FakeJoiner(self.main)
        self.admins = FakeAdmins()
        self.claims = StudentClaims(self.store, self.accounts, self.joiner, self.admins)
        self.approvals = Approvals(self.store, self.claims, self.accounts)
        self.rooms = FakeRooms(self.main)
        self.mailer = FakeMailer()
        self.handlers = StudentInvitationHandlers(
            store=self.store,
            claims=self.claims,
            approvals=self.approvals,
            accounts=self.accounts,
            admins=self.admins,
            rooms=self.rooms,
            mailer=self.mailer,
        )
        self.main.membership[(ROOM, TEACHER)] = "join"

    def verify(self, user_id: str, address: str, at: int = 1) -> None:
        self.main.threepids.setdefault(user_id, []).append((address, at))

    async def add(self, *emails: str, room: str = ROOM) -> List[Dict[str, Any]]:
        status, body = await self.handlers.add(
            TEACHER, {"room_id": room, "emails": list(emails), "source": "manual"}
        )
        assert status == 200, body
        return body["invitations"]

    async def confirm(
        self, user: str, invitation_id: str, version: int = V
    ) -> Tuple[int, Dict[str, Any]]:
        return await self.handlers.confirm(
            user, {"invitation_id": invitation_id, "disclosure_version": version}
        )

    async def row(self, invitation_id: str) -> Dict[str, Any]:
        row = await self.store.get(invitation_id)
        assert row is not None
        return row

    async def pending(self, room: str = ROOM) -> List[Dict[str, Any]]:
        status, body = await self.handlers.pending_approvals(TEACHER, {"room_id": room})
        assert status == 200, body
        return body["pending"]

    async def listing(self, room: str = ROOM) -> Dict[str, Dict[str, Any]]:
        status, body = await self.handlers.list(TEACHER, {"room_id": room})
        assert status == 200, body
        return {i["invitation_id"]: i for i in body["invitations"]}


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.h = Harness()

    def capture_logs(self) -> "_Captured":
        handler = _capture_logs()
        self.addCleanup(_release_logs, handler)
        return handler


class TestClaimRules(_Base):
    async def test_claim_requires_confirmation_plus_email_match_or_grant(self):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        # Verified email match, but no confirmation: nothing is claimed.
        self.h.verify(STUDENT, INVITED_KEY)
        outcome, _ = await self.h.claims.claim(ident, STUDENT)
        self.assertEqual(outcome, "not_eligible")
        self.assertEqual((await self.h.row(ident))["state"], "invited")
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))
        self.assertEqual(self.h.joiner.joins, [])
        # Confirmation, no match and no grant: a pending approval, no claim.
        status, body = await self.h.confirm(OTHER, ident)
        self.assertEqual((status, body["result"]), (200, "pending_approval"))
        self.assertEqual((await self.h.row(ident))["state"], "invited")
        self.assertNotIn((ROOM, OTHER), self.h.joiner.joins)
        # Confirmation plus match claims.
        status, body = await self.h.confirm(STUDENT, ident)
        self.assertEqual(
            (status, body),
            (200, {"result": "claimed", "invitation_id": ident, "room_id": ROOM}),
        )
        row = await self.h.row(ident)
        self.assertEqual((row["state"], row["claimant"]), ("joined", STUDENT))
        self.assertIn((ROOM, STUDENT), self.h.joiner.joins)

    async def test_claim_transaction_rechecks_every_rule_itself(self):
        """The database step refuses on its own, whatever the caller checked."""
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        # No confirmation: refused even with a match and a grant.
        outcome, _ = await self.h.store.claim_txn(
            ident, STUDENT, email_match=True, grant=True, now_ms=1
        )
        self.assertEqual(outcome, "not_eligible")
        # Confirmation, but no match and no grant: refused.
        await self.h.store.record_ack(ident, STUDENT, V, 2)
        outcome, _ = await self.h.store.claim_txn(
            ident, STUDENT, email_match=False, grant=False, now_ms=3
        )
        self.assertEqual(outcome, "not_eligible")
        row = await self.h.row(ident)
        self.assertEqual((row["state"], row["claimant"]), ("invited", None))
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))
        self.assertIsNone((await self.h.store.get_ack(ident, STUDENT))["decision"])
        # A grant claims, and records the decision with the claim.
        outcome, row = await self.h.store.claim_txn(
            ident, STUDENT, email_match=False, grant=True, now_ms=4
        )
        self.assertEqual(outcome, "claimed")
        self.assertEqual(
            (row["state"], row["claimant"], row["joined_at_ms"]), ("joined", STUDENT, 4)
        )
        self.assertEqual(
            (await self.h.store.get_ack(ident, STUDENT))["decision"], "granted"
        )
        self.assertEqual(
            (await self.h.store.managed_record(STUDENT, ROOM))["since_ms"], 4
        )

    async def test_managed_record_alone_blocks_a_second_claim_in_the_course(self):
        first, second = await self.h.add(INVITED, "second@school.example")
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.confirm(STUDENT, first["invitation_id"])
        # Even if the joined row were missed, the managed record refuses.
        await self.h.main.db_pool.runInteraction(
            "simulate",
            lambda txn: txn.execute(
                "UPDATE pangea_student_invitation SET claimant = NULL WHERE id = ?",
                (first["invitation_id"],),
            ),
        )
        await self.h.store.record_ack(second["invitation_id"], STUDENT, V, 9)
        outcome, _ = await self.h.store.claim_txn(
            second["invitation_id"], STUDENT, email_match=True, grant=False, now_ms=9
        )
        self.assertEqual(outcome, "already_claimed_in_course")
        self.assertEqual(
            (await self.h.row(second["invitation_id"]))["state"], "invited"
        )

    async def test_course_admin_claiming_own_course_invitation_is_joined_but_not_managed(
        self,
    ):
        (own, elsewhere) = await self.h.add(INVITED, "second@school.example")
        (other_course,) = await self.h.add(INVITED, room=OTHER_ROOM)
        self.h.admins.admins.add((ROOM, STUDENT))
        self.h.verify(STUDENT, INVITED_KEY)
        self.h.verify(STUDENT, "second@school.example", 2)
        status, body = await self.h.confirm(STUDENT, own["invitation_id"])
        self.assertEqual((status, body["result"]), (200, "claimed"))
        row = await self.h.row(own["invitation_id"])
        self.assertEqual((row["state"], row["claimant"]), ("joined", STUDENT))
        self.assertIn((ROOM, STUDENT), self.h.joiner.joins)
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))
        _, joined = await self.h.handlers.mine_joined(STUDENT)
        self.assertEqual(
            [i["invitation_id"] for i in joined["invitations"]], [own["invitation_id"]]
        )
        # Every other rule still holds: one claimed invitation per course.
        status, body = await self.h.confirm(STUDENT, elsewhere["invitation_id"])
        self.assertEqual(
            (status, body["errcode"]), (409, "ORG.PANGEA.ALREADY_CLAIMED_IN_COURSE")
        )
        # Admin of this course only: in another course the claim is managed.
        status, body = await self.h.confirm(STUDENT, other_course["invitation_id"])
        self.assertEqual(body["result"], "claimed")
        self.assertIsNotNone(await self.h.store.managed_record(STUDENT, OTHER_ROOM))

    async def test_non_admin_claim_still_records_managed(self):
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        status, body = await self.h.confirm(STUDENT, inv["invitation_id"])
        self.assertEqual(body["result"], "claimed")
        record = await self.h.store.managed_record(STUDENT, ROOM)
        self.assertEqual(
            (record["user_id"], record["course_room_id"], record["invited_by"]),
            (STUDENT, ROOM, TEACHER),
        )
        self.assertIn((ROOM, STUDENT), self.h.admins.checks)

    async def test_grant_without_confirmation_claims_nothing(self):
        (inv,) = await self.h.add(INVITED)
        status, body = await self.h.handlers.decide(
            TEACHER,
            {
                "room_id": ROOM,
                "invitation_id": inv["invitation_id"],
                "user_id": OTHER,
                "decision": "grant",
            },
        )
        self.assertEqual(status, 404, body)
        self.assertEqual((await self.h.row(inv["invitation_id"]))["state"], "invited")

    async def test_unverified_or_other_address_never_matches(self):
        (inv,) = await self.h.add(INVITED)
        self.h.verify(OTHER, "student@school.example.evil")
        status, body = await self.h.confirm(OTHER, inv["invitation_id"])
        self.assertEqual(body["result"], "pending_approval")
        self.assertEqual((await self.h.row(inv["invitation_id"]))["state"], "invited")

    async def test_confirmation_from_another_account_never_blocks_the_rightful_email_match(
        self,
    ):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        status, body = await self.h.confirm(OTHER, ident)
        self.assertEqual(body["result"], "pending_approval")
        self.h.verify(STUDENT, INVITED_KEY)
        status, body = await self.h.confirm(STUDENT, ident)
        self.assertEqual((status, body["result"]), (200, "claimed"))
        self.assertEqual((await self.h.row(ident))["claimant"], STUDENT)
        # The forwarded-link account is no longer a pending approval.
        self.assertEqual(await self.h.pending(), [])

    async def test_invitation_claimed_by_at_most_one_account(self):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        await self.h.confirm(OTHER, ident)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.confirm(STUDENT, ident)
        # A later grant for the other confirmation cannot move the claim.
        status, body = await self.h.handlers.decide(
            TEACHER,
            {
                "room_id": ROOM,
                "invitation_id": ident,
                "user_id": OTHER,
                "decision": "grant",
            },
        )
        self.assertEqual(
            (status, body["errcode"]), (409, "ORG.PANGEA.INVITATION_NOT_LIVE")
        )
        # And at the transaction level, a second account's claim is refused.
        self.h.verify(THIRD, INVITED_KEY)
        await self.h.store.record_ack(ident, THIRD, V, 5)
        outcome, _ = await self.h.store.claim_txn(
            ident, THIRD, email_match=True, grant=False, now_ms=6
        )
        self.assertEqual(outcome, "not_live")
        row = await self.h.row(ident)
        self.assertEqual((row["state"], row["claimant"]), ("joined", STUDENT))
        self.assertIsNone(await self.h.store.managed_record(OTHER, ROOM))
        self.assertIsNone(await self.h.store.managed_record(THIRD, ROOM))
        # A third account confirming sees the one 404 body.
        status, body = await self.h.confirm(THIRD, ident)
        self.assertEqual((status, body["errcode"]), (404, "M_NOT_FOUND"))

    async def test_second_invitation_in_the_same_course_refused_for_an_account_that_already_claimed_one(
        self,
    ):
        first, second = await self.h.add(INVITED, "second@school.example")
        self.h.verify(STUDENT, INVITED_KEY)
        self.h.verify(STUDENT, "second@school.example", 2)
        status, body = await self.h.confirm(STUDENT, first["invitation_id"])
        self.assertEqual(body["result"], "claimed")
        status, body = await self.h.confirm(STUDENT, second["invitation_id"])
        self.assertEqual(
            (status, body["errcode"]), (409, "ORG.PANGEA.ALREADY_CLAIMED_IN_COURSE")
        )
        # The confirmation is recorded; the roster marks the duplicate.
        self.assertIsNotNone(
            await self.h.store.get_ack(second["invitation_id"], STUDENT)
        )
        listed = await self.h.listing()
        self.assertEqual(listed[second["invitation_id"]]["state"], "invited")
        self.assertEqual(listed[second["invitation_id"]]["same_student_as"], STUDENT)
        self.assertIsNone(listed[first["invitation_id"]]["same_student_as"])
        # A grant is refused the same way and records no decision.
        status, body = await self.h.handlers.decide(
            TEACHER,
            {
                "room_id": ROOM,
                "invitation_id": second["invitation_id"],
                "user_id": STUDENT,
                "decision": "grant",
            },
        )
        self.assertEqual(
            (status, body["errcode"]), (409, "ORG.PANGEA.ALREADY_CLAIMED_IN_COURSE")
        )
        ack = await self.h.store.get_ack(second["invitation_id"], STUDENT)
        self.assertIsNone(ack["decision"])
        # The database refuses it even past the handler's own check.
        await self.h.store.record_ack(second["invitation_id"], STUDENT, V, 9)
        outcome, _ = await self.h.store.claim_txn(
            second["invitation_id"], STUDENT, email_match=True, grant=False, now_ms=9
        )
        self.assertEqual(outcome, "already_claimed_in_course")

    async def test_one_joined_invitation_per_course_holds_in_the_database_itself(self):
        first, second = await self.h.add(INVITED, "second@school.example")
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.confirm(STUDENT, first["invitation_id"])
        with self.assertRaises(Exception) as raised:
            await self.h.main.db_pool.runInteraction(
                "bypass",
                lambda txn: txn.execute(
                    "UPDATE pangea_student_invitation SET state = 'joined',"
                    " claimant = ? WHERE id = ?",
                    (STUDENT, second["invitation_id"]),
                ),
            )
        self.assertIn("IntegrityError", type(raised.exception).__name__)

    async def test_claims_in_two_courses_are_independent(self):
        (a,) = await self.h.add(INVITED)
        (b,) = await self.h.add(INVITED, room=OTHER_ROOM)
        self.h.verify(STUDENT, INVITED_KEY)
        for inv in (a, b):
            status, body = await self.h.confirm(STUDENT, inv["invitation_id"])
            self.assertEqual(body["result"], "claimed")
        self.assertIsNotNone(await self.h.store.managed_record(STUDENT, ROOM))
        self.assertIsNotNone(await self.h.store.managed_record(STUDENT, OTHER_ROOM))

    async def test_every_claim_writes_managed_record_revoke_deletes_it(self):
        (by_email, by_grant) = await self.h.add(INVITED, "third@school.example")
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.confirm(STUDENT, by_email["invitation_id"])
        await self.h.confirm(OTHER, by_grant["invitation_id"])
        status, _ = await self.h.handlers.decide(
            TEACHER,
            {
                "room_id": ROOM,
                "invitation_id": by_grant["invitation_id"],
                "user_id": OTHER,
                "decision": "grant",
            },
        )
        self.assertEqual(status, 200)
        for user in (STUDENT, OTHER):
            record = await self.h.store.managed_record(user, ROOM)
            self.assertEqual(
                (record["user_id"], record["course_room_id"], record["invited_by"]),
                (user, ROOM, TEACHER),
            )
        status, body = await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": by_email["invitation_id"]}
        )
        self.assertEqual((status, body["invitation"]["state"]), (200, "revoked"))
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))
        self.assertIsNotNone(await self.h.store.managed_record(OTHER, ROOM))
        # Course membership is unchanged by a revoke.
        self.assertEqual(self.h.main.membership[(ROOM, STUDENT)], "join")
        # Repeat revoke is 200 unchanged; unknown id is 404.
        status, body = await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": by_email["invitation_id"]}
        )
        self.assertEqual((status, body["invitation"]["state"]), (200, "revoked"))
        status, body = await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": "nope"}
        )
        self.assertEqual((status, body["errcode"]), (404, "M_NOT_FOUND"))
        # A revoked invitation cannot be claimed again.
        status, body = await self.h.confirm(STUDENT, by_email["invitation_id"])
        self.assertEqual((status, body["errcode"]), (404, "M_NOT_FOUND"))

    async def test_claim_join_failure_claims_nothing(self):
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        self.h.joiner.refuse.add(STUDENT)
        status, body = await guarded(
            "confirm", lambda: self.h.confirm(STUDENT, inv["invitation_id"])
        )
        self.assertEqual((status, body), (500, {"error": "Internal server error"}))
        self.assertEqual((await self.h.row(inv["invitation_id"]))["state"], "invited")
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))


class TestConfirm(_Base):
    async def test_confirm_records_the_current_managed_disclosure_version_on_the_ack(
        self,
    ):
        (inv,) = await self.h.add(INVITED)
        status, _ = await self.h.confirm(OTHER, inv["invitation_id"])
        self.assertEqual(status, 200)
        ack = await self.h.store.get_ack(inv["invitation_id"], OTHER)
        self.assertEqual(ack["disclosure_version"], MANAGED_DISCLOSURE_VERSION)
        self.assertIsNone(ack["decision"])

    async def test_confirm_with_an_outdated_disclosure_records_nothing(self):
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        for version in (V - 1, V + 1):
            status, body = await self.h.confirm(STUDENT, inv["invitation_id"], version)
            self.assertEqual(
                (status, body["errcode"]), (409, "ORG.PANGEA.DISCLOSURE_OUTDATED")
            )
        self.assertIsNone(await self.h.store.get_ack(inv["invitation_id"], STUDENT))
        self.assertEqual((await self.h.row(inv["invitation_id"]))["state"], "invited")

    async def test_confirm_rejects_malformed_bodies(self):
        (inv,) = await self.h.add(INVITED)
        for body in (
            None,
            [],
            {"invitation_id": inv["invitation_id"]},
            {"invitation_id": 5, "disclosure_version": V},
            {"invitation_id": inv["invitation_id"], "disclosure_version": True},
            {"invitation_id": inv["invitation_id"], "disclosure_version": "2"},
        ):
            status, payload = await self.h.handlers.confirm(STUDENT, body)
            self.assertEqual((status, payload["errcode"]), (400, "M_INVALID_PARAM"))

    async def test_confirm_answers_one_404_for_unknown_revoked_left_or_taken(self):
        a, b, c = await self.h.add(
            INVITED, "second@school.example", "third@school.example"
        )
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": b["invitation_id"]}
        )
        self.h.verify(THIRD, "third@school.example")
        await self.h.confirm(THIRD, c["invitation_id"])
        await self.h.store.release_on_leave(ROOM, THIRD)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.confirm(STUDENT, a["invitation_id"])
        bodies = []
        for ident in (
            "unknown",
            b["invitation_id"],
            c["invitation_id"],
            a["invitation_id"],
        ):
            status, body = await self.h.confirm(OTHER, ident)
            self.assertEqual(status, 404)
            bodies.append(body)
            self.assertIsNone(await self.h.store.get_ack(ident, OTHER))
        self.assertTrue(all(b == bodies[0] for b in bodies), bodies)
        self.assertEqual(bodies[0], {"error": "Not found", "errcode": "M_NOT_FOUND"})

    async def test_confirm_by_the_claimant_again_is_claimed(self):
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.confirm(STUDENT, inv["invitation_id"])
        status, body = await self.h.confirm(STUDENT, inv["invitation_id"])
        self.assertEqual((status, body["result"]), (200, "claimed"))

    async def test_confirm_after_a_deny_says_denied(self):
        (inv,) = await self.h.add(INVITED)
        await self.h.confirm(OTHER, inv["invitation_id"])
        await self.h.handlers.decide(
            TEACHER,
            {
                "room_id": ROOM,
                "invitation_id": inv["invitation_id"],
                "user_id": OTHER,
                "decision": "deny",
            },
        )
        status, body = await self.h.confirm(OTHER, inv["invitation_id"])
        self.assertEqual((status, body["result"]), (200, "denied"))

    async def test_disclosure_read_returns_the_controls_spec_text_and_its_version(self):
        status, body = await self.h.handlers.disclosure()
        self.assertEqual(status, 200)
        self.assertEqual(body, {"version": 2, "text": MANAGED_DISCLOSURE_TEXT})
        self.assertIn("{course}", body["text"])
        self.assertTrue(
            body["text"].startswith("While you're in {course}, your teacher")
        )
        self.assertIn(
            "If you leave the course, your teacher stops managing", body["text"]
        )
        self.assertTrue(body["text"].endswith("delete your data at any time."))


class TestApprovals(_Base):
    async def decide(self, ident: str, user: str, decision: str) -> Tuple[int, Dict]:
        return await self.h.handlers.decide(
            TEACHER,
            {
                "room_id": ROOM,
                "invitation_id": ident,
                "user_id": user,
                "decision": decision,
            },
        )

    async def test_pending_approvals_list_the_signed_up_as_email_to_admins(self):
        (inv,) = await self.h.add(INVITED)
        self.h.verify(OTHER, "zz-later@gmail.example", 9)
        self.h.verify(OTHER, "other@gmail.example", 3)
        self.h.main.displaynames[OTHER] = "Other Person"
        await self.h.confirm(OTHER, inv["invitation_id"])
        (pending,) = await self.h.pending()
        self.assertEqual(
            {k: v for k, v in pending.items() if k != "acked_at_ms"},
            {
                "invitation_id": inv["invitation_id"],
                "user_id": OTHER,
                "display_name": "Other Person",
                "signed_up_as_email": "other@gmail.example",
                "same_student": False,
            },
        )
        self.assertIsInstance(pending["acked_at_ms"], int)
        self.assertEqual(
            (await self.h.listing())[inv["invitation_id"]]["pending_count"], 1
        )

    async def test_grant_claims_and_closes_other_pending_approvals(self):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        await self.h.confirm(OTHER, ident)
        await self.h.confirm(THIRD, ident)
        self.assertEqual(len(await self.h.pending()), 2)
        status, body = await self.decide(ident, OTHER, "grant")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["invitation"]["state"], "joined")
        self.assertEqual(body["invitation"]["claimant"], OTHER)
        self.assertEqual(
            (await self.h.store.get_ack(ident, OTHER))["decision"], "granted"
        )
        self.assertIsNotNone(await self.h.store.managed_record(OTHER, ROOM))
        self.assertIn((ROOM, OTHER), self.h.joiner.joins)
        self.assertEqual(await self.h.pending(), [])
        self.assertEqual((await self.h.listing())[ident]["pending_count"], 0)
        # Granting the same account again is 200; the other is not live.
        status, _ = await self.decide(ident, OTHER, "grant")
        self.assertEqual(status, 200)
        status, body = await self.decide(ident, THIRD, "grant")
        self.assertEqual(
            (status, body["errcode"]), (409, "ORG.PANGEA.INVITATION_NOT_LIVE")
        )

    async def test_deny_sets_denied_and_removes_the_row_from_pending_approvals(self):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        await self.h.confirm(OTHER, ident)
        status, body = await self.decide(ident, OTHER, "deny")
        self.assertEqual((status, body["invitation"]["state"]), (200, "invited"))
        self.assertEqual(
            (await self.h.store.get_ack(ident, OTHER))["decision"], "denied"
        )
        self.assertEqual(await self.h.pending(), [])
        self.assertIsNone(await self.h.store.managed_record(OTHER, ROOM))
        status, _ = await self.decide(ident, OTHER, "deny")
        self.assertEqual(status, 200)
        # A grant after a deny is allowed while the invitation is live.
        status, body = await self.decide(ident, OTHER, "grant")
        self.assertEqual((status, body["invitation"]["state"]), (200, "joined"))

    async def test_denied_account_that_later_verifies_the_invited_email_is_claimed(
        self,
    ):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        await self.h.confirm(OTHER, ident)
        await self.decide(ident, OTHER, "deny")
        self.h.verify(OTHER, INVITED_KEY)
        await self.h.claims.claim_confirmed_for(OTHER)
        row = await self.h.row(ident)
        self.assertEqual((row["state"], row["claimant"]), ("joined", OTHER))
        self.assertIsNotNone(await self.h.store.managed_record(OTHER, ROOM))

    async def test_decide_errors(self):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        status, body = await self.decide(ident, OTHER, "grant")
        self.assertEqual((status, body["errcode"]), (404, "M_NOT_FOUND"))
        status, body = await self.decide("missing", OTHER, "deny")
        self.assertEqual((status, body["errcode"]), (404, "M_NOT_FOUND"))
        await self.h.confirm(OTHER, ident)
        status, body = await self.decide(ident, OTHER, "maybe")
        self.assertEqual((status, body["errcode"]), (400, "M_INVALID_PARAM"))
        # An invitation of another course is unknown in this one.
        (elsewhere,) = await self.h.add(INVITED, room=OTHER_ROOM)
        await self.h.confirm(OTHER, elsewhere["invitation_id"])
        status, body = await self.decide(elsewhere["invitation_id"], OTHER, "grant")
        self.assertEqual((status, body["errcode"]), (404, "M_NOT_FOUND"))
        await self.h.handlers.revoke(TEACHER, {"room_id": ROOM, "invitation_id": ident})
        for decision in ("grant", "deny"):
            status, body = await self.decide(ident, OTHER, decision)
            self.assertEqual(
                (status, body["errcode"]), (409, "ORG.PANGEA.INVITATION_NOT_LIVE")
            )

    async def test_approve_all_skips_invitations_with_two_pending_accounts(self):
        single, double, dup, none = await self.h.add(
            INVITED,
            "second@school.example",
            "third@school.example",
            "member@school.example",
        )
        await self.h.confirm(OTHER, single["invitation_id"])
        await self.h.confirm(THIRD, double["invitation_id"])
        await self.h.confirm("@fourth:x", double["invitation_id"])
        # dup's only pending account already holds a joined invitation here.
        self.h.verify(STUDENT, "member@school.example")
        await self.h.confirm(STUDENT, none["invitation_id"])
        await self.h.confirm(STUDENT, dup["invitation_id"])
        status, body = await self.h.handlers.approve_all(TEACHER, {"room_id": ROOM})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["granted"], [single["invitation_id"]])
        self.assertEqual(body["skipped_multiple"], [double["invitation_id"]])
        self.assertEqual(
            body["refused"],
            [
                {
                    "invitation_id": dup["invitation_id"],
                    "reason": "already_claimed_in_course",
                }
            ],
        )
        self.assertEqual((await self.h.row(single["invitation_id"]))["claimant"], OTHER)
        self.assertEqual(
            (await self.h.row(double["invitation_id"]))["state"], "invited"
        )
        # Re-running is safe.
        status, again = await self.h.handlers.approve_all(TEACHER, {"room_id": ROOM})
        self.assertEqual((status, again["granted"]), (200, []))


class TestReads(_Base):
    async def test_my_joined_returns_only_the_callers_own_joined_claims_never_revoked_left_or_unconfirmed_rows(
        self,
    ):
        joined, revoked, left, unconfirmed = await self.h.add(
            INVITED,
            "second@school.example",
            "third@school.example",
            "member@school.example",
        )
        (elsewhere,) = await self.h.add("other@gmail.example", room=OTHER_ROOM)
        for address in ADDRESSES:
            self.h.verify(STUDENT, address)
        await self.h.confirm(STUDENT, joined["invitation_id"])
        # Each other row is claimed by STUDENT in turn and then taken away.
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": joined["invitation_id"]}
        )
        await self.h.confirm(STUDENT, revoked["invitation_id"])
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": revoked["invitation_id"]}
        )
        await self.h.confirm(STUDENT, left["invitation_id"])
        await self.h.store.release_on_leave(ROOM, STUDENT)
        await self.h.add(INVITED)  # re-invite resets the first row
        await self.h.confirm(STUDENT, joined["invitation_id"])
        # Another user's claim in another course.
        self.h.verify(OTHER, "other@gmail.example")
        await self.h.confirm(OTHER, elsewhere["invitation_id"])
        status, body = await self.h.handlers.mine_joined(STUDENT)
        self.assertEqual(
            (status, body),
            (
                200,
                {
                    "invitations": [
                        {
                            "invitation_id": joined["invitation_id"],
                            "room_id": ROOM,
                            "invited_by": TEACHER,
                        }
                    ]
                },
            ),
        )
        self.assertNotIn(unconfirmed["invitation_id"], str(body))
        status, body = await self.h.handlers.mine_joined(OTHER)
        self.assertEqual(
            [i["invitation_id"] for i in body["invitations"]],
            [elsewhere["invitation_id"]],
        )
        status, body = await self.h.handlers.mine_joined(THIRD)
        self.assertEqual(body, {"invitations": []})

    async def test_my_pending_returns_only_live_invited_rows_matching_the_callers_verified_emails_that_the_caller_has_not_confirmed(
        self,
    ):
        wanted, confirmed, revoked, unmatched = await self.h.add(
            INVITED,
            "second@school.example",
            "third@school.example",
            "other@gmail.example",
        )
        (other_course,) = await self.h.add(INVITED, room=OTHER_ROOM)
        for address in (INVITED_KEY, "second@school.example", "third@school.example"):
            self.h.verify(STUDENT, address)
        await self.h.store.record_ack(confirmed["invitation_id"], STUDENT, V, 1)
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": revoked["invitation_id"]}
        )
        status, body = await self.h.handlers.mine_pending(STUDENT)
        self.assertEqual(status, 200)
        self.assertEqual(
            sorted(body["invitations"], key=lambda i: i["room_id"]),
            sorted(
                [
                    {
                        "invitation_id": wanted["invitation_id"],
                        "room_id": ROOM,
                        "course_name": "Spanish 101",
                    },
                    {
                        "invitation_id": other_course["invitation_id"],
                        "room_id": OTHER_ROOM,
                        "course_name": "French 201",
                    },
                ],
                key=lambda i: i["room_id"],
            ),
        )
        # No email anywhere in the answer.
        self.assertNotIn("@school", str(body))
        # An unverified account sees nothing.
        status, body = await self.h.handlers.mine_pending(THIRD)
        self.assertEqual(body, {"invitations": []})

    async def test_live_reports_state_in_this_course_only(self):
        (inv,) = await self.h.add(INVITED)
        (elsewhere,) = await self.h.add(INVITED, room=OTHER_ROOM)
        status, body = await self.h.handlers.live(
            TEACHER, {"room_id": ROOM, "invitation_id": inv["invitation_id"]}
        )
        self.assertEqual((status, body), (200, {"live": True, "state": "invited"}))
        for ident in ("unknown", elsewhere["invitation_id"]):
            status, body = await self.h.handlers.live(
                TEACHER, {"room_id": ROOM, "invitation_id": ident}
            )
            self.assertEqual((status, body), (200, {"live": False, "state": None}))
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": inv["invitation_id"]}
        )
        status, body = await self.h.handlers.live(
            TEACHER, {"room_id": ROOM, "invitation_id": inv["invitation_id"]}
        )
        self.assertEqual(body, {"live": False, "state": "revoked"})


class TestAdminGate(_Base):
    TEACHER_CALLS: Tuple[Tuple[str, Dict[str, Any]], ...] = (
        ("add", {"emails": [INVITED], "source": "manual"}),
        ("send", {"items": [{"invitation_id": "x", "expected_send_count": 0}]}),
        ("list", {}),
        ("revoke", {"invitation_id": "x"}),
        ("pending_approvals", {}),
        ("decide", {"invitation_id": "x", "user_id": OTHER, "decision": "grant"}),
        ("approve_all", {}),
        ("invite_member", {"user_id": STUDENT}),
        ("live", {"invitation_id": "x"}),
    )

    async def test_non_admin_cannot_list_invitations_pending_approvals_or_invitation_live_state(
        self,
    ):
        (inv,) = await self.h.add(INVITED)
        self.h.main.threepids[OTHER] = [("other@gmail.example", 1)]
        await self.h.confirm(OTHER, inv["invitation_id"])
        for name in ("list", "pending_approvals", "live"):
            status, body = await getattr(self.h.handlers, name)(
                STUDENT, {"room_id": ROOM, "invitation_id": inv["invitation_id"]}
            )
            self.assertEqual(
                (status, body),
                (
                    403,
                    {
                        "error": "Forbidden: course admin required",
                        "errcode": "M_FORBIDDEN",
                    },
                ),
                name,
            )
            self.assertNotIn("other@gmail", str(body))

    async def test_teacher_writes_require_pl100(self):
        (inv,) = await self.h.add(INVITED)
        before = await self.h.store.list_room(ROOM)
        for name, body in self.TEACHER_CALLS:
            self.h.admins.checks.clear()
            status, payload = await getattr(self.h.handlers, name)(
                STUDENT, {"room_id": ROOM, **body}
            )
            self.assertEqual(status, 403, name)
            self.assertEqual(payload["errcode"], "M_FORBIDDEN")
            # Checked on this request, for this room and caller.
            self.assertEqual(self.h.admins.checks, [(ROOM, STUDENT)], name)
        self.assertEqual(await self.h.store.list_room(ROOM), before)
        self.assertEqual(self.h.mailer.sent, [])
        # Admin of another course is not admin of this one.
        self.h.admins.admins.add(("!third:x", STUDENT))
        status, _ = await self.h.handlers.list(STUDENT, {"room_id": ROOM})
        self.assertEqual(status, 403)
        self.assertEqual(inv["state"], "invited")

    async def test_bad_room_id_is_400(self):
        for room in (None, 5, "", "not-a-room"):
            status, body = await self.h.handlers.list(TEACHER, {"room_id": room})
            self.assertEqual((status, body["errcode"]), (400, "M_INVALID_PARAM"))


class TestAddAndSend(_Base):
    async def test_adding_the_same_email_twice_case_and_space_variants_returns_the_same_invitation(
        self,
    ):
        first = await self.h.add(
            INVITED, "  student@school.EXAMPLE ", "other@gmail.example"
        )
        self.assertEqual(first[0]["invitation_id"], first[1]["invitation_id"])
        self.assertNotEqual(first[0]["invitation_id"], first[2]["invitation_id"])
        self.assertEqual(first[0]["email"], INVITED)
        again = await self.h.add("STUDENT@school.example")
        self.assertEqual(again[0]["invitation_id"], first[0]["invitation_id"])
        self.assertEqual(len(await self.h.store.list_room(ROOM)), 2)
        # Same address in another course is another invitation.
        (other,) = await self.h.add(INVITED, room=OTHER_ROOM)
        self.assertNotEqual(other["invitation_id"], first[0]["invitation_id"])

    async def test_add_returns_the_teacher_view_and_ids_are_opaque(self):
        (inv,) = await self.h.add(INVITED)
        self.assertEqual(
            set(inv),
            {
                "invitation_id",
                "room_id",
                "email",
                "state",
                "source",
                "invited_by",
                "claimant",
                "send_count",
                "last_sent_at_ms",
                "created_at_ms",
                "joined_at_ms",
                "canvas_identity",
                "pending_count",
                "same_student_as",
            },
        )
        self.assertEqual(len(inv["invitation_id"]), 22)
        self.assertEqual(
            (inv["state"], inv["source"], inv["invited_by"], inv["send_count"]),
            ("invited", "manual", TEACHER, 0),
        )
        self.assertFalse(inv["canvas_identity"])

    async def test_add_rejects_invalid_addresses_and_stores_nothing(self):
        status, body = await self.h.handlers.add(
            TEACHER,
            {
                "room_id": ROOM,
                "emails": [INVITED, "not-an-email", "a@b", 7, "x@@y.example"],
                "source": "csv",
            },
        )
        self.assertEqual((status, body["errcode"]), (400, "M_INVALID_PARAM"))
        self.assertEqual(body["invalid_indexes"], [1, 2, 3, 4])
        self.assertNotIn("not-an-email", str(body))
        self.assertEqual(await self.h.store.list_room(ROOM), [])
        for bad in (
            {"room_id": ROOM, "emails": [], "source": "manual"},
            {"room_id": ROOM, "emails": [INVITED] * 501, "source": "manual"},
            {"room_id": ROOM, "emails": [INVITED], "source": "canvas"},
            {"room_id": ROOM, "emails": INVITED, "source": "manual"},
        ):
            status, body = await self.h.handlers.add(TEACHER, bad)
            self.assertEqual((status, body["errcode"]), (400, "M_INVALID_PARAM"))

    async def test_re_invite_resets_a_left_row_to_invited_and_deletes_old_confirmations(
        self,
    ):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.confirm(OTHER, ident)
        await self.h.confirm(STUDENT, ident)
        await self.h.store.release_on_leave(ROOM, STUDENT)
        self.assertEqual((await self.h.row(ident))["state"], "left")
        status, body = await self.h.handlers.send(
            TEACHER,
            {
                "room_id": ROOM,
                "items": [{"invitation_id": ident, "expected_send_count": 0}],
            },
        )
        self.assertEqual(body["results"][0]["outcome"], "skipped")
        (again,) = await self.h.add(INVITED)
        self.assertEqual(again["invitation_id"], ident)
        self.assertEqual(
            (again["state"], again["claimant"], again["joined_at_ms"]),
            ("invited", None, None),
        )
        self.assertIsNone(await self.h.store.get_ack(ident, STUDENT))
        self.assertIsNone(await self.h.store.get_ack(ident, OTHER))
        # A fresh confirmation is required: sign-in alone claims nothing.
        await self.h.claims.claim_confirmed_for(STUDENT)
        self.assertEqual((await self.h.row(ident))["state"], "invited")
        status, body = await self.h.confirm(STUDENT, ident)
        self.assertEqual(body["result"], "claimed")

    async def test_re_invite_resets_a_revoked_row_and_keeps_its_send_count(self):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        await self.h.handlers.send(
            TEACHER,
            {
                "room_id": ROOM,
                "items": [{"invitation_id": ident, "expected_send_count": 0}],
            },
        )
        await self.h.confirm(OTHER, ident)
        await self.h.handlers.revoke(TEACHER, {"room_id": ROOM, "invitation_id": ident})
        (again,) = await self.h.add(INVITED)
        self.assertEqual((again["state"], again["send_count"]), ("invited", 1))
        self.assertIsNone(await self.h.store.get_ack(ident, OTHER))

    async def test_send_is_compare_and_set_per_row(self):
        a, b = await self.h.add(INVITED, "second@school.example")
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": b["invitation_id"]}
        )
        status, body = await self.h.handlers.send(
            TEACHER,
            {
                "room_id": ROOM,
                "items": [
                    {"invitation_id": a["invitation_id"], "expected_send_count": 0},
                    {"invitation_id": a["invitation_id"], "expected_send_count": 0},
                    {"invitation_id": b["invitation_id"], "expected_send_count": 0},
                    {"invitation_id": "unknown", "expected_send_count": 0},
                ],
            },
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(
            [(r["outcome"], r["reason"]) for r in body["results"]],
            [
                ("sent", None),
                ("skipped", "stale"),
                ("skipped", "not_invited"),
                ("skipped", "not_invited"),
            ],
        )
        self.assertIsNone(body["stopped_reason"])
        self.assertEqual(self.h.mailer.sent, [(a["invitation_id"], "cl4sscd")])
        row = await self.h.row(a["invitation_id"])
        self.assertEqual(row["send_count"], 1)
        self.assertIsNotNone(row["last_sent_at_ms"])
        # Resend is explicit and takes the new count.
        status, body = await self.h.handlers.send(
            TEACHER,
            {
                "room_id": ROOM,
                "items": [
                    {"invitation_id": a["invitation_id"], "expected_send_count": 1}
                ],
            },
        )
        self.assertEqual(body["results"][0]["outcome"], "sent")
        self.assertEqual((await self.h.row(a["invitation_id"]))["send_count"], 2)

    async def test_send_without_a_class_code_sends_nothing(self):
        (inv,) = await self.h.add(INVITED)
        self.h.rooms.codes[ROOM] = None
        status, body = await self.h.handlers.send(
            TEACHER,
            {
                "room_id": ROOM,
                "items": [
                    {"invitation_id": inv["invitation_id"], "expected_send_count": 0}
                ],
            },
        )
        self.assertEqual((status, body["errcode"]), (400, "ORG.PANGEA.NO_JOIN_CODE"))
        self.assertEqual(self.h.mailer.sent, [])
        self.assertEqual((await self.h.row(inv["invitation_id"]))["send_count"], 0)

    async def test_send_failure_is_reported_per_row_and_not_counted(self):
        from twisted.mail.smtp import SMTPDeliveryError

        a, b = await self.h.add(INVITED, "second@school.example")
        self.h.mailer.fail_with = SMTPDeliveryError(
            550, b"no such user " + INVITED.encode()
        )
        items = [
            {"invitation_id": a["invitation_id"], "expected_send_count": 0},
            {"invitation_id": b["invitation_id"], "expected_send_count": 0},
        ]
        status, body = await self.h.handlers.send(
            TEACHER, {"room_id": ROOM, "items": items}
        )
        self.assertEqual(
            [(r["outcome"], r["reason"]) for r in body["results"]],
            [("failed", "address_rejected"), ("failed", "address_rejected")],
        )
        self.assertNotIn("school", str(body))
        self.assertEqual((await self.h.row(a["invitation_id"]))["send_count"], 0)
        # A transport failure stops the batch.
        self.h.mailer.fail_with = ConnectionRefusedError("smtp down")
        status, body = await self.h.handlers.send(
            TEACHER, {"room_id": ROOM, "items": items}
        )
        self.assertEqual(
            [(r["invitation_id"], r["outcome"], r["reason"]) for r in body["results"]],
            [(a["invitation_id"], "failed", "mail_error")],
        )
        self.assertEqual(body["stopped_reason"], "mail_error")
        self.assertEqual((await self.h.row(b["invitation_id"]))["send_count"], 0)

    async def test_send_validates_items(self):
        for items in (
            [],
            [{"invitation_id": "a"}],
            [{"invitation_id": "a", "expected_send_count": -1}],
            [{"invitation_id": "a", "expected_send_count": 0}] * 51,
        ):
            status, body = await self.h.handlers.send(
                TEACHER, {"room_id": ROOM, "items": items}
            )
            self.assertEqual((status, body["errcode"]), (400, "M_INVALID_PARAM"))


class TestInviteMember(_Base):
    async def test_invite_to_a_seat_creates_the_invitation_from_the_members_first_bound_email_requires_pl100_and_returns_logs_and_reports_no_address(
        self,
    ):
        self.h.main.membership[(ROOM, STUDENT)] = "join"
        self.h.verify(STUDENT, "zz-late@school.example", 9)
        self.h.verify(STUDENT, "member@school.example", 2)
        captured = self.capture_logs()
        with patch.object(report, "sentry_sdk") as sentry:
            status, body = await self.h.handlers.invite_member(
                STUDENT, {"room_id": ROOM, "user_id": STUDENT}
            )
            self.assertEqual(status, 403)
            status, body = await self.h.handlers.invite_member(
                TEACHER, {"room_id": ROOM, "user_id": STUDENT}
            )
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body), {"invitation_id", "state"})
        self.assertEqual(body["state"], "invited")
        row = await self.h.row(body["invitation_id"])
        self.assertEqual(
            (row["email_key"], row["email"], row["source"]),
            ("member@school.example", None, "member"),
        )
        listed = (await self.h.listing())[body["invitation_id"]]
        self.assertIsNone(listed["email"])
        self.assertEqual(listed["source"], "member")
        self.assertNotIn("school", str(body))
        self.assertNotIn("school", captured.text())
        self.assertNotIn("school", str(sentry.mock_calls))
        # Idempotent; the student sees it in "my pending".
        status, again = await self.h.handlers.invite_member(
            TEACHER, {"room_id": ROOM, "user_id": STUDENT}
        )
        self.assertEqual(again, body)
        _, mine = await self.h.handlers.mine_pending(STUDENT)
        self.assertEqual(
            [i["invitation_id"] for i in mine["invitations"]], [body["invitation_id"]]
        )
        # Once claimed, the joined invitation is returned.
        await self.h.confirm(STUDENT, body["invitation_id"])
        status, joined = await self.h.handlers.invite_member(
            TEACHER, {"room_id": ROOM, "user_id": STUDENT}
        )
        self.assertEqual(
            joined, {"invitation_id": body["invitation_id"], "state": "joined"}
        )

    async def test_invite_member_refusals(self):
        status, body = await self.h.handlers.invite_member(
            TEACHER, {"room_id": ROOM, "user_id": STUDENT}
        )
        self.assertEqual((status, body["errcode"]), (409, "ORG.PANGEA.NOT_MEMBER"))
        self.h.main.membership[(ROOM, STUDENT)] = "join"
        status, body = await self.h.handlers.invite_member(
            TEACHER, {"room_id": ROOM, "user_id": STUDENT}
        )
        self.assertEqual((status, body["errcode"]), (409, "ORG.PANGEA.NO_EMAIL"))
        self.assertEqual(await self.h.store.list_room(ROOM), [])

    async def test_invite_member_returns_a_joined_invitation_the_member_holds(self):
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.confirm(STUDENT, inv["invitation_id"])
        self.h.verify(STUDENT, "another@school.example", 0)
        status, body = await self.h.handlers.invite_member(
            TEACHER, {"room_id": ROOM, "user_id": STUDENT}
        )
        self.assertEqual(
            body, {"invitation_id": inv["invitation_id"], "state": "joined"}
        )
        self.assertEqual(len(await self.h.store.list_room(ROOM)), 1)


class TestHint(_Base):
    async def test_hint_lookup_returns_only_course_name_and_masked_email_hint_for_a_live_invitation_nothing_for_revoked_or_joined(
        self,
    ):
        live, revoked, joined, left = await self.h.add(
            INVITED,
            "second@school.example",
            "third@school.example",
            "other@gmail.example",
        )
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": revoked["invitation_id"]}
        )
        self.h.verify(STUDENT, "third@school.example")
        await self.h.confirm(STUDENT, joined["invitation_id"])
        self.h.verify(OTHER, "other@gmail.example")
        await self.h.confirm(OTHER, left["invitation_id"])
        await self.h.store.release_on_leave(ROOM, OTHER)
        status, body = await self.h.handlers.hint(
            {"invitation_id": live["invitation_id"]}
        )
        self.assertEqual(
            (status, body),
            (
                200,
                {
                    "course_name": "Spanish 101",
                    "masked_email_hint": "s***@school.example",
                },
            ),
        )
        not_found = {"error": "Not found", "errcode": "M_NOT_FOUND"}
        for ident in (
            revoked["invitation_id"],
            joined["invitation_id"],
            left["invitation_id"],
            "unknown",
            "",
            None,
            "x" * 300,
        ):
            status, body = await self.h.handlers.hint({"invitation_id": ident})
            self.assertEqual((status, body), (404, not_found), ident)

    def test_mask_email(self):
        self.assertEqual(mask_email("Student@School.example"), "s***@school.example")
        self.assertEqual(mask_email("a@b.example"), "a***@b.example")


class TestClaimByEmailHook(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from synapse_pangea_chat.email_invite.claim_by_email import ClaimByEmail

        self.legacy = MagicMock()
        self.legacy.prepared_for_emails = _async_return([])
        api = MagicMock()
        api._hs.get_datastores.return_value.main = self.h.main
        self.hook = ClaimByEmail(
            api, self.legacy, MagicMock(), student_claims=self.h.claims
        )

    async def test_confirmed_pending_invitation_is_claimed_when_the_invited_email_is_later_added_and_verified(
        self,
    ):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        status, body = await self.h.confirm(STUDENT, ident)
        self.assertEqual(body["result"], "pending_approval")
        # Sign-in alone, without the address, claims nothing.
        await self.hook.on_user_login(STUDENT, None, None)
        self.assertEqual((await self.h.row(ident))["state"], "invited")
        self.h.verify(STUDENT, INVITED_KEY)
        await self.hook.on_add_user_third_party_identifier(
            STUDENT, "email", INVITED_KEY
        )
        row = await self.h.row(ident)
        self.assertEqual((row["state"], row["claimant"]), ("joined", STUDENT))

    async def test_add_email_claim_records_the_managed_account_and_closes_the_invitations_other_pending_approvals(
        self,
    ):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        await self.h.confirm(OTHER, ident)
        await self.h.confirm(STUDENT, ident)
        self.assertEqual(len(await self.h.pending()), 2)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.hook.on_add_user_third_party_identifier(
            STUDENT, "email", INVITED_KEY
        )
        record = await self.h.store.managed_record(STUDENT, ROOM)
        self.assertEqual(
            (record["invited_by"], record["course_room_id"]), (TEACHER, ROOM)
        )
        self.assertEqual(await self.h.pending(), [])
        self.assertEqual((await self.h.listing())[ident]["pending_count"], 0)

    async def test_unconfirmed_matching_invitation_is_not_claimed_at_sign_in(self):
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.hook.on_user_login(STUDENT, None, None)
        self.assertEqual((await self.h.row(inv["invitation_id"]))["state"], "invited")
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))

    async def test_claim_never_fails_login(self):
        (inv,) = await self.h.add(INVITED)
        await self.h.confirm(STUDENT, inv["invitation_id"])
        self.h.verify(STUDENT, INVITED_KEY)
        captured = self.capture_logs()
        with patch.object(report, "sentry_sdk") as sentry:
            self.h.joiner.refuse.add(STUDENT)
            await self.hook.on_user_login(STUDENT, None, None)
            self.h.joiner.refuse.clear()
            self.h.main.db_pool.error = RuntimeError(
                "duplicate key (email_key)=(" + INVITED_KEY + ")"
            )
            await self.hook.on_user_login(STUDENT, None, None)
            await self.hook.on_add_user_third_party_identifier(
                STUDENT, "email", INVITED_KEY
            )
        self.h.main.db_pool.error = None
        self.assertGreaterEqual(sentry.capture_message.call_count, 2)
        self.assertNotIn(INVITED_KEY, str(sentry.mock_calls))
        self.assertNotIn(INVITED_KEY, captured.text())
        self.assertEqual((await self.h.row(inv["invitation_id"]))["state"], "invited")


class TestMembershipRelease(_Base):
    async def test_leave_kick_or_ban_sets_the_invitation_left_and_deletes_the_managed_record(
        self,
    ):
        release = MembershipRelease(self.h.store)
        for membership, sender in (
            ("leave", STUDENT),
            ("leave", TEACHER),
            ("ban", TEACHER),
        ):
            (inv,) = await self.h.add(INVITED)
            self.h.main.threepids[STUDENT] = [(INVITED_KEY, 1)]
            status, body = await self.h.confirm(STUDENT, inv["invitation_id"])
            self.assertEqual(body["result"], "claimed", membership)
            await release.on_new_event(
                _member_event(ROOM, STUDENT, membership, sender), {}
            )
            row = await self.h.row(inv["invitation_id"])
            self.assertEqual(row["state"], "left", (membership, sender))
            self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))

    async def test_other_events_release_nothing(self):
        release = MembershipRelease(self.h.store)
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.confirm(STUDENT, inv["invitation_id"])
        for event in (
            _member_event(ROOM, STUDENT, "join", STUDENT),
            _member_event(OTHER_ROOM, STUDENT, "leave", STUDENT),
            _member_event(ROOM, OTHER, "leave", OTHER),
            SimpleNamespace(
                type="m.room.message",
                room_id=ROOM,
                state_key=None,
                content={},
                sender=STUDENT,
                is_state=lambda: False,
            ),
        ):
            await release.on_new_event(event, {})
        self.assertEqual((await self.h.row(inv["invitation_id"]))["state"], "joined")
        self.assertIsNotNone(await self.h.store.managed_record(STUDENT, ROOM))

    async def test_release_failure_is_reported_without_raising(self):
        release = MembershipRelease(self.h.store)
        self.h.main.db_pool.error = RuntimeError("db down")
        with patch.object(report, "sentry_sdk") as sentry:
            await release.on_new_event(
                _member_event(ROOM, STUDENT, "leave", STUDENT), {}
            )
        self.h.main.db_pool.error = None
        sentry.capture_message.assert_called_once()

    async def test_class_code_rejoin_restores_nothing(self):
        release = MembershipRelease(self.h.store)
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.confirm(STUDENT, inv["invitation_id"])
        await release.on_new_event(_member_event(ROOM, STUDENT, "leave", STUDENT), {})
        await release.on_new_event(_member_event(ROOM, STUDENT, "join", STUDENT), {})
        await self.h.claims.claim_confirmed_for(STUDENT)
        self.assertEqual((await self.h.row(inv["invitation_id"]))["state"], "left")
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))
        _, joined = await self.h.handlers.mine_joined(STUDENT)
        self.assertEqual(joined, {"invitations": []})

    async def test_leave_during_a_claim_releases_it(self):
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        original = self.h.joiner.force_join

        async def join_then_leave(room_id: str, user_id: str) -> Dict[str, Any]:
            result = await original(room_id, user_id)
            self.h.main.membership[(room_id, user_id)] = "leave"
            return result

        self.h.joiner.force_join = join_then_leave  # type: ignore[method-assign]
        await self.h.confirm(STUDENT, inv["invitation_id"])
        self.assertEqual((await self.h.row(inv["invitation_id"]))["state"], "left")
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))


class TestInviteEmail(unittest.IsolatedAsyncioTestCase):
    def mailer(self) -> Tuple[InviteMailer, List[Dict[str, Any]], Any]:
        rendered: List[Dict[str, Any]] = []

        class Template:
            def render(self, **kwargs: Any) -> str:
                rendered.append(kwargs)
                return "body"

        api = MagicMock()
        api.read_templates.return_value = [Template(), Template()]
        sender = MagicMock()
        sender.send_email = _async_return(None)
        api._hs.get_send_email_handler.return_value = sender
        api._hs.config.email.email_app_name = "Pangea Chat"
        main = FakeMain()
        main.displaynames[TEACHER] = "Ms Teacher"
        api._hs.get_datastores.return_value.main = main
        rooms = FakeRooms(main)
        config = PangeaChatConfig(app_base_url="https://app.example.test/")
        return InviteMailer(api, config, rooms, Accounts(api)), rendered, sender

    async def test_invite_link_carries_class_code_and_inv_id(self):
        mailer, rendered, sender = self.mailer()
        row = {
            "id": "Abc_def-123456789012",
            "course_room_id": ROOM,
            "email": INVITED,
            "email_key": INVITED_KEY,
            "invited_by": TEACHER,
        }
        await mailer.send_invite(row, "cl4sscd")
        self.assertEqual(
            rendered[0]["join_url"],
            "https://app.example.test/cl4sscd?inv=Abc_def-123456789012",
        )
        self.assertEqual(rendered[0]["course_title"], "Spanish 101")
        self.assertEqual(rendered[0]["course_description"], "Learn Spanish")
        self.assertEqual(rendered[0]["inviter_names"], ["Ms Teacher"])
        kwargs = sender.send_email.calls[0]
        self.assertEqual(kwargs["email_address"], INVITED)
        self.assertEqual(kwargs["subject"], "Join Spanish 101 on Pangea Chat")
        self.assertEqual(
            build_join_url("https://app.example.test", "cl4sscd", "a b"),
            "https://app.example.test/cl4sscd?inv=a%20b",
        )
        self.assertEqual(
            build_join_url("https://app.example.test", "cl4sscd"),
            "https://app.example.test/cl4sscd",
        )

    async def test_member_invitation_is_mailed_to_its_key(self):
        mailer, rendered, sender = self.mailer()
        row = {
            "id": "i",
            "course_room_id": ROOM,
            "email": None,
            "email_key": INVITED_KEY,
            "invited_by": TEACHER,
        }
        await mailer.send_invite(row, "cl4sscd")
        self.assertEqual(sender.send_email.calls[0]["email_address"], INVITED_KEY)

    async def test_invite_email_in_release_1a_never_reads_lti_state_and_omits_the_canvas_line(
        self,
    ):
        mailer, rendered, sender = self.mailer()
        statements: List[str] = []
        pool = RollbackPool()
        pool.on_statement = lambda sql, args: statements.append(sql)
        mailer._api._hs.get_datastores.return_value.main.db_pool = pool
        row = {
            "id": "i",
            "course_room_id": ROOM,
            "email": INVITED,
            "email_key": INVITED_KEY,
            "invited_by": TEACHER,
        }
        await mailer.send_invite(row, "cl4sscd")
        self.assertIs(rendered[0]["canvas_connected"], False)
        self.assertIs(rendered[1]["canvas_connected"], False)
        self.assertEqual(statements, [])
        self.assertIsNone(mailer.canvas_connected)


class TestNoEmailInLogs(_Base):
    async def test_no_email_in_logs(self):
        captured = self.capture_logs()
        with patch.object(report, "sentry_sdk") as sentry:
            a, b = await self.h.add(INVITED, "second@school.example")
            self.h.verify(STUDENT, INVITED_KEY)
            self.h.verify(OTHER, "other@gmail.example")
            await self.h.confirm(OTHER, a["invitation_id"])
            await self.h.pending()
            await self.h.handlers.approve_all(TEACHER, {"room_id": ROOM})
            await self.h.confirm(STUDENT, b["invitation_id"])
            await self.h.handlers.send(
                TEACHER,
                {
                    "room_id": ROOM,
                    "items": [
                        {"invitation_id": b["invitation_id"], "expected_send_count": 0}
                    ],
                },
            )
            self.h.mailer.fail_with = RuntimeError("refused " + INVITED_KEY)
            await self.h.handlers.send(
                TEACHER,
                {
                    "room_id": ROOM,
                    "items": [
                        {"invitation_id": b["invitation_id"], "expected_send_count": 0}
                    ],
                },
            )
            self.h.main.db_pool.error = RuntimeError("DETAIL: (" + INVITED_KEY + ")")
            status, _ = await guarded(
                "confirm", lambda: self.h.confirm(STUDENT, a["invitation_id"])
            )
            self.assertEqual(status, 500)
            self.h.main.db_pool.error = None
            await self.h.handlers.add(
                TEACHER, {"room_id": ROOM, "emails": ["bad", INVITED], "source": "csv"}
            )
        text = captured.text()
        self.assertTrue(text, "expected some log lines")
        for address in ADDRESSES:
            self.assertNotIn(address, text.lower())
            self.assertNotIn(address, str(sentry.mock_calls).lower())
        self.assertNotIn("school.example", text.lower())


def _member_event(room_id: str, user_id: str, membership: str, sender: str) -> Any:
    return SimpleNamespace(
        type="m.room.member",
        room_id=room_id,
        state_key=user_id,
        sender=sender,
        content={"membership": membership},
        is_state=lambda: True,
        membership=membership,
    )


def _async_return(value: Any) -> Any:
    calls: List[Dict[str, Any]] = []

    async def fn(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return value

    fn.calls = calls  # type: ignore[attr-defined]
    return fn


class _Captured(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)


_LOGGERS = (
    "synapse.module.synapse_pangea_chat.student_invitations",
    "synapse.module.synapse_pangea_chat.email_invite",
)


def _capture_logs() -> _Captured:
    handler = _Captured()
    for name in _LOGGERS:
        log = logging.getLogger(name)
        log.setLevel(logging.DEBUG)
        log.addHandler(handler)
    return handler


def _release_logs(handler: _Captured) -> None:
    for name in _LOGGERS:
        logging.getLogger(name).removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
