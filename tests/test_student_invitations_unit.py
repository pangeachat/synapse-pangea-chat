"""Student invitations: the claim rules, pending approvals and the routes'
handlers (CONTRACTS C2), over the real store on an in-memory database.

Synapse's surfaces the handlers read (threepids, profiles, room state, the
force-join) are doubles; the HTTP layer, real power levels, the membership
callback on real events and the sent email are covered end to end in
``test_student_invitations_e2e.py``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest.mock import MagicMock, patch

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.email_invite.build_join_url import build_join_url
from synapse_pangea_chat.notice_delivery.rate_limit import SlidingWindowRateLimiter
from synapse_pangea_chat.student_invitations import report
from synapse_pangea_chat.student_invitations.accounts import Accounts
from synapse_pangea_chat.student_invitations.api import (
    InvitationOpenRoute,
    StudentInvitationHandlers,
    StudentInvitationsRoot,
    guarded,
)
from synapse_pangea_chat.student_invitations.approvals import Approvals
from synapse_pangea_chat.student_invitations.claim import StudentClaims, now_ms
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

    async def get_user_id_by_threepid(self, medium: str, address: str) -> Optional[str]:
        # Synapse binds an address to at most one account, stored canonical.
        for user_id, entries in self.threepids.items():
            if medium == "email" and any(a.lower() == address for a, _ in entries):
                return user_id
        return None

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

    async def open(self, user: str, invitation_id: str) -> Tuple[int, Dict[str, Any]]:
        return await self.handlers.open(user, invitation_id)

    async def events(self, room: str = ROOM, **query: str) -> List[Dict[str, Any]]:
        status, body = await self.handlers.events(TEACHER, {"room_id": room, **query})
        assert status == 200, body
        return body["events"]

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
    async def test_a_verified_match_claims_with_no_confirmation_and_another_address_only_requests(
        self,
    ):
        """Seats amendment 2026-10-10: no checkbox. A verified match claims at
        once; another address records a request, and claims nothing."""
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        status, body = await self.h.open(OTHER, ident)
        self.assertEqual((status, body["result"]), (200, "pending"))
        self.assertIsNotNone(await self.h.store.get_request(ident, OTHER))
        self.assertEqual((await self.h.row(ident))["state"], "invited")
        self.assertNotIn((ROOM, OTHER), self.h.joiner.joins)
        self.h.verify(STUDENT, INVITED_KEY)
        status, body = await self.h.open(STUDENT, ident)
        self.assertEqual(
            (status, body),
            (200, {"result": "claimed", "invitation_id": ident, "room_id": ROOM}),
        )
        row = await self.h.row(ident)
        self.assertEqual((row["state"], row["claimant"]), ("joined", STUDENT))
        self.assertIn((ROOM, STUDENT), self.h.joiner.joins)
        # A match never writes a request: a request never stands in for a claim.
        self.assertIsNone(await self.h.store.get_request(ident, STUDENT))

    async def test_claim_transaction_rechecks_every_rule_itself(self):
        """The database step refuses on its own, whatever the caller checked."""
        (inv, by_match) = await self.h.add(INVITED, "second@school.example")
        ident = inv["invitation_id"]
        # No match, no request: refused, even with the teacher's grant.
        for grant in (False, True):
            outcome, _ = await self.h.store.claim_txn(
                ident, STUDENT, email_match=False, grant=grant, now_ms=1
            )
            self.assertEqual(outcome, "not_eligible", grant)
        # A request with no decision and no grant: refused.
        await self.h.store.record_request(ident, STUDENT, 2)
        outcome, _ = await self.h.store.claim_txn(
            ident, STUDENT, email_match=False, grant=False, now_ms=3
        )
        self.assertEqual(outcome, "not_eligible")
        row = await self.h.row(ident)
        self.assertEqual((row["state"], row["claimant"]), ("invited", None))
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))
        self.assertIsNone((await self.h.store.get_request(ident, STUDENT))["decision"])
        # A grant of the request claims, and records the decision with it.
        outcome, row = await self.h.store.claim_txn(
            ident, STUDENT, email_match=False, grant=True, now_ms=4, actor=TEACHER
        )
        self.assertEqual(outcome, "claimed")
        self.assertEqual(
            (row["state"], row["claimant"], row["joined_at_ms"]), ("joined", STUDENT, 4)
        )
        self.assertEqual(
            (await self.h.store.get_request(ident, STUDENT))["decision"], "granted"
        )
        self.assertEqual(
            await self.h.store.managed_record(STUDENT, ROOM),
            {"user_id": STUDENT, "course_room_id": ROOM, "created_at_ms": 4},
        )
        # A verified match needs no request at all.
        outcome, _ = await self.h.store.claim_txn(
            by_match["invitation_id"], OTHER, email_match=True, grant=False, now_ms=5
        )
        self.assertEqual(outcome, "claimed")
        self.assertIsNone(
            await self.h.store.get_request(by_match["invitation_id"], OTHER)
        )

    async def test_managed_record_alone_blocks_a_second_claim_in_the_course(self):
        first, second = await self.h.add(INVITED, "second@school.example")
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.open(STUDENT, first["invitation_id"])
        # Even if the joined row were missed, the managed record refuses.
        await self.h.main.db_pool.runInteraction(
            "simulate",
            lambda txn: txn.execute(
                "UPDATE pangea_student_invitation SET claimant = NULL WHERE id = ?",
                (first["invitation_id"],),
            ),
        )
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
        status, body = await self.h.open(STUDENT, own["invitation_id"])
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
        status, body = await self.h.open(STUDENT, elsewhere["invitation_id"])
        self.assertEqual(
            (status, body["errcode"]), (409, "ORG.PANGEA.ALREADY_CLAIMED_IN_COURSE")
        )
        # Admin of this course only: in another course the claim is managed.
        status, body = await self.h.open(STUDENT, other_course["invitation_id"])
        self.assertEqual(body["result"], "claimed")
        self.assertIsNotNone(await self.h.store.managed_record(STUDENT, OTHER_ROOM))

    async def test_non_admin_claim_still_records_managed(self):
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        status, body = await self.h.open(STUDENT, inv["invitation_id"])
        self.assertEqual(body["result"], "claimed")
        record = await self.h.store.managed_record(STUDENT, ROOM)
        # The managed record carries only its time (no disclosure, no inviter).
        self.assertEqual(set(record), {"user_id", "course_room_id", "created_at_ms"})
        self.assertEqual((record["user_id"], record["course_room_id"]), (STUDENT, ROOM))
        self.assertIn((ROOM, STUDENT), self.h.admins.checks)

    async def test_grant_without_a_request_claims_nothing(self):
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
        status, body = await self.h.open(OTHER, inv["invitation_id"])
        self.assertEqual(body["result"], "pending")
        self.assertEqual((await self.h.row(inv["invitation_id"]))["state"], "invited")

    async def test_a_request_from_another_account_never_blocks_the_rightful_email_match(
        self,
    ):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        status, body = await self.h.open(OTHER, ident)
        self.assertEqual(body["result"], "pending")
        self.h.verify(STUDENT, INVITED_KEY)
        status, body = await self.h.open(STUDENT, ident)
        self.assertEqual((status, body["result"]), (200, "claimed"))
        self.assertEqual((await self.h.row(ident))["claimant"], STUDENT)
        # The forwarded-link account is no longer a pending approval.
        self.assertEqual(await self.h.pending(), [])

    async def test_invitation_claimed_by_at_most_one_account(self):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        await self.h.open(OTHER, ident)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.open(STUDENT, ident)
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
        outcome, _ = await self.h.store.claim_txn(
            ident, THIRD, email_match=True, grant=False, now_ms=6
        )
        self.assertEqual(outcome, "not_live")
        row = await self.h.row(ident)
        self.assertEqual((row["state"], row["claimant"]), ("joined", STUDENT))
        self.assertIsNone(await self.h.store.managed_record(OTHER, ROOM))
        self.assertIsNone(await self.h.store.managed_record(THIRD, ROOM))
        # A third account opening it sees the one 404 body.
        status, body = await self.h.open(THIRD, ident)
        self.assertEqual((status, body["errcode"]), (404, "M_NOT_FOUND"))

    async def test_second_invitation_in_the_same_course_refused_for_an_account_that_already_claimed_one(
        self,
    ):
        first, second = await self.h.add(INVITED, "second@school.example")
        self.h.verify(STUDENT, INVITED_KEY)
        self.h.verify(STUDENT, "second@school.example", 2)
        status, body = await self.h.open(STUDENT, first["invitation_id"])
        self.assertEqual(body["result"], "claimed")
        status, body = await self.h.open(STUDENT, second["invitation_id"])
        self.assertEqual(
            (status, body["errcode"]), (409, "ORG.PANGEA.ALREADY_CLAIMED_IN_COURSE")
        )
        # No request is written (the address matches); the roster marks the
        # duplicate from the verified address alone (read time).
        self.assertIsNone(
            await self.h.store.get_request(second["invitation_id"], STUDENT)
        )
        listed = await self.h.listing()
        self.assertEqual(listed[second["invitation_id"]]["state"], "invited")
        self.assertEqual(listed[second["invitation_id"]]["same_student_as"], STUDENT)
        self.assertIsNone(listed[first["invitation_id"]]["same_student_as"])
        # There is no request to grant.
        status, body = await self.h.handlers.decide(
            TEACHER,
            {
                "room_id": ROOM,
                "invitation_id": second["invitation_id"],
                "user_id": STUDENT,
                "decision": "grant",
            },
        )
        self.assertEqual((status, body["errcode"]), (404, "M_NOT_FOUND"))
        # The database refuses it even past the handler's own check.
        outcome, _ = await self.h.store.claim_txn(
            second["invitation_id"], STUDENT, email_match=True, grant=False, now_ms=9
        )
        self.assertEqual(outcome, "already_claimed_in_course")

    async def test_one_joined_invitation_per_course_holds_in_the_database_itself(self):
        first, second = await self.h.add(INVITED, "second@school.example")
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.open(STUDENT, first["invitation_id"])
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
            status, body = await self.h.open(STUDENT, inv["invitation_id"])
            self.assertEqual(body["result"], "claimed")
        self.assertIsNotNone(await self.h.store.managed_record(STUDENT, ROOM))
        self.assertIsNotNone(await self.h.store.managed_record(STUDENT, OTHER_ROOM))

    async def test_every_claim_writes_managed_record_revoke_deletes_it(self):
        (by_email, by_grant) = await self.h.add(INVITED, "third@school.example")
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.open(STUDENT, by_email["invitation_id"])
        await self.h.open(OTHER, by_grant["invitation_id"])
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
                (record["user_id"], record["course_room_id"]), (user, ROOM)
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
        status, body = await self.h.open(STUDENT, by_email["invitation_id"])
        self.assertEqual((status, body["errcode"]), (404, "M_NOT_FOUND"))

    async def test_claim_join_failure_claims_nothing(self):
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        self.h.joiner.refuse.add(STUDENT)
        with patch.object(report, "sentry_sdk") as sentry:
            status, body = await guarded(
                "open", lambda: self.h.open(STUDENT, inv["invitation_id"])
            )
        self.assertEqual((status, body), (500, {"error": "Internal server error"}))
        # The failure is the refused join itself, not anything else.
        self.assertIn((ROOM, STUDENT), self.h.joiner.joins)
        self.assertIn("ClaimJoinFailed", str(sentry.mock_calls))
        self.assertEqual((await self.h.row(inv["invitation_id"]))["state"], "invited")
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))


class TestOpen(_Base):
    """S1 open (seats amendment 2026-10-10): no body, no disclosure."""

    async def test_open_with_another_address_records_a_request_once(self):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        for _ in range(2):
            status, body = await self.h.open(OTHER, ident)
            self.assertEqual((status, body["result"]), (200, "pending"))
        request = await self.h.store.get_request(ident, OTHER)
        self.assertEqual(
            set(request), {"invitation_id", "user_id", "requested_at_ms", "decision"}
        )
        self.assertIsNone(request["decision"])
        self.assertEqual(
            [e["action"] for e in await self.h.events()],
            ["request_made", "students_added"],
        )

    async def test_open_answers_one_404_for_unknown_revoked_left_or_taken(self):
        a, b, c = await self.h.add(
            INVITED, "second@school.example", "third@school.example"
        )
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": b["invitation_id"]}
        )
        self.h.verify(THIRD, "third@school.example")
        await self.h.open(THIRD, c["invitation_id"])
        await self.h.store.release_on_leave(ROOM, THIRD, 1)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.open(STUDENT, a["invitation_id"])
        bodies = []
        for ident in (
            "unknown",
            "",
            "x" * 300,
            b["invitation_id"],
            c["invitation_id"],
            a["invitation_id"],
        ):
            status, body = await self.h.open(OTHER, ident)
            self.assertEqual(status, 404)
            bodies.append(body)
            self.assertIsNone(await self.h.store.get_request(ident, OTHER))
        self.assertTrue(all(b == bodies[0] for b in bodies), bodies)
        self.assertEqual(bodies[0], {"error": "Not found", "errcode": "M_NOT_FOUND"})

    async def test_open_by_the_claimant_again_is_claimed(self):
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.open(STUDENT, inv["invitation_id"])
        status, body = await self.h.open(STUDENT, inv["invitation_id"])
        self.assertEqual((status, body["result"]), (200, "claimed"))

    async def test_open_after_a_deny_says_denied(self):
        (inv,) = await self.h.add(INVITED)
        await self.h.open(OTHER, inv["invitation_id"])
        await self.h.handlers.decide(
            TEACHER,
            {
                "room_id": ROOM,
                "invitation_id": inv["invitation_id"],
                "user_id": OTHER,
                "decision": "deny",
            },
        )
        status, body = await self.h.open(OTHER, inv["invitation_id"])
        self.assertEqual((status, body["result"]), (200, "denied"))

    async def test_open_route_takes_the_id_from_the_path_and_an_empty_body(self):
        calls: List[Any] = []

        async def handler(caller: str, ident: str) -> Tuple[int, Any]:
            calls.append((caller, ident))
            return 200, {}

        class Auth:
            async def get_user_by_req(self, request: Any) -> Any:
                return MagicMock(user=MagicMock(to_string=lambda: STUDENT))

        homeserver = MagicMock()
        homeserver.get_auth.return_value = Auth()
        route = InvitationOpenRoute(
            homeserver,
            "student_invitations_open",
            "POST",
            "student",
            handler,
            SlidingWindowRateLimiter(requests_per_burst=50, burst_duration_seconds=60),
        )
        root = StudentInvitationsRoot(route)
        self.assertIs(root.getChild(b"inv-1", MagicMock()), route)

        def request(postpath: List[bytes], body: bytes) -> Any:
            r = MagicMock()
            r.prepath = [b"_synapse", b"student_invitations", b"inv-1"]
            r.postpath = postpath
            r.content = io.BytesIO(body)
            return r

        self.assertEqual(await route._dispatch(request([b"open"], b"{}")), (200, {}))
        self.assertEqual(calls, [(STUDENT, "inv-1")])
        # The body is exactly `{}`.
        for body in (b"", b'{"x": 1}', b"[]", b"not json"):
            status, answer = await route._dispatch(request([b"open"], body))
            self.assertEqual(
                (status, answer["errcode"]), (400, "M_INVALID_PARAM"), body
            )
        for postpath in ([], [b"close"], [b"open", b"more"]):
            status, _ = await route._dispatch(request(postpath, b"{}"))
            self.assertEqual(status, 404, postpath)
        self.assertEqual(len(calls), 1)


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
        await self.h.open(OTHER, inv["invitation_id"])
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
        await self.h.open(OTHER, ident)
        await self.h.open(THIRD, ident)
        self.assertEqual(len(await self.h.pending()), 2)
        status, body = await self.decide(ident, OTHER, "grant")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["invitation"]["state"], "joined")
        self.assertEqual(body["invitation"]["claimant"], OTHER)
        self.assertEqual(
            (await self.h.store.get_request(ident, OTHER))["decision"], "granted"
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
        await self.h.open(OTHER, ident)
        status, body = await self.decide(ident, OTHER, "deny")
        self.assertEqual((status, body["invitation"]["state"]), (200, "invited"))
        self.assertEqual(
            (await self.h.store.get_request(ident, OTHER))["decision"], "denied"
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
        await self.h.open(OTHER, ident)
        await self.decide(ident, OTHER, "deny")
        self.h.verify(OTHER, INVITED_KEY)
        await self.h.claims.claim_matching_for(OTHER)
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
        await self.h.open(OTHER, ident)
        status, body = await self.decide(ident, OTHER, "maybe")
        self.assertEqual((status, body["errcode"]), (400, "M_INVALID_PARAM"))
        # An invitation of another course is unknown in this one.
        (elsewhere,) = await self.h.add(INVITED, room=OTHER_ROOM)
        await self.h.open(OTHER, elsewhere["invitation_id"])
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
        await self.h.open(OTHER, single["invitation_id"])
        await self.h.open(THIRD, double["invitation_id"])
        await self.h.open("@fourth:x", double["invitation_id"])
        # dup's only requesting account already holds a joined invitation here.
        self.h.verify(STUDENT, "member@school.example")
        await self.h.open(STUDENT, none["invitation_id"])
        await self.h.open(STUDENT, dup["invitation_id"])
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
        await self.h.open(STUDENT, joined["invitation_id"])
        # Each other row is claimed by STUDENT in turn and then taken away.
        # (Never opened, the last row stays invited.)
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": joined["invitation_id"]}
        )
        await self.h.open(STUDENT, revoked["invitation_id"])
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": revoked["invitation_id"]}
        )
        await self.h.open(STUDENT, left["invitation_id"])
        await self.h.store.release_on_leave(ROOM, STUDENT, 1)
        await self.h.add(INVITED)  # re-invite resets the first row
        await self.h.open(STUDENT, joined["invitation_id"])
        # Another user's claim in another course.
        self.h.verify(OTHER, "other@gmail.example")
        await self.h.open(OTHER, elsewhere["invitation_id"])
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
        await self.h.open(OTHER, inv["invitation_id"])
        for name in ("list", "pending_approvals", "live", "events"):
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

    async def test_re_invite_resets_a_left_row_to_invited_and_deletes_old_requests(
        self,
    ):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.open(OTHER, ident)
        await self.h.open(STUDENT, ident)
        await self.h.store.release_on_leave(ROOM, STUDENT, 1)
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
        self.assertIsNone(await self.h.store.get_request(ident, OTHER))
        self.assertEqual(await self.h.pending(), [])
        # The re-invited row is claimed again by the matching address.
        await self.h.claims.claim_matching_for(STUDENT)
        self.assertEqual((await self.h.row(ident))["claimant"], STUDENT)

    async def test_re_invite_is_a_fresh_invitation_from_the_new_inviter(self):
        coteacher = "@coteacher:x"
        self.h.admins.admins.add((ROOM, coteacher))
        self.h.main.membership[(ROOM, STUDENT)] = "join"
        self.h.verify(STUDENT, INVITED_KEY)
        # First invited by the teacher through "invite to a seat" (no
        # address as entered), with a Canvas identity, then claimed and left.
        status, made = await self.h.handlers.invite_member(
            TEACHER, {"room_id": ROOM, "user_id": STUDENT}
        )
        self.assertEqual(status, 200, made)
        ident = made["invitation_id"]
        await self.h.main.db_pool.runInteraction(
            "canvas",
            lambda txn: txn.execute(
                "UPDATE pangea_student_invitation SET lti_issuer = 'iss',"
                " lti_context_id = 'ctx', lti_user_id = 'sub', created_at_ms = 1"
                " WHERE id = ?",
                (ident,),
            ),
        )
        await self.h.open(STUDENT, ident)
        await self.h.store.release_on_leave(ROOM, STUDENT, 1)
        # Re-added by the co-teacher, typed by hand, from a CSV.
        status, body = await self.h.handlers.add(
            coteacher, {"room_id": ROOM, "emails": [INVITED], "source": "csv"}
        )
        self.assertEqual(status, 200, body)
        (again,) = body["invitations"]
        self.assertEqual(again["invitation_id"], ident)
        self.assertEqual(
            (again["state"], again["invited_by"], again["source"], again["email"]),
            ("invited", coteacher, "csv", INVITED),
        )
        self.assertFalse(again["canvas_identity"])
        self.assertGreater(again["created_at_ms"], 1)
        row = await self.h.row(ident)
        self.assertEqual(
            (row["lti_issuer"], row["lti_context_id"], row["lti_user_id"]),
            (None, None, None),
        )
        # The new claim's joined invitation names the new inviter.
        status, claimed = await self.h.open(STUDENT, ident)
        self.assertEqual(claimed["result"], "claimed")
        self.assertIsNotNone(await self.h.store.managed_record(STUDENT, ROOM))
        _, joined = await self.h.handlers.mine_joined(STUDENT)
        self.assertEqual(joined["invitations"][0]["invited_by"], coteacher)

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
        await self.h.open(OTHER, ident)
        await self.h.handlers.revoke(TEACHER, {"room_id": ROOM, "invitation_id": ident})
        (again,) = await self.h.add(INVITED)
        self.assertEqual((again["state"], again["send_count"]), ("invited", 1))
        self.assertIsNone(await self.h.store.get_request(ident, OTHER))

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
        # Idempotent.
        status, again = await self.h.handlers.invite_member(
            TEACHER, {"room_id": ROOM, "user_id": STUDENT}
        )
        self.assertEqual(again, body)
        # Once claimed, the joined invitation is returned.
        await self.h.open(STUDENT, body["invitation_id"])
        status, joined = await self.h.handlers.invite_member(
            TEACHER, {"room_id": ROOM, "user_id": STUDENT}
        )
        self.assertEqual(
            joined, {"invitation_id": body["invitation_id"], "state": "joined"}
        )

    async def test_list_names_the_member_a_member_invitation_was_made_for_and_no_other_row(
        self,
    ):
        """D2: T3 carries `user_id` on `source=member` rows (never the
        address), so the roster can join the row to its course member. No
        other row kind gains the field."""
        self.h.main.membership[(ROOM, STUDENT)] = "join"
        self.h.verify(STUDENT, "member@school.example")
        (manual,) = await self.h.add("manual@school.example")
        status, made = await self.h.handlers.invite_member(
            TEACHER, {"room_id": ROOM, "user_id": STUDENT}
        )
        self.assertEqual(status, 200, made)
        listed = await self.h.listing()
        member_row = listed[made["invitation_id"]]
        self.assertEqual(member_row["user_id"], STUDENT)
        self.assertIsNone(member_row["email"])
        self.assertNotIn("school", str(member_row))
        self.assertNotIn("user_id", listed[manual["invitation_id"]])
        # The member it was made for, even after the address moves to
        # another course member, or the member leaves.
        self.h.main.threepids.pop(STUDENT)
        self.h.main.membership[(ROOM, OTHER)] = "join"
        self.h.verify(OTHER, "member@school.example")
        self.h.main.membership[(ROOM, STUDENT)] = "leave"
        self.assertEqual(
            (await self.h.listing())[made["invitation_id"]]["user_id"], STUDENT
        )
        # A manual add of a revoked member row's address makes it a manual row
        # again: it no longer names a member.
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": made["invitation_id"]}
        )
        (readded,) = await self.h.add("member@school.example")
        self.assertEqual(readded["invitation_id"], made["invitation_id"])
        self.assertNotIn("user_id", readded)
        self.assertIsNone((await self.h.row(made["invitation_id"]))["member_user_id"])

    async def test_an_existing_table_gains_the_member_column(self):
        """A database created before `member_user_id` existed is migrated
        in place, and its rows keep their data."""
        pool = self.h.main.db_pool
        pool.connection.execute(
            "CREATE TABLE pangea_student_invitation (id TEXT PRIMARY KEY,"
            " course_room_id TEXT NOT NULL, email_key TEXT NOT NULL, email TEXT,"
            " state TEXT NOT NULL, source TEXT NOT NULL, invited_by TEXT NOT NULL,"
            " claimant TEXT, send_count BIGINT NOT NULL DEFAULT 0,"
            " last_sent_at_ms BIGINT, created_at_ms BIGINT NOT NULL,"
            " joined_at_ms BIGINT, lti_issuer TEXT, lti_context_id TEXT,"
            " lti_user_id TEXT, UNIQUE (course_room_id, email_key))"
        )
        pool.connection.execute(
            "INSERT INTO pangea_student_invitation (id, course_room_id, email_key,"
            " state, source, invited_by, created_at_ms)"
            " VALUES ('old', ?, 'old@school.example', 'invited', 'manual', ?, 1)",
            (ROOM, TEACHER),
        )
        pool.connection.commit()
        row = await self.h.row("old")
        self.assertIsNone(row["member_user_id"])
        self.assertEqual(row["email_key"], "old@school.example")
        # Running the migration again is harmless.
        store = StudentInvitationStore(self.h.api._hs)
        await store.ensure()
        self.assertEqual((await store.get("old"))["email_key"], "old@school.example")

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
        await self.h.open(STUDENT, inv["invitation_id"])
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
        await self.h.open(STUDENT, joined["invitation_id"])
        self.h.verify(OTHER, "other@gmail.example")
        await self.h.open(OTHER, left["invitation_id"])
        await self.h.store.release_on_leave(ROOM, OTHER, 1)
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

    async def test_a_requested_invitation_is_claimed_when_the_invited_email_is_later_added_and_verified(
        self,
    ):
        (inv,) = await self.h.add(INVITED)
        ident = inv["invitation_id"]
        status, body = await self.h.open(STUDENT, ident)
        self.assertEqual(body["result"], "pending")
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
        await self.h.open(OTHER, ident)
        await self.h.open(STUDENT, ident)
        self.assertEqual(len(await self.h.pending()), 2)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.hook.on_add_user_third_party_identifier(
            STUDENT, "email", INVITED_KEY
        )
        record = await self.h.store.managed_record(STUDENT, ROOM)
        self.assertEqual(record["course_room_id"], ROOM)
        self.assertEqual(await self.h.pending(), [])
        self.assertEqual((await self.h.listing())[ident]["pending_count"], 0)

    async def test_a_matching_invitation_is_claimed_at_sign_in_with_no_step(self):
        """Seats amendment 2026-10-10: no prompt, no checkbox."""
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.hook.on_user_login(STUDENT, None, None)
        row = await self.h.row(inv["invitation_id"])
        self.assertEqual((row["state"], row["claimant"]), ("joined", STUDENT))
        self.assertIsNotNone(await self.h.store.managed_record(STUDENT, ROOM))
        self.assertIsNone(await self.h.store.get_request(inv["invitation_id"], STUDENT))

    async def test_sign_in_claims_the_oldest_matching_row_per_course_and_marks_the_rest(
        self,
    ):
        (oldest,) = await self.h.add(INVITED)
        (newer,) = await self.h.add("second@school.example")
        (elsewhere,) = await self.h.add("second@school.example", room=OTHER_ROOM)
        # Distinct creation times, so "oldest" is the rule under test and not
        # a tie broken by id.
        for ident, created in ((oldest, 1000), (newer, 2000), (elsewhere, 3000)):
            await self.h.main.db_pool.runInteraction(
                "created",
                lambda txn, i=ident["invitation_id"], t=created: txn.execute(
                    "UPDATE pangea_student_invitation SET created_at_ms = ? WHERE id = ?",
                    (t, i),
                ),
            )
        self.h.verify(STUDENT, "second@school.example", 1)
        self.h.verify(STUDENT, INVITED_KEY, 2)
        await self.hook.on_user_login(STUDENT, None, None)
        self.assertEqual(
            (await self.h.row(oldest["invitation_id"]))["claimant"], STUDENT
        )
        self.assertEqual((await self.h.row(newer["invitation_id"]))["state"], "invited")
        # One claim per course: the other course's row is claimed too.
        self.assertEqual(
            (await self.h.row(elsewhere["invitation_id"]))["claimant"], STUDENT
        )
        listed = await self.h.listing()
        self.assertEqual(listed[newer["invitation_id"]]["same_student_as"], STUDENT)
        self.assertIsNone(listed[oldest["invitation_id"]]["same_student_as"])
        # A row is never claimed twice: signing in again changes nothing.
        await self.hook.on_user_login(STUDENT, None, None)
        self.assertEqual((await self.h.row(newer["invitation_id"]))["state"], "invited")

    async def test_claim_never_fails_login(self):
        (inv,) = await self.h.add(INVITED)
        await self.h.open(STUDENT, inv["invitation_id"])
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
        release = MembershipRelease(self.h.store, self.h.claims)
        for membership, sender in (
            ("leave", STUDENT),
            ("leave", TEACHER),
            ("ban", TEACHER),
        ):
            (inv,) = await self.h.add(INVITED)
            self.h.main.threepids[STUDENT] = [(INVITED_KEY, 1)]
            status, body = await self.h.open(STUDENT, inv["invitation_id"])
            self.assertEqual(body["result"], "claimed", membership)
            await release.on_new_event(
                _member_event(ROOM, STUDENT, membership, sender), {}
            )
            row = await self.h.row(inv["invitation_id"])
            self.assertEqual(row["state"], "left", (membership, sender))
            self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))

    async def test_other_events_release_nothing(self):
        release = MembershipRelease(self.h.store, self.h.claims)
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.open(STUDENT, inv["invitation_id"])
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
        release = MembershipRelease(self.h.store, self.h.claims)
        self.h.main.db_pool.error = RuntimeError("db down")
        with patch.object(report, "sentry_sdk") as sentry:
            await release.on_new_event(
                _member_event(ROOM, STUDENT, "leave", STUDENT), {}
            )
        self.h.main.db_pool.error = None
        sentry.capture_message.assert_called_once()

    async def test_class_code_rejoin_restores_nothing(self):
        release = MembershipRelease(self.h.store, self.h.claims)
        (inv,) = await self.h.add(INVITED)
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.open(STUDENT, inv["invitation_id"])
        await release.on_new_event(_member_event(ROOM, STUDENT, "leave", STUDENT), {})
        await release.on_new_event(_member_event(ROOM, STUDENT, "join", STUDENT), {})
        await self.h.claims.claim_matching_for(STUDENT)
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
        await self.h.open(STUDENT, inv["invitation_id"])
        self.assertEqual((await self.h.row(inv["invitation_id"]))["state"], "left")
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))


def _power_event(room_id: str) -> Any:
    return SimpleNamespace(
        type="m.room.power_levels",
        room_id=room_id,
        state_key="",
        sender=TEACHER,
        content={"users": {}},
        is_state=lambda: True,
    )


class TestManagedRule(_Base):
    """CONTRACTS C2.5 / SPEC INV-10: a joined invitation has a managed record
    exactly while its claimant is not a course admin of that course."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.release = MembershipRelease(self.h.store, self.h.claims)

    async def claimed(self, user: str = STUDENT) -> str:
        (inv,) = await self.h.add(INVITED)
        self.h.verify(user, INVITED_KEY)
        status, body = await self.h.open(user, inv["invitation_id"])
        self.assertEqual(body["result"], "claimed")
        return inv["invitation_id"]

    async def test_admins_own_claim_records_no_managed_account(self):
        (inv,) = await self.h.add(INVITED)
        await self.h.open(OTHER, inv["invitation_id"])
        self.h.admins.admins.add((ROOM, OTHER))
        status, body = await self.h.handlers.decide(
            TEACHER,
            {
                "room_id": ROOM,
                "invitation_id": inv["invitation_id"],
                "user_id": OTHER,
                "decision": "grant",
            },
        )
        self.assertEqual((status, body["invitation"]["state"]), (200, "joined"))
        self.assertIsNone(await self.h.store.managed_record(OTHER, ROOM))

    async def test_claimant_losing_pl100_gets_a_managed_record(self):
        """Rebuilt from the joined invitation alone: no request, no disclosure
        (seats amendment 2026-10-10)."""
        self.h.admins.admins.add((ROOM, STUDENT))
        ident = await self.claimed()
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))
        self.assertIsNone(await self.h.store.get_request(ident, STUDENT))
        self.h.admins.admins.discard((ROOM, STUDENT))
        await self.release.on_new_event(_power_event(ROOM), {})
        record = await self.h.store.managed_record(STUDENT, ROOM)
        self.assertEqual((record["user_id"], record["course_room_id"]), (STUDENT, ROOM))
        # Idempotent.
        await self.release.on_new_event(_power_event(ROOM), {})
        self.assertEqual(await self.h.store.managed_record(STUDENT, ROOM), record)

    async def test_claimant_gaining_pl100_loses_the_managed_record(self):
        await self.claimed()
        self.assertIsNotNone(await self.h.store.managed_record(STUDENT, ROOM))
        self.h.admins.admins.add((ROOM, STUDENT))
        await self.release.on_new_event(_power_event(ROOM), {})
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))
        await self.release.on_new_event(_power_event(ROOM), {})
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))
        # The invitation stays joined; only management changes.
        _, joined = await self.h.handlers.mine_joined(STUDENT)
        self.assertEqual(len(joined["invitations"]), 1)
        # A power-level change in another course touches nothing here.
        self.h.admins.admins.discard((ROOM, STUDENT))
        await self.release.on_new_event(_power_event(OTHER_ROOM), {})
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))

    async def test_login_repairs_a_missed_power_level_change(self):
        from synapse_pangea_chat.email_invite.claim_by_email import ClaimByEmail

        legacy = MagicMock()
        legacy.prepared_for_emails = _async_return([])
        api = MagicMock()
        api._hs.get_datastores.return_value.main = self.h.main
        hook = ClaimByEmail(api, legacy, MagicMock(), student_claims=self.h.claims)
        await self.claimed()
        # Promoted with no event seen: the next sign-in deletes the record.
        self.h.admins.admins.add((ROOM, STUDENT))
        await hook.on_user_login(STUDENT, None, None)
        self.assertIsNone(await self.h.store.managed_record(STUDENT, ROOM))
        # Demoted with no event seen: the next sign-in records it again.
        self.h.admins.admins.discard((ROOM, STUDENT))
        await hook.on_user_login(STUDENT, None, None)
        self.assertIsNotNone(await self.h.store.managed_record(STUDENT, ROOM))

    async def test_power_level_change_during_a_claim_is_applied(self):
        for promoted_mid_claim in (True, False):
            h = Harness()
            # The student's answers in order: before the claim transaction,
            # then every read after it.
            answers = [not promoted_mid_claim]

            async def changing(room_id: str, user_id: str) -> bool:
                if user_id != STUDENT:
                    return (room_id, user_id) in h.admins.admins
                return answers.pop(0) if answers else promoted_mid_claim

            h.admins.is_course_admin = changing  # type: ignore[method-assign]
            (inv,) = await h.add(INVITED)
            h.verify(STUDENT, INVITED_KEY)
            status, body = await h.open(STUDENT, inv["invitation_id"])
            self.assertEqual(body["result"], "claimed")
            record = await h.store.managed_record(STUDENT, ROOM)
            if promoted_mid_claim:
                self.assertIsNone(record, "promoted during the claim")
            else:
                self.assertIsNotNone(record, "demoted during the claim")

    async def test_power_level_callback_failure_never_fails_the_event(self):
        await self.claimed()
        captured = self.capture_logs()
        with patch.object(report, "sentry_sdk") as sentry:
            self.h.main.db_pool.error = RuntimeError("DETAIL: " + INVITED_KEY)
            await self.release.on_new_event(_power_event(ROOM), {})
            self.h.main.db_pool.error = None
            # A failing admin check for one claimant is reported, not raised.
            failing = self.h.admins.is_course_admin

            async def broken(room_id: str, user_id: str) -> bool:
                raise RuntimeError("state read failed for " + INVITED_KEY)

            self.h.admins.is_course_admin = broken  # type: ignore[method-assign]
            await self.release.on_new_event(_power_event(ROOM), {})
            self.h.admins.is_course_admin = failing  # type: ignore[method-assign]
        self.assertEqual(sentry.capture_message.call_count, 2)
        self.assertNotIn(INVITED_KEY, str(sentry.mock_calls))
        self.assertNotIn(INVITED_KEY, captured.text())
        self.assertIsNotNone(await self.h.store.managed_record(STUDENT, ROOM))


class TestRouteTree(unittest.TestCase):
    def test_fixed_routes_survive_the_open_root_in_synapses_resource_tree(self):
        """The open root sits at the fixed routes' parent path. Synapse's
        tree builder moves the fixed routes under it, whichever is registered
        first, and Twisted serves a fixed child before the root's getChild."""
        from synapse.util.httpresourcetree import create_resource_tree
        from twisted.web.resource import Resource, getChildForRequest

        for root_first in (True, False):
            add_route, events_route, joined_route, open_route = (
                Resource(),
                Resource(),
                Resource(),
                MagicMock(spec=InvitationOpenRoute),
            )
            for leaf in (add_route, events_route, joined_route):
                leaf.isLeaf = True
            open_route.isLeaf = True
            fixed = {
                "/p/student_invitations/add": add_route,
                "/p/student_invitations/events": events_route,
                "/p/student_invitations/mine/joined": joined_route,
            }
            tree: Dict[str, Any] = {}
            root = {"/p/student_invitations": StudentInvitationsRoot(open_route)}
            for part in (root, fixed) if root_first else (fixed, root):
                tree.update(part)
            top = create_resource_tree(tree, Resource())

            def resolve(path: bytes) -> Any:
                request = MagicMock()
                request.postpath = path.split(b"/")[1:]
                request.prepath = []
                return getChildForRequest(top, request)

            self.assertIs(resolve(b"/p/student_invitations/add"), add_route)
            self.assertIs(resolve(b"/p/student_invitations/events"), events_route)
            self.assertIs(resolve(b"/p/student_invitations/mine/joined"), joined_route)
            self.assertIs(resolve(b"/p/student_invitations/inv-1/open"), open_route)


class TestEvents(_Base):
    """The activity ledger (seats amendment 2026-10-10, §2): append-only,
    written in the same transaction as each change, no email, actor only to
    course admins, never logged."""

    async def test_every_invitation_action_writes_its_event_with_its_actor(self):
        (a, b, c) = await self.h.add(
            INVITED, "second@school.example", "third@school.example"
        )
        send = {
            "room_id": ROOM,
            "items": [{"invitation_id": a["invitation_id"], "expected_send_count": 0}],
        }
        await self.h.handlers.send(TEACHER, send)
        send["items"][0]["expected_send_count"] = 1
        await self.h.handlers.send(TEACHER, send)
        await self.h.open(OTHER, b["invitation_id"])
        await self.h.handlers.decide(
            TEACHER,
            {
                "room_id": ROOM,
                "invitation_id": b["invitation_id"],
                "user_id": OTHER,
                "decision": "deny",
            },
        )
        await self.h.handlers.decide(
            TEACHER,
            {
                "room_id": ROOM,
                "invitation_id": b["invitation_id"],
                "user_id": OTHER,
                "decision": "grant",
            },
        )
        self.h.verify(STUDENT, INVITED_KEY)
        await self.h.open(STUDENT, a["invitation_id"])
        await self.h.store.release_on_leave(ROOM, STUDENT, now_ms())
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": c["invitation_id"]}
        )
        events = list(reversed(await self.h.events()))
        self.assertEqual(
            [(e["action"], e["actor"], e["target"], e["count"]) for e in events],
            [
                ("students_added", TEACHER, None, 3),
                ("invite_sent", TEACHER, a["invitation_id"], None),
                ("invite_resent", TEACHER, a["invitation_id"], None),
                ("request_made", "system", b["invitation_id"], None),
                ("request_denied", TEACHER, b["invitation_id"], None),
                ("request_granted", TEACHER, b["invitation_id"], None),
                ("invitation_claimed", "system", b["invitation_id"], None),
                ("invitation_claimed", "system", a["invitation_id"], None),
                ("student_left", "system", a["invitation_id"], None),
                ("invitation_withdrawn", TEACHER, c["invitation_id"], None),
            ],
        )
        self.assertEqual(
            set(events[0]),
            {"event_id", "time_ms", "actor", "action", "target", "count"},
        )
        self.assertNotIn("school", str(events))
        # Repeats that change nothing write nothing.
        before = len(await self.h.events())
        await self.h.add("second@school.example", "third@school.example")
        # (a live row, unchanged, and the revoked row, reset: one event, count 1)
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": c["invitation_id"]}
        )
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": c["invitation_id"]}
        )
        actions = [e["action"] for e in await self.h.events()][
            : len(await self.h.events()) - before
        ]
        self.assertEqual(actions, ["invitation_withdrawn", "students_added"])
        latest = (await self.h.events())[1]
        self.assertEqual((latest["count"], latest["target"]), (1, c["invitation_id"]))

    async def test_an_event_is_written_in_the_same_transaction_as_its_change(self):
        (inv,) = await self.h.add(INVITED)
        before = await self.h.events()

        def refuse_the_event(sql: str, args: Any) -> None:
            if sql.startswith("INSERT INTO pangea_student_invitation_event"):
                raise RuntimeError("event write refused")

        self.h.main.db_pool.on_statement = refuse_the_event
        with self.assertRaises(RuntimeError):
            await self.h.store.revoke(ROOM, inv["invitation_id"], TEACHER, 5)
        with self.assertRaises(RuntimeError):
            await self.h.store.record_request(inv["invitation_id"], OTHER, 5)
        self.h.main.db_pool.on_statement = None
        # Neither change landed without its event.
        self.assertEqual((await self.h.row(inv["invitation_id"]))["state"], "invited")
        self.assertIsNone(await self.h.store.get_request(inv["invitation_id"], OTHER))
        self.assertEqual(await self.h.events(), before)

    async def test_a_send_that_failed_removes_its_event_with_the_count(self):
        (inv,) = await self.h.add(INVITED)
        self.h.mailer.fail_with = RuntimeError("mail down")
        await self.h.handlers.send(
            TEACHER,
            {
                "room_id": ROOM,
                "items": [
                    {"invitation_id": inv["invitation_id"], "expected_send_count": 0}
                ],
            },
        )
        self.assertEqual((await self.h.row(inv["invitation_id"]))["send_count"], 0)
        self.assertEqual(
            [e["action"] for e in await self.h.events()], ["students_added"]
        )

    async def test_canvas_and_member_events(self):
        self.h.main.membership[(ROOM, STUDENT)] = "join"
        self.h.verify(STUDENT, "member@school.example")
        _, made = await self.h.handlers.invite_member(
            TEACHER, {"room_id": ROOM, "user_id": STUDENT}
        )
        await self.h.store.import_canvas(
            ROOM,
            "https://canvas.example",
            "ctx",
            [("u1", "a@school.example", "a@school.example")],
            TEACHER,
            now_ms(),
            lambda: "canvas-row",
        )
        events = await self.h.events()
        self.assertEqual(
            [(e["action"], e["actor"], e["target"], e["count"]) for e in events],
            [
                ("canvas_roster_imported", TEACHER, None, 1),
                ("students_added", TEACHER, made["invitation_id"], 1),
            ],
        )

    async def test_events_read_is_admin_gated_filtered_and_paged(self):
        rows = await self.h.add(*[f"s{i}@school.example" for i in range(3)])
        for i, row in enumerate(rows):
            await self.h.store.revoke(ROOM, row["invitation_id"], TEACHER, 1000 + i)
        (elsewhere,) = await self.h.add(INVITED, room=OTHER_ROOM)
        status, body = await self.h.handlers.events(STUDENT, {"room_id": ROOM})
        self.assertEqual((status, body["errcode"]), (403, "M_FORBIDDEN"))
        withdrawn = await self.h.events(action="invitation_withdrawn")
        self.assertEqual([e["time_ms"] for e in withdrawn], [1002, 1001, 1000])
        self.assertEqual(
            [
                e["time_ms"]
                for e in await self.h.events(
                    action="invitation_withdrawn", **{"from": "1001", "to": "1002"}
                )
            ],
            [1001],
        )
        # Another course's events never show.
        self.assertNotIn(elsewhere["invitation_id"], str(await self.h.events()))
        for bad in (
            {"from": "x"},
            {"to": "-1"},
            {"action": "nope"},
            {"cursor": "abc"},
            {"cursor": "1."},
        ):
            status, body = await self.h.handlers.events(
                TEACHER, {"room_id": ROOM, **bad}
            )
            self.assertEqual((status, body["errcode"]), (400, "M_INVALID_PARAM"), bad)
        # Paging: newest first, a cursor until the last page.
        with patch("synapse_pangea_chat.student_invitations.store.EVENTS_PAGE", 2):
            status, first = await self.h.handlers.events(
                TEACHER, {"room_id": ROOM, "action": "invitation_withdrawn"}
            )
            self.assertEqual([e["time_ms"] for e in first["events"]], [1002, 1001])
            self.assertIsNotNone(first["next_cursor"])
            status, second = await self.h.handlers.events(
                TEACHER,
                {
                    "room_id": ROOM,
                    "action": "invitation_withdrawn",
                    "cursor": first["next_cursor"],
                },
            )
            self.assertEqual([e["time_ms"] for e in second["events"]], [1000])
            self.assertIsNone(second["next_cursor"])
            # Stable for a fixed `to`: an event arriving between two page
            # reads moves no row between pages.
            fixed = {"room_id": ROOM, "to": str(now_ms() + 1)}
            status, page1 = await self.h.handlers.events(TEACHER, fixed)
            # A new event is written at the time it happens, at or after `to`.
            await asyncio.sleep(0.002)
            later = await self.h.add("late@school.example")
            status, page2 = await self.h.handlers.events(
                TEACHER, {**fixed, "cursor": page1["next_cursor"]}
            )
            status, again1 = await self.h.handlers.events(TEACHER, fixed)
            self.assertEqual(
                [e["event_id"] for e in again1["events"]],
                [e["event_id"] for e in page1["events"]],
            )
            seen = [e["event_id"] for e in page1["events"] + page2["events"]]
            self.assertEqual(len(seen), len(set(seen)))
            self.assertNotIn(later[0]["invitation_id"], str(page2["events"]))

    async def test_the_actor_is_never_logged(self):
        captured = self.capture_logs()
        (inv,) = await self.h.add(INVITED)
        await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": inv["invitation_id"]}
        )
        await self.h.events()
        self.assertTrue(captured.text())
        self.assertNotIn(TEACHER, captured.text())


class TestMigration(_Base):
    async def test_tables_of_earlier_builds_are_brought_to_the_current_columns(self):
        """In place and idempotent: requests lose disclosure_version and
        rename acked_at_ms; the managed record keeps only created_at."""
        pool = self.h.main.db_pool
        for sql in (
            "CREATE TABLE pangea_invitation_ack (invitation_id TEXT NOT NULL,"
            " user_id TEXT NOT NULL, acked_at_ms BIGINT NOT NULL,"
            " disclosure_version INTEGER NOT NULL, decision TEXT,"
            " PRIMARY KEY (invitation_id, user_id))",
            "INSERT INTO pangea_invitation_ack VALUES ('i', 'u', 5, 2, 'denied')",
            "CREATE TABLE pangea_managed_account (user_id TEXT NOT NULL,"
            " course_room_id TEXT NOT NULL, invited_by TEXT NOT NULL,"
            " since_ms BIGINT NOT NULL, PRIMARY KEY (user_id, course_room_id))",
            "INSERT INTO pangea_managed_account VALUES ('u', '!r:x', '@t:x', 7)",
        ):
            pool.connection.execute(sql)
        pool.connection.commit()
        for _ in range(2):
            store = StudentInvitationStore(self.h.api._hs)
            self.assertEqual(
                await store.get_request("i", "u"),
                {
                    "invitation_id": "i",
                    "user_id": "u",
                    "requested_at_ms": 5,
                    "decision": "denied",
                },
            )
            self.assertEqual(
                await store.managed_record("u", "!r:x"),
                {"user_id": "u", "course_room_id": "!r:x", "created_at_ms": 7},
            )

        def columns(table: str) -> set:
            rows = pool.connection.execute(
                "SELECT name FROM pragma_table_info(?)", (table,)
            )
            return {row[0] for row in rows}

        self.assertEqual(
            columns("pangea_managed_account"),
            {"user_id", "course_room_id", "created_at_ms"},
        )
        self.assertEqual(
            columns("pangea_invitation_ack"),
            {"invitation_id", "user_id", "requested_at_ms", "decision"},
        )
        # The migrated tables take new rows.
        (inv,) = await self.h.add(INVITED)
        status, body = await self.h.open(OTHER, inv["invitation_id"])
        self.assertEqual((status, body["result"]), (200, "pending"))
        self.h.verify(STUDENT, INVITED_KEY)
        status, body = await self.h.open(STUDENT, inv["invitation_id"])
        self.assertEqual(body["result"], "claimed")


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
            await self.h.open(OTHER, a["invitation_id"])
            await self.h.pending()
            await self.h.handlers.approve_all(TEACHER, {"room_id": ROOM})
            await self.h.open(STUDENT, b["invitation_id"])
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
                "open", lambda: self.h.open(STUDENT, a["invitation_id"])
            )
            self.assertEqual(status, 500)
            # The open reached the database, whose error carried the address.
            self.assertIn("open failed (RuntimeError)", captured.text())
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
