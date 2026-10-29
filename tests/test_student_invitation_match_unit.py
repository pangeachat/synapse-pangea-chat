"""Inviting an account into the course a student invitation was sent for, when the account
holds the invited address (invite-by-email.instructions.md, "Joining without the link").

Runs the real invitation store over an in-memory database, with Synapse's room
membership stubbed.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.email_invite import student_invitation_match
from synapse_pangea_chat.email_invite.student_invitation_match import (
    MATCH_WINDOW_MS,
    StudentInvitationMatch,
)
from synapse_pangea_chat.email_invite.student_invitations import (
    StudentInvitationStore,
)
from synapse_pangea_chat.room_code.invite_user_to_room import NoInviterAvailableError
from tests.moderation_doubles import DbPoolDouble

STUDENT = "@ann:x"
NOW = MATCH_WINDOW_MS + 1000
COURSE = "!course:x"
OTHER_COURSE = "!other:x"


class TestStudentInvitationMatch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = MagicMock()
        self.main = self.api._hs.get_datastores.return_value.main
        self.main.db_pool = DbPoolDouble()
        self.main.user_get_threepids = AsyncMock(
            return_value=[
                SimpleNamespace(medium="email", address="ann@school.example"),
                SimpleNamespace(medium="msisdn", address="15550000000"),
            ]
        )
        self.main.get_user_id_by_threepid = AsyncMock(return_value=None)
        self.api._hs.get_clock.return_value.time_msec.return_value = NOW
        self.students = StudentInvitationStore(self.api._hs)
        self.matcher = StudentInvitationMatch(
            self.api, self.students, blocked_join_gate_enabled=True
        )
        self.membership: dict = {}
        self.blocked_in: set = set()
        doubles = {
            "get_user_room_membership": AsyncMock(
                side_effect=lambda _api, _user, room: self.membership.get(room)
            ),
            "invite_user_to_room": AsyncMock(),
            "is_blocked_by_room_admin": AsyncMock(
                side_effect=lambda _api, room, _user: room in self.blocked_in
            ),
            "_capture_exception": MagicMock(),
        }
        for name, double in doubles.items():
            patcher = patch.object(student_invitation_match, name, double)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.invite = doubles["invite_user_to_room"]
        self.captured = doubles["_capture_exception"]

    async def _invitation(self, room=COURSE, email="Ann@School.example", sent=NOW):
        # Codes only need to differ; the room keeps them apart.
        code = "c" + str(len(await self.students.for_room(room))) + room[1:6]
        return await self.students.create(room, email, code, "@teacher:x", sent)

    def _invited(self):
        return [
            (call.kwargs["user_id"], call.kwargs["room_id"])
            for call in self.invite.await_args_list
        ]

    async def test_registers_both_triggers(self):
        self.api.register_account_validity_callbacks.assert_called_once_with(
            on_user_login=self.matcher.on_user_login
        )
        self.api.register_third_party_rules_callbacks.assert_called_once_with(
            on_add_user_third_party_identifier=self.matcher.on_add_user_third_party_identifier
        )

    async def test_sign_in_invites_into_each_matching_course_once(self):
        await self._invitation(COURSE)
        await self._invitation(OTHER_COURSE, "ann@school.example")
        await self._invitation("!stranger:x", "bob@school.example")
        await self.matcher.on_user_login(STUDENT, None, None)
        self.assertCountEqual(
            self._invited(), [(STUDENT, COURSE), (STUDENT, OTHER_COURSE)]
        )
        # Declined: the student holds no membership, and is not invited again.
        await self.matcher.on_user_login(STUDENT, None, None)
        self.assertEqual(len(self._invited()), 2)
        self.captured.assert_not_called()

    async def test_adding_a_verified_email_invites_and_other_media_do_not(self):
        await self._invitation()
        await self.matcher.on_add_user_third_party_identifier(
            STUDENT, "msisdn", "15550000000"
        )
        self.assertEqual(self._invited(), [])
        await self.matcher.on_add_user_third_party_identifier(
            STUDENT, "email", "ann@school.example"
        )
        self.assertEqual(self._invited(), [(STUDENT, COURSE)])

    async def test_only_invitations_from_the_last_120_days_match(self):
        await self._invitation(OTHER_COURSE, sent=NOW - MATCH_WINDOW_MS - 1)
        await self._invitation(COURSE, sent=NOW - MATCH_WINDOW_MS)
        await self.matcher.on_user_login(STUDENT, None, None)
        self.assertEqual(self._invited(), [(STUDENT, COURSE)])

    async def test_a_member_an_invitee_a_banned_or_blocked_account_is_not_invited(self):
        for room, membership in (
            ("!joined:x", "join"),
            ("!invited:x", "invite"),
            ("!banned:x", "ban"),
            ("!blocked:x", None),
        ):
            self.membership[room] = membership
            await self._invitation(room)
        self.blocked_in.add("!blocked:x")
        await self.matcher.on_user_login(STUDENT, None, None)
        self.assertEqual(self._invited(), [])
        # The match stands: once each has left, unbanned or unblocked, a
        # sign-in still invites into none of them.
        self.membership.clear()
        self.blocked_in.clear()
        await self.matcher.on_user_login(STUDENT, None, None)
        self.assertEqual(self._invited(), [])
        self.captured.assert_not_called()

    async def test_a_failed_invite_is_reported_and_tried_again_at_the_next_sign_in(
        self,
    ):
        await self._invitation()
        self.invite.side_effect = RuntimeError("homeserver busy")
        await self.matcher.on_user_login(STUDENT, None, None)
        self.captured.assert_called_once()
        self.invite.side_effect = None
        await self.matcher.on_user_login(STUDENT, None, None)
        self.assertEqual(self._invited(), [(STUDENT, COURSE)] * 2)

    async def test_a_course_with_no_one_to_invite_from_is_reported_once(self):
        await self._invitation()
        self.invite.side_effect = NoInviterAvailableError(COURSE)
        await self.matcher.on_user_login(STUDENT, None, None)
        await self.matcher.on_user_login(STUDENT, None, None)
        self.assertEqual(len(self._invited()), 1)
        self.captured.assert_called_once()

    async def test_a_failed_lookup_is_reported_and_never_fails_the_sign_in(self):
        await self._invitation()
        self.main.user_get_threepids.side_effect = RuntimeError("database down")
        await self.matcher.on_user_login(STUDENT, None, None)
        self.assertEqual(self._invited(), [])
        self.captured.assert_called_once()

    async def test_an_unparseable_invited_address_matches_nothing_and_breaks_nothing(
        self,
    ):
        await self._invitation(OTHER_COURSE, "not an address")
        await self._invitation(COURSE)
        await self.matcher.on_user_login(STUDENT, None, None)
        self.assertEqual(self._invited(), [(STUDENT, COURSE)])

    async def test_an_invitation_to_an_address_an_account_holds_invites_it(self):
        await self._invitation()
        self.main.get_user_id_by_threepid.return_value = STUDENT
        await self.matcher.match_address(" Ann@School.example ")
        self.main.get_user_id_by_threepid.assert_awaited_once_with(
            "email", "ann@school.example"
        )
        self.assertEqual(self._invited(), [(STUDENT, COURSE)])

    async def test_an_invitation_to_an_address_no_account_holds_invites_no_one(self):
        await self._invitation()
        await self.matcher.match_address("ann@school.example")
        await self.matcher.match_address("not an address")
        self.assertEqual(self._invited(), [])
        self.main.get_user_id_by_threepid.side_effect = RuntimeError("database down")
        await self.matcher.match_address("ann@school.example")
        self.captured.assert_called_once()
