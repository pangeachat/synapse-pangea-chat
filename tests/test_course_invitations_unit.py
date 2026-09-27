"""Persistence contracts across retries and reconstructed module instances."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.api.errors import SynapseError
from twisted.mail.smtp import SMTPDeliveryError

from synapse_pangea_chat.email_invite.course_claims import CourseClaimStore
from synapse_pangea_chat.email_invite.course_invitation_api import CourseInvitationAPI
from tests.moderation_doubles import DbPoolDouble


class TestInvitations(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hs = MagicMock()
        self.pool = DbPoolDouble()
        self.hs.get_datastores.return_value.main.db_pool = self.pool
        self.claims = CourseClaimStore(self.hs)
        await self.claims._ensure_table()
        self.store = self.claims.invitations
        self.spec = {
            "title": "Course",
            "course_plan_id": "quest",
            "target_language": "es",
        }
        self.ident, _ = await self.store.prepare(
            "@operator:x",
            "request-1",
            self.spec,
            "teacher@school.example",
            "abc2def",
            1,
        )

    async def test_preparation_is_idempotent_and_operator_scoped(self):
        ident, created = await self.store.prepare(
            "@operator:x",
            "request-1",
            self.spec,
            "teacher@school.example",
            "ghi3jkm",
            2,
        )
        self.assertEqual(ident, self.ident)
        self.assertFalse(created)
        self.assertFalse(await self.store.code_in_use("ghi3jkm"))
        with self.assertRaises(SynapseError) as error:
            await self.store.prepare(
                "@operator:x",
                "request-1",
                {**self.spec, "title": "Other"},
                "teacher@school.example",
                "ghi3jkm",
                2,
            )
        self.assertEqual(error.exception.code, 409)
        other, created = await self.store.prepare(
            "@other:x", "request-1", self.spec, "teacher@school.example", "ghi3jkm", 2
        )
        self.assertNotEqual(other, ident)
        self.assertTrue(created)

    async def test_reserved_claim_never_transfers_or_recreates_after_restart(self):
        row, create = await self.store.reserve_creation(self.ident, "@teacher:x")
        self.assertTrue(create)
        restarted = CourseClaimStore(self.hs).invitations
        row, create = await restarted.reserve_creation(self.ident, "@teacher:x")
        self.assertFalse(create)
        self.assertEqual(row["status"], "provisioning")
        with self.assertRaises(SynapseError) as error:
            await restarted.reserve_creation(self.ident, "@other:x")
        self.assertEqual(error.exception.code, 404)

    async def test_revocation_wins_before_and_during_provisioning(self):
        await self.store.reserve_creation(self.ident, "@teacher:x")
        await self.store.revoke(self.ident)
        for operation in (
            self.store.reserve_creation(self.ident, "@teacher:x"),
            self.store.associate(self.ident, "@teacher:x", "!room:x"),
            self.store.complete(self.ident, "@teacher:x", "!room:x", 10),
            self.store.begin_delivery(self.ident, "ghi3jkm", 10),
        ):
            with self.assertRaises(SynapseError):
                await operation
        self.assertEqual((await self.store.status(self.ident))["status"], "revoked")

    async def test_completion_atomically_owes_one_share_kit(self):
        await self.store.reserve_creation(self.ident, "@teacher:x")
        await self.store.associate(self.ident, "@teacher:x", "!room:x")
        for _ in range(2):
            await self.store.complete(self.ident, "@teacher:x", "!room:x", 10)
        self.assertEqual(
            await self.claims.outstanding_notices(11), [("!room:x", "@teacher:x")]
        )
        self.assertIsNone((await self.store.get(self.ident))["requested_email"])
        reservation = await self.claims.reserve_notice("!room:x", "@teacher:x", 12, 100)
        self.assertEqual(reservation.requested_email, "teacher@school.example")
        self.assertIsNone(
            await self.claims.reserve_notice("!room:x", "@teacher:x", 13, 100)
        )
        await self.claims.mark_notice_sent("!room:x", "@teacher:x", 14)
        self.assertEqual(await self.claims.outstanding_notices(200), [])
        self.assertIsNotNone(await self.store.for_code("ABC2DEF"))
        self.assertTrue(await self.claims.code_in_use("abc2def"))

    async def test_delivery_audit_is_private_and_ambiguous_attempt_survives_restart(
        self,
    ):
        status = await self.store.status(self.ident)
        self.assertEqual(status["delivery_outcome"], "unsent")
        attempt = await self.store.begin_delivery(self.ident, "ghi3jkm", 3)
        restarted = CourseClaimStore(self.hs).invitations
        status = await restarted.status(self.ident)
        self.assertEqual(status["delivery_outcome"], "uncertain")
        self.assertNotIn("teacher@school.example", str(status))
        self.assertNotIn("ghi3jkm", str(status))
        await restarted.finish_delivery(attempt, "failed", 4)
        self.assertEqual(
            (await restarted.status(self.ident))["delivery_outcome"], "failed"
        )
        await restarted.finish_delivery(attempt, "accepted", 5)
        self.assertEqual(
            (await restarted.status(self.ident))["delivery_outcome"], "accepted"
        )
        self.assertIsNotNone(await restarted.for_code("abc2def"))
        self.assertIsNotNone(await restarted.for_code("ghi3jkm"))

    async def test_partial_and_completed_claims_refuse_reminders(self):
        await self.store.reserve_creation(self.ident, "@teacher:x")
        with self.assertRaises(SynapseError):
            await self.store.begin_delivery(self.ident, "ghi3jkm", 3)
        await self.store.associate(self.ident, "@teacher:x", "!room:x")
        await self.store.complete(self.ident, "@teacher:x", "!room:x", 4)
        with self.assertRaises(SynapseError):
            await self.store.begin_delivery(self.ident, "ghi3jkm", 5)

    async def test_mail_failure_leaves_live_invitation_and_explicit_uncertainty(self):
        api = MagicMock()
        api._hs = self.hs
        self.hs.get_clock.return_value.time_msec.return_value = 2
        mailer = MagicMock()
        mailer.send_course_ready = AsyncMock(side_effect=TimeoutError())
        config = MagicMock(app_base_url="https://app.example.test")
        resource = CourseInvitationAPI(
            api, config, self.claims, self.store, mailer, "prepare"
        )
        await resource.send(
            self.ident,
            "abc2def",
            "teacher@school.example",
            spec={**self.spec, "description": "", "request_summary": ""},
        )
        status = await self.store.status(self.ident)
        self.assertEqual(status["status"], "prepared")
        self.assertEqual(status["delivery_outcome"], "uncertain")
        self.assertIsNotNone(await self.store.for_code("abc2def"))

    async def test_explicit_smtp_rejection_is_failed_without_capturing_address(self):
        api = MagicMock()
        api._hs = self.hs
        self.hs.get_clock.return_value.time_msec.return_value = 2
        mailer = MagicMock()
        mailer.send_course_ready = AsyncMock(
            side_effect=SMTPDeliveryError(550, "rejected teacher@school.example")
        )
        resource = CourseInvitationAPI(
            api,
            MagicMock(app_base_url="https://app.example.test"),
            self.claims,
            self.store,
            mailer,
            "prepare",
        )
        with patch(
            "synapse_pangea_chat.email_invite.course_invitation_api._capture_exception"
        ) as capture:
            await resource.send(
                self.ident,
                "abc2def",
                "teacher@school.example",
                spec={**self.spec, "description": "", "request_summary": ""},
            )
        self.assertEqual(
            (await self.store.status(self.ident))["delivery_outcome"], "failed"
        )
        self.assertNotIn("teacher@school.example", str(capture.call_args))
        self.assertIsNotNone(await self.store.for_code("abc2def"))
