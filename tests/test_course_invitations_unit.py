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


class TestCoursePlanReplacement(unittest.IsolatedAsyncioTestCase):
    """create-course-space.instructions.md, "Replacing the course plan"."""

    NEW = {"course_plan_id": "quest-2", "title": "Tailored", "description": "Text"}

    async def asyncSetUp(self):
        self.hs = MagicMock()
        self.hs.get_clock.return_value.time_msec.return_value = 7
        self.pool = DbPoolDouble()
        self.hs.get_datastores.return_value.main.db_pool = self.pool
        self.claims = CourseClaimStore(self.hs)
        await self.claims._ensure_table()
        self.store = self.claims.invitations
        self.spec = {
            "title": "Starter",
            "course_plan_id": "quest",
            "target_language": "es",
            "description": "",
        }
        self.ident, _ = await self.store.prepare(
            "@operator:x",
            "request-1",
            self.spec,
            "teacher@school.example",
            "abc2def",
            1,
        )
        api = MagicMock()
        api._hs = self.hs
        self.room_plan = MagicMock()
        self.room_plan.apply = AsyncMock()
        self.resource = CourseInvitationAPI(
            api,
            MagicMock(),
            self.claims,
            self.store,
            MagicMock(),
            "status",
            self.ident,
            self.room_plan,
        )

    def body(self, **changes):
        return {**self.NEW, "expected_course_plan_id": "quest", **changes}

    async def claimed(self):
        await self.store.reserve_creation(self.ident, "@teacher:x")
        await self.store.associate(self.ident, "@teacher:x", "!room:x")
        await self.store.complete(self.ident, "@teacher:x", "!room:x", 3)

    async def test_the_claim_provisions_the_replaced_preparation(self):
        result = await self.resource.replace("@operator:x", self.body())
        self.assertEqual(
            result["replacement"]["changes"], ["course_plan_id", "title", "description"]
        )
        self.assertEqual(result["course_plan_id"], "quest-2")
        self.assertEqual(
            result["replacements"],
            [
                {
                    "replaced_at_ms": 7,
                    "previous_course_plan_id": "quest",
                    "course_plan_id": "quest-2",
                }
            ],
        )
        row, created = await self.store.reserve_creation(self.ident, "@teacher:x")
        self.assertTrue(created)
        self.assertEqual(
            row["specification"], {**self.spec, **self.NEW}, "the claim reads it"
        )
        # The invitation and its claim code are the same ones.
        self.assertEqual(
            (await self.store.for_code("abc2def"))["invitation_id"], self.ident
        )

    async def test_repeating_the_original_preparation_still_finds_the_invitation(self):
        await self.resource.replace("@operator:x", self.body())
        ident, created = await self.store.prepare(
            "@operator:x", "request-1", self.spec, "teacher@school.example", "x", 9
        )
        self.assertEqual((ident, created), (self.ident, False))

    async def test_a_different_quest_than_expected_is_refused(self):
        with self.assertRaises(SynapseError) as error:
            await self.resource.replace(
                "@operator:x", self.body(expected_course_plan_id="other")
            )
        self.assertEqual(error.exception.code, 409)
        self.assertEqual(
            error.exception.error_dict(None)["current_course_plan_id"], "quest"
        )
        self.assertEqual(
            (await self.store.status(self.ident))["course_plan_id"], "quest"
        )

    async def test_a_dry_run_writes_nothing(self):
        result = await self.resource.replace("@operator:x", self.body(dry_run=True))
        self.assertTrue(result["replacement"]["dry_run"])
        self.assertEqual(len(result["replacement"]["changes"]), 3)
        self.assertEqual(result["course_plan_id"], "quest")
        self.assertEqual(result["replacements"], [])

    async def test_a_new_title_alone_is_no_course_plan_replacement(self):
        result = await self.resource.replace(
            "@operator:x", self.body(course_plan_id="quest")
        )
        self.assertEqual(result["replacement"]["changes"], ["title", "description"])
        self.assertEqual(result["replacements"], [])
        self.assertEqual(
            (await self.store.get(self.ident))["specification"]["title"], "Tailored"
        )

    async def test_refused_while_provisioning_and_once_revoked(self):
        await self.store.reserve_creation(self.ident, "@teacher:x")
        with self.assertRaises(SynapseError) as error:
            await self.resource.replace("@operator:x", self.body())
        self.assertEqual(error.exception.code, 409)
        self.assertEqual(error.exception.error_dict(None)["status"], "provisioning")
        await self.store.revoke(self.ident)
        with self.assertRaises(SynapseError) as error:
            await self.resource.replace("@operator:x", self.body())
        self.assertEqual(error.exception.error_dict(None)["status"], "revoked")
        self.assertEqual((await self.store.get(self.ident))["specification"], self.spec)

    async def test_a_claim_that_lands_mid_replacement_wins(self):
        def claim_first(sql, args):
            if sql.lstrip().startswith(
                "UPDATE pangea_course_invitation SET specification"
            ):
                self.pool.on_statement = None
                self.pool.connection.execute(
                    "UPDATE pangea_course_invitation SET status = 'provisioning' WHERE invitation_id = ?",
                    (self.ident,),
                )

        self.pool.on_statement = claim_first
        with self.assertRaises(SynapseError) as error:
            await self.resource.replace("@operator:x", self.body())
        self.assertEqual(error.exception.code, 409)
        self.assertEqual((await self.store.get(self.ident))["specification"], self.spec)
        self.assertEqual((await self.store.status(self.ident))["replacements"], [])

    async def test_an_instructor_invitation_is_refused(self):
        ident, _ = await self.store.prepare(
            "@operator:x",
            "instructor-1",
            {"kind": "instructor", "course_plan_id": "quest", "title": "Course"},
            "co@school.example",
            "jkm4npq",
            2,
            room_id="!room:x",
        )
        self.resource.invitation_id = ident
        with self.assertRaises(SynapseError) as error:
            await self.resource.replace("@operator:x", self.body())
        self.assertEqual(error.exception.code, 409)
        self.room_plan.apply.assert_not_awaited()

    async def test_a_legacy_room_reference_is_refused(self):
        self.resource.invitation_id = "!room:x"
        with self.assertRaises(SynapseError) as error:
            await self.resource.replace("@operator:x", self.body())
        self.assertEqual(error.exception.code, 400)

    async def test_the_expected_quest_is_required(self):
        body = self.body()
        del body["expected_course_plan_id"]
        with self.assertRaises(SynapseError) as error:
            await self.resource.replace("@operator:x", body)
        self.assertEqual(error.exception.code, 400)

    async def test_a_claimed_course_is_replaced_in_its_room_then_recorded(self):
        await self.claimed()
        self.room_plan.apply.return_value = (
            200,
            {"previous_quest_id": "quest", "quest_id": "quest-2", "changed": True},
        )
        result = await self.resource.replace("@operator:x", self.body())
        request, operator = self.room_plan.apply.await_args.args
        self.assertEqual(
            tuple(request), ("!room:x", "quest-2", "quest", "Tailored", "Text", False)
        )
        self.assertEqual(operator, "@operator:x")
        self.assertEqual(result["course_plan_id"], "quest-2")
        self.assertEqual(result["replacements"][0]["previous_course_plan_id"], "quest")
        self.assertEqual(result["replacement"]["room"]["changed"], True)

    async def test_a_room_refusal_is_returned_and_nothing_recorded(self):
        await self.claimed()
        self.room_plan.apply.return_value = (
            409,
            {
                "error": "Room points at a different quest than expected",
                "current_quest_id": "teacher-pick",
            },
        )
        with self.assertRaises(SynapseError) as error:
            await self.resource.replace("@operator:x", self.body())
        self.assertEqual(error.exception.code, 409)
        self.assertEqual(
            error.exception.error_dict(None)["current_quest_id"], "teacher-pick"
        )
        status = await self.store.status(self.ident)
        self.assertEqual(
            (status["course_plan_id"], status["replacements"]), ("quest", [])
        )

    async def test_a_claimed_dry_run_records_nothing(self):
        await self.claimed()
        self.room_plan.apply.return_value = (
            200,
            {"previous_quest_id": "quest", "quest_id": "quest-2", "changed": False},
        )
        result = await self.resource.replace("@operator:x", self.body(dry_run=True))
        self.assertTrue(self.room_plan.apply.await_args.args[0].dry_run)
        self.assertEqual(result["replacements"], [])

    async def test_a_room_already_on_the_quest_records_a_missed_replacement(self):
        # The room write landed but its record did not; sent again with the
        # new quest expected, the room is unchanged and the record is made.
        await self.claimed()
        self.room_plan.apply.return_value = (
            200,
            {"previous_quest_id": "quest-2", "quest_id": "quest-2", "changed": False},
        )
        result = await self.resource.replace(
            "@operator:x", self.body(expected_course_plan_id="quest-2")
        )
        self.assertEqual(
            [
                (r["previous_course_plan_id"], r["course_plan_id"])
                for r in result["replacements"]
            ],
            [("quest", "quest-2")],
        )
