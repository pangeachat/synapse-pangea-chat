"""Claiming a prepared course for the account that holds its requesting address.

Runs the real invitation store and provisioner over an in-memory database, with
Synapse's room creation and room state stubbed.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.api.errors import SynapseError

from synapse_pangea_chat.email_invite import claim_by_email
from synapse_pangea_chat.email_invite.claim_by_email import ClaimByEmail
from synapse_pangea_chat.email_invite.course_claims import CourseClaimStore
from synapse_pangea_chat.email_invite.provision_course import CourseProvisioner
from tests.moderation_doubles import DbPoolDouble

TEACHER = "@teacher:x"
SPEC = {
    "title": "Course",
    "description": "",
    "course_plan_id": "quest",
    "target_language": "es",
}


def _event(content):
    return SimpleNamespace(content=content)


def _claimed_room_state(user):
    return {
        ("m.room.member", user): _event({"membership": "join"}),
        ("m.room.power_levels", ""): _event(
            {"users": {user: 100}, "events": {"m.space.child": 0}}
        ),
        ("pangea.course_plan", ""): _event({"uuid": "quest", "l2": "es"}),
        ("pangea.course_settings", ""): _event({"require_analytics_access": True}),
        ("m.room.join_rules", ""): _event(
            {"join_rule": "knock", "access_code": "cl4sscd"}
        ),
    }


async def _free_code(*_args):
    # A real code search reads the database; yielding here is what lets two
    # claims both read an invitation as prepared before either reserves it.
    await asyncio.sleep(0)
    return "cl4sscd"


class TestClaimByEmail(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = MagicMock()
        self.api.server_name = "x"
        main = self.api._hs.get_datastores.return_value.main
        main.db_pool = DbPoolDouble()
        main.user_get_threepids = AsyncMock(
            return_value=[
                SimpleNamespace(medium="email", address="teacher@school.example"),
                SimpleNamespace(medium="msisdn", address="15550000000"),
            ]
        )
        self.threepids = main.user_get_threepids
        self.api._hs.get_clock.return_value.time_msec.return_value = 10
        self.release_create = asyncio.Event()
        self.release_create.set()

        async def create_room(**_kwargs):
            await self.release_create.wait()
            return f"!room{self.create_room.await_count}:x", None, None

        self.create_room = AsyncMock(side_effect=create_room)
        self.api._hs.get_room_creation_handler.return_value.create_room = (
            self.create_room
        )
        self.api.get_room_state = AsyncMock(return_value=_claimed_room_state(TEACHER))
        self.claims = CourseClaimStore(self.api._hs)
        await self.claims._ensure_table()
        self.invitations = self.claims.invitations
        self.provisioner = CourseProvisioner(
            self.api, self.invitations, self.claims, MagicMock()
        )
        self.claimer = ClaimByEmail(self.api, self.invitations, self.provisioner)
        # The address as the teacher typed it at the booth.
        self.ident, _ = await self.invitations.prepare(
            "@operator:x", "request-1", SPEC, "Teacher@School.example", "abc2def", 1
        )
        for patcher in (
            patch(
                "synapse_pangea_chat.email_invite.provision_course.new_unique_code",
                side_effect=_free_code,
            ),
            patch("synapse.logging.context.run_in_background"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        capture = patch.object(claim_by_email, "_capture_exception")
        self.captured = capture.start()
        self.addCleanup(capture.stop)

    async def test_registers_both_triggers(self):
        self.api.register_account_validity_callbacks.assert_called_once_with(
            on_user_login=self.claimer.on_user_login
        )
        self.api.register_third_party_rules_callbacks.assert_called_once_with(
            on_add_user_third_party_identifier=self.claimer.on_add_user_third_party_identifier
        )

    async def test_prepared_lookup_ignores_case_and_skips_claimed_or_revoked(self):
        other, _ = await self.invitations.prepare(
            "@operator:x", "request-2", SPEC, "teacher@school.example", "ghi3jkm", 2
        )
        revoked, _ = await self.invitations.prepare(
            "@operator:x", "request-3", SPEC, "teacher@school.example", "mno4pqr", 3
        )
        await self.invitations.revoke(revoked)
        await self.invitations.prepare(
            "@operator:x", "request-4", SPEC, "someone@else.example", "stu5vwx", 4
        )
        found = await self.invitations.prepared_for_emails(["TEACHER@school.EXAMPLE"])
        self.assertEqual([row["invitation_id"] for row in found], [self.ident, other])
        await self.invitations.reserve_creation(other, TEACHER)
        found = await self.invitations.prepared_for_emails(["teacher@school.example"])
        self.assertEqual([row["invitation_id"] for row in found], [self.ident])
        self.assertEqual(await self.invitations.prepared_for_emails([]), [])

    async def test_sign_in_claims_every_matching_invitation(self):
        second, _ = await self.invitations.prepare(
            "@operator:x", "request-2", SPEC, "teacher@school.example", "ghi3jkm", 2
        )
        await self.claimer.on_user_login(TEACHER, "m.login.password", None)
        for ident in (self.ident, second):
            row = await self.invitations.get(ident)
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["claimant"], TEACHER)
        self.assertEqual(self.create_room.await_count, 2)
        self.captured.assert_not_called()

    async def test_adding_a_verified_email_claims_and_other_media_do_not(self):
        await self.claimer.on_add_user_third_party_identifier(
            TEACHER, "msisdn", "15550000000"
        )
        self.threepids.assert_not_awaited()
        await self.claimer.on_add_user_third_party_identifier(
            TEACHER, "email", "teacher@school.example"
        )
        self.assertEqual(
            (await self.invitations.get(self.ident))["status"], "completed"
        )

    async def test_a_second_sign_in_creates_no_second_room(self):
        await self.claimer.on_user_login(TEACHER, "m.login.sso", "google")
        await self.claimer.on_user_login(TEACHER, "m.login.token", None)
        self.assertEqual(self.create_room.await_count, 1)
        self.captured.assert_not_called()

    async def test_two_devices_signing_in_at_once_make_one_room_and_no_error(self):
        self.release_create.clear()
        first = asyncio.ensure_future(self.claimer.on_user_login(TEACHER, None, None))
        second = asyncio.ensure_future(self.claimer.on_user_login(TEACHER, None, None))
        while self.create_room.await_count == 0:
            await asyncio.sleep(0)
        # The second sign-in reserved after the first and yields to it.
        await second
        self.release_create.set()
        await first
        self.assertEqual(self.create_room.await_count, 1)
        self.assertEqual(
            (await self.invitations.get(self.ident))["status"], "completed"
        )
        self.captured.assert_not_called()

    async def test_an_unmatched_account_claims_nothing(self):
        self.threepids.return_value = [
            SimpleNamespace(medium="email", address="other@school.example")
        ]
        await self.claimer.on_user_login("@other:x", None, None)
        self.assertEqual((await self.invitations.get(self.ident))["status"], "prepared")
        self.create_room.assert_not_awaited()

    async def test_a_failed_claim_is_reported_and_never_fails_the_sign_in(self):
        self.create_room.side_effect = RuntimeError("room creation failed")
        await self.claimer.on_user_login(TEACHER, None, None)
        self.captured.assert_called_once()
        self.assertIsInstance(self.captured.call_args.args[0], RuntimeError)

    async def test_a_failed_lookup_is_reported_and_never_fails_the_sign_in(self):
        self.threepids.side_effect = RuntimeError("database down")
        await self.claimer.on_user_login(TEACHER, None, None)
        self.captured.assert_called_once()
        self.create_room.assert_not_awaited()

    async def test_an_invitation_taken_since_the_lookup_is_not_an_error(self):
        self.provisioner.claim = AsyncMock(
            side_effect=SynapseError(404, "gone", "ORG.PANGEA.CODE_NOT_FOUND")
        )
        await self.claimer.on_user_login(TEACHER, None, None)
        self.captured.assert_not_called()

    async def test_a_server_error_from_the_claim_is_reported(self):
        self.provisioner.claim = AsyncMock(
            side_effect=SynapseError(
                503, "recover", "ORG.PANGEA.CLAIM_RECOVERY_REQUIRED"
            )
        )
        await self.claimer.on_user_login(TEACHER, None, None)
        self.captured.assert_called_once()
