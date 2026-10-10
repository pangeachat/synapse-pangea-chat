"""Additional-instructor invitations into an existing course: the operator
resource that prepares and lists them, and the claim that grants the role
without provisioning, ownership transfer or a share kit
(knock-with-code.instructions.md, "Codes, share kit and existing courses").

Runs the real invitation store and provisioner over an in-memory database,
with Synapse's room state and membership writes stubbed to mutate one room.
"""

from __future__ import annotations

import itertools
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.api.errors import SynapseError

from synapse_pangea_chat.email_invite.course_claims import CourseClaimStore
from synapse_pangea_chat.email_invite.course_invitation_api import CourseInvitationAPI
from synapse_pangea_chat.email_invite.instructor_invitations import (
    InstructorInvitationAPI,
)
from synapse_pangea_chat.email_invite.provision_course import CourseProvisioner
from tests.moderation_doubles import DbPoolDouble

MODULE = "synapse_pangea_chat.email_invite.instructor_invitations"
PROVISION = "synapse_pangea_chat.email_invite.provision_course"
ROOM = "!course:x"
OWNER = "@owner:x"
TEACHER = "@teacher:x"
STUDENT = "@student:x"
EMAIL = "co-teacher@school.example"
BODY = {"request_key": "invite-1", "room_id": ROOM, "teacher_email": EMAIL}


_CODES = itertools.count(1)


async def _fresh_code(*_args):
    # Every prepare mints its own code, as the real search does.
    return f"n3wcd{next(_CODES) % 100:02d}"


def _event(content: dict, sender: str = OWNER) -> SimpleNamespace:
    return SimpleNamespace(content=content, sender=sender, room_version=None)


def _course_state(*, owner_joined: bool = True, members: dict | None = None):
    state = {
        ("m.room.create", ""): _event({"type": "m.space"}),
        ("m.room.name", ""): _event({"name": "Spanish 101"}),
        ("pangea.course_plan", ""): _event({"uuid": "quest-1", "l2": "es"}),
        ("m.room.join_rules", ""): _event(
            {"join_rule": "knock", "access_code": "cl4sscd"}
        ),
        ("m.room.power_levels", ""): _event(
            {"users": {OWNER: 100}, "events": {"m.room.power_levels": 100}}
        ),
        ("m.room.member", OWNER): _event(
            {"membership": "join" if owner_joined else "leave"}
        ),
    }
    for user, membership in (members or {}).items():
        state[("m.room.member", user)] = _event({"membership": membership})
    return state


class _Room:
    """One room whose state the stubbed module API reads and writes."""

    def __init__(self, api: MagicMock, state: dict):
        self.state = state
        self.membership_writes: list[tuple[str, str, str]] = []
        self.events: list[dict] = []
        api.get_room_state = AsyncMock(side_effect=self._get_state)
        api.update_room_membership = AsyncMock(side_effect=self._membership)
        api.create_and_send_event_into_room = AsyncMock(side_effect=self._send)

    async def _get_state(self, room_id, event_filter=None):
        return dict(self.state) if room_id == ROOM else {}

    async def _membership(self, *, sender, target, room_id, new_membership, **_):
        self.membership_writes.append((sender, target, new_membership))
        self.state[("m.room.member", target)] = _event({"membership": new_membership})

    async def _send(self, event):
        self.events.append(event)
        if event["type"] == "m.room.power_levels":
            self.state[("m.room.power_levels", "")] = _event(
                event["content"], event["sender"]
            )


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api: Any = MagicMock()
        self.api.server_name = "x"
        self.api.is_mine = lambda user: user.endswith(":x")
        self.api.is_user_admin = AsyncMock(return_value=True)
        requester = MagicMock()
        requester.user.to_string.return_value = "@bot:x"
        self.api._hs.get_auth.return_value.get_user_by_req = AsyncMock(
            return_value=requester
        )
        self.api._hs.get_datastores.return_value.main.db_pool = DbPoolDouble()
        self.api._hs.get_clock.return_value.time_msec.return_value = 10
        self.claims = CourseClaimStore(self.api._hs)
        await self.claims._ensure_table()
        self.invitations = self.claims.invitations
        self.room = _Room(self.api, _course_state())
        self.blocked = AsyncMock(return_value=False)
        self.previously_admin = AsyncMock(return_value=False)
        for patcher in (
            patch(f"{MODULE}.new_unique_code", AsyncMock(side_effect=_fresh_code)),
            patch(f"{PROVISION}.is_blocked_by_room_admin", self.blocked),
            patch(f"{PROVISION}.was_previously_admin", self.previously_admin),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.resource = InstructorInvitationAPI(
            self.api, MagicMock(), self.claims, self.invitations
        )
        self.notifier = MagicMock()
        self.notifier.notify = AsyncMock()
        self.provisioner = CourseProvisioner(
            self.api, self.invitations, self.claims, self.notifier
        )

    async def _call(self, method: str, body: Any = None, args: dict | None = None):
        request = MagicMock()
        request.args = args or {}
        respond = MagicMock()
        with patch(f"{MODULE}.respond_with_json", respond), patch(
            f"{MODULE}.extract_body_json", AsyncMock(return_value=body)
        ):
            await self.resource.handle(request, method)
        respond.assert_called_once()
        return respond.call_args.args[1], respond.call_args.args[2]

    async def _prepare(self, **overrides):
        status, body = await self._call("POST", {**BODY, **overrides})
        self.assertEqual(status, 200, body)
        return body


class TestPrepare(_Base):
    async def test_prepares_a_roomed_invitation_and_sends_nothing(self):
        answer = await self._prepare()
        self.assertEqual(answer["status"], "prepared")
        self.assertEqual(answer["kind"], "instructor")
        self.assertEqual(answer["room_id"], ROOM)
        self.assertEqual(answer["deliveries"], [])
        self.assertEqual(answer["delivery_outcome"], "unsent")
        self.assertNotIn(EMAIL, str(answer))
        self.assertNotIn("n3wcd", str(answer))
        [
            digest_row
        ] = self.api._hs.get_datastores.return_value.main.db_pool.connection.execute(
            "SELECT invitation_id FROM pangea_course_invitation_code"
        ).fetchall()
        self.assertEqual(digest_row[0], answer["invitation_id"])
        row = await self.invitations.get(answer["invitation_id"])

        self.assertEqual(row["requested_email"], EMAIL)
        self.assertEqual(
            row["specification"],
            {
                "kind": "instructor",
                "title": "Spanish 101",
                "course_plan_id": "quest-1",
                "target_language": "es",
            },
        )
        self.api._hs.get_send_email_handler.assert_not_called()

    async def test_repeat_is_idempotent_and_a_changed_input_conflicts(self):
        first = await self._prepare()
        again = await self._prepare()
        self.assertEqual(again["invitation_id"], first["invitation_id"])
        status, body = await self._call(
            "POST", {**BODY, "teacher_email": "other@school.example"}
        )
        self.assertEqual(status, 409)
        self.assertNotIn("other@school.example", str(body))

    async def test_rejects_missing_fields_and_bad_addresses(self):
        for broken in (
            {"request_key": ""},
            {"teacher_email": "not-an-address"},
            {"room_id": "course:x"},
            {"room_id": None},
        ):
            with self.subTest(broken=broken):
                status, _ = await self._call("POST", {**BODY, **broken})
                self.assertEqual(status, 400)
        status, _ = await self._call("POST", ["not", "an", "object"])
        self.assertEqual(status, 400)

    async def test_unknown_room_is_404(self):
        status, body = await self._call("POST", {**BODY, "room_id": "!missing:x"})
        self.assertEqual(status, 404)
        self.assertEqual(body["errcode"], "M_NOT_FOUND")

    async def test_a_room_that_is_not_a_course_space_is_400(self):
        del self.room.state[("pangea.course_plan", "")]
        status, _ = await self._call("POST", BODY)
        self.assertEqual(status, 400)
        self.room.state = _course_state()
        self.room.state[("m.room.create", "")] = _event({})
        status, _ = await self._call("POST", BODY)
        self.assertEqual(status, 400)

    async def test_a_room_without_an_eligible_instructor_is_409(self):
        self.room.state = _course_state(owner_joined=False)
        status, body = await self._call("POST", BODY)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "no eligible instructor")
        self.assertEqual(body["errcode"], "ORG.PANGEA.NO_ELIGIBLE_INSTRUCTOR")

        # A remote administrator cannot act for the room either.
        self.room.state = _course_state(members={"@remote:elsewhere": "join"})
        self.room.state[("m.room.power_levels", "")] = _event(
            {"users": {"@remote:elsewhere": 100}}
        )
        self.room.state[("m.room.member", OWNER)] = _event({"membership": "leave"})
        status, _ = await self._call("POST", BODY)
        self.assertEqual(status, 409)

    async def test_only_a_server_admin_may_prepare_or_list(self):
        self.api.is_user_admin = AsyncMock(return_value=False)
        for method in ("POST", "GET"):
            status, _ = await self._call(method, BODY)
            self.assertEqual(status, 403)

    async def test_status_reads_carry_the_kind(self):
        answer = await self._prepare()
        status = await self.invitations.status(answer["invitation_id"])
        self.assertEqual(status["kind"], "instructor")
        ident, _ = await self.invitations.prepare(
            "@bot:x",
            "course-1",
            {"title": "Course", "course_plan_id": "q", "target_language": "es"},
            "teacher@school.example",
            "abc2def",
            1,
        )
        self.assertEqual((await self.invitations.status(ident))["kind"], "course")

    async def test_the_reminder_path_emails_an_instructor_invitation(self):
        answer = await self._prepare()
        mailer = MagicMock()
        mailer.send_course_reminder = AsyncMock()
        reminder = CourseInvitationAPI(
            self.api,
            MagicMock(app_base_url="https://app.example.test"),
            self.claims,
            self.invitations,
            mailer,
            "reminder",
        )
        with patch(
            "synapse_pangea_chat.email_invite.course_invitation_api.new_unique_code",
            AsyncMock(return_value="fr3shcd"),
        ):
            result = await reminder.remind(
                {
                    "invitation_id": answer["invitation_id"],
                    "subject": "Join Spanish 101 as an instructor",
                    "body": "You have been invited.",
                    "cta_label": "Accept",
                }
            )
        sent = mailer.send_course_reminder.await_args.kwargs
        self.assertEqual(sent["email_address"], EMAIL)
        self.assertEqual(sent["claim_url"], "https://app.example.test/fr3shcd")
        self.assertEqual(result["delivery_outcome"], "accepted")
        self.assertEqual(result["kind"], "instructor")
        self.assertIsNotNone(await self.invitations.for_code("fr3shcd"))


class TestList(_Base):
    async def test_lists_instructor_invitations_newest_first_without_addresses(self):
        first = await self._prepare()
        self.api._hs.get_clock.return_value.time_msec.return_value = 20
        second = await self._prepare(request_key="invite-2")
        await self.invitations.prepare(
            "@bot:x",
            "course-1",
            {"title": "Course", "course_plan_id": "q", "target_language": "es"},
            "teacher@school.example",
            "abc2def",
            30,
        )
        status, body = await self._call("GET")
        self.assertEqual(status, 200)
        self.assertEqual(
            [row["invitation_id"] for row in body["invitations"]],
            [second["invitation_id"], first["invitation_id"]],
        )
        row = body["invitations"][0]
        self.assertEqual(
            set(row),
            {
                "invitation_id",
                "room_id",
                "status",
                "claimant",
                "created_at_ms",
                "completed_at_ms",
                "deliveries",
                "delivery_outcome",
            },
        )
        self.assertEqual(row["deliveries"], 0)
        self.assertEqual(row["room_id"], ROOM)
        self.assertNotIn(EMAIL, str(body))

    async def test_filters_by_status_and_refuses_unknown_ones(self):
        answer = await self._prepare()
        await self.invitations.revoke(answer["invitation_id"])
        status, body = await self._call("GET", args={b"status": [b"prepared"]})
        self.assertEqual((status, body), (200, {"invitations": []}))
        status, body = await self._call("GET", args={b"status": [b"revoked"]})
        self.assertEqual([row["status"] for row in body["invitations"]], ["revoked"])
        status, _ = await self._call("GET", args={b"status": [b"sent"]})
        self.assertEqual(status, 400)


class TestClaim(_Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ident = (await self._prepare())["invitation_id"]

    async def _invitation(self):
        return await self.invitations.get(self.ident)

    async def _claim(self, user=TEACHER, **kwargs):
        return await self.provisioner.claim(await self._invitation(), user, **kwargs)

    def _power(self, user):
        return self.room.state[("m.room.power_levels", "")].content["users"].get(user)

    async def test_a_stranger_is_invited_joined_and_promoted(self):
        self.assertEqual(await self._claim(), ROOM)
        self.assertEqual(
            self.room.membership_writes,
            [(OWNER, TEACHER, "invite"), (TEACHER, TEACHER, "join")],
        )
        [grant] = self.room.events
        self.assertEqual(grant["sender"], OWNER)
        self.assertEqual(grant["content"]["users"], {OWNER: 100, TEACHER: 100})
        self.assertEqual(
            grant["content"]["events"], {"m.room.power_levels": 100}, "rest preserved"
        )
        row = await self._invitation()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["claimant"], TEACHER)
        self.assertEqual(row["completed_at_ms"], 10)
        self.assertIsNone(row["requested_email"])

    async def test_a_joined_student_is_promoted_without_an_invite(self):
        self.room.state[("m.room.member", TEACHER)] = _event({"membership": "join"})
        self.assertEqual(await self._claim(), ROOM)
        self.assertEqual(self.room.membership_writes, [])
        self.assertEqual(self._power(TEACHER), 100)
        self.assertEqual((await self._invitation())["status"], "completed")

    async def test_a_pending_invite_is_accepted_not_reissued(self):
        self.room.state[("m.room.member", TEACHER)] = _event({"membership": "invite"})
        await self._claim()
        self.assertEqual(self.room.membership_writes, [(TEACHER, TEACHER, "join")])

    async def test_completion_owes_no_share_kit_and_writes_no_claim_row(self):
        await self._claim()
        self.notifier.notify.assert_not_called()
        self.assertEqual(await self.claims.outstanding_notices(1_000), [])
        self.assertIsNone(await self.claims.get(ROOM))
        self.assertEqual(await self.claims.rooms_for_admin_code("n3wcd01"), [])

        # Nothing about the room changed but membership and power.
        join = self.room.state[("m.room.join_rules", "")].content
        self.assertEqual(join, {"join_rule": "knock", "access_code": "cl4sscd"})
        self.assertEqual([e["type"] for e in self.room.events], ["m.room.power_levels"])
        self.api._hs.get_room_creation_handler.return_value.create_room.assert_not_called()

    async def test_replay_after_completion_returns_the_room_and_changes_nothing(self):
        await self._claim()
        writes = list(self.room.membership_writes)
        self.assertEqual(await self._claim(), ROOM)
        self.assertEqual(self.room.membership_writes, writes)
        self.assertEqual(len(self.room.events), 1)

    async def test_a_demoted_winner_cannot_replay_and_others_see_nothing(self):
        await self._claim()
        self.room.state[("m.room.power_levels", "")] = _event({"users": {OWNER: 100}})
        with self.assertRaises(SynapseError) as error:
            await self._claim()
        self.assertEqual(error.exception.errcode, "ORG.PANGEA.CODE_NOT_FOUND")
        with self.assertRaises(SynapseError) as error:
            await self._claim(user="@other:x")
        self.assertEqual(error.exception.errcode, "ORG.PANGEA.CODE_NOT_FOUND")

    async def test_a_banned_user_claims_nothing(self):
        self.room.state[("m.room.member", TEACHER)] = _event({"membership": "ban"})
        with self.assertRaises(SynapseError) as error:
            await self._claim()
        self.assertEqual(error.exception.errcode, "ORG.PANGEA.CODE_NOT_FOUND")
        self.assertEqual((await self._invitation())["status"], "prepared")
        self.assertEqual(self.room.membership_writes, [])

    async def test_a_user_every_admin_blocked_claims_nothing_and_learns_nothing(self):
        self.blocked.return_value = True
        with self.assertRaises(SynapseError) as error:
            await self._claim()
        self.assertEqual(error.exception.code, 404)
        self.assertEqual(error.exception.errcode, "ORG.PANGEA.CODE_NOT_FOUND")
        self.assertNotIn("block", error.exception.msg.lower())
        self.blocked.assert_awaited_once_with(self.api, ROOM, TEACHER)
        self.assertEqual((await self._invitation())["status"], "prepared")
        self.assertEqual(self.room.membership_writes, [])

    async def test_the_gate_switch_turns_the_block_check_off(self):
        self.blocked.return_value = True
        self.provisioner.blocked_join_gate_enabled = False
        self.assertEqual(await self._claim(), ROOM)
        self.blocked.assert_not_awaited()

    async def test_a_revoked_invitation_claims_nothing(self):
        await self.invitations.revoke(self.ident)
        with self.assertRaises(SynapseError) as error:
            await self._claim()
        self.assertEqual(error.exception.errcode, "ORG.PANGEA.CODE_NOT_FOUND")
        self.assertEqual(self.room.membership_writes, [])
        self.assertEqual(self.room.events, [])

    async def test_no_eligible_instructor_requires_recovery(self):
        self.room.state[("m.room.member", OWNER)] = _event({"membership": "leave"})
        with self.assertRaises(SynapseError) as error:
            await self._claim()
        self.assertEqual(error.exception.errcode, "ORG.PANGEA.CLAIM_RECOVERY_REQUIRED")
        self.assertEqual(error.exception.code, 503)
        row = await self._invitation()
        self.assertEqual((row["status"], row["claimant"]), ("provisioning", TEACHER))
        self.assertEqual(self.room.membership_writes, [])

    async def test_a_grant_that_does_not_take_is_not_completed(self):
        self.api.create_and_send_event_into_room = AsyncMock()
        with self.assertRaises(SynapseError) as error:
            await self._claim()
        self.assertEqual(error.exception.errcode, "ORG.PANGEA.CLAIM_RECOVERY_REQUIRED")
        self.assertEqual((await self._invitation())["status"], "provisioning")

    async def test_rights_withdrawn_during_a_partial_claim_are_not_restored(self):
        await self.invitations.reserve_creation(self.ident, TEACHER)
        self.previously_admin.return_value = True
        with self.assertRaises(SynapseError) as error:
            await self._claim()
        self.assertEqual(error.exception.errcode, "ORG.PANGEA.CLAIM_RECOVERY_REQUIRED")
        self.assertEqual(self.room.membership_writes, [])

    async def test_a_concurrent_claim_by_the_same_account_defers(self):
        await self.invitations.reserve_creation(self.ident, TEACHER)
        prepared = {
            **(await self._invitation()),
            "status": "prepared",
            "claimant": None,
        }
        self.assertIsNone(
            await self.provisioner.claim(prepared, TEACHER, defer_to_concurrent=True)
        )

    async def test_another_account_never_takes_a_reserved_claim(self):
        await self.invitations.reserve_creation(self.ident, TEACHER)
        with self.assertRaises(SynapseError) as error:
            await self._claim(user="@other:x")
        self.assertEqual(error.exception.errcode, "ORG.PANGEA.CODE_NOT_FOUND")
