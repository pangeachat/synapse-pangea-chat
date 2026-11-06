"""Real Synapse/Postgres/SMTP coverage of inviting an account into the course a
student invitation was sent for, when the account holds the invited address
(invite-by-email.instructions.md, "Joining without the link")."""

from urllib.parse import quote

import requests

from tests.base_e2e import BaseSynapseE2ETest
from tests.smtp_sink import SmtpSink

APP = "https://app.example.test"
PASSWORD = "123123123"


class TestStudentInvitationMatchE2E(BaseSynapseE2ETest):
    sink: SmtpSink

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.sink = SmtpSink().start()
        cls.addClassCleanup(cls.sink.stop)

    def request(self, method, path, token, body=None):
        return requests.request(
            method,
            self.server_url + path,
            json=body,
            headers={"Authorization": "Bearer " + token},
            timeout=60,
        )

    async def course(self, teacher_token):
        room = await self.create_private_room_knock_allowed_room(teacher_token)
        rules = self.request(
            "PUT",
            f"/_matrix/client/v3/rooms/{quote(room, safe='')}/state/m.room.join_rules",
            teacher_token,
            {"join_rule": "knock", "access_code": "cl4ss" + self.tag[:2].lower()},
        )
        self.assertEqual(rules.status_code, 200, rules.text)
        return room

    def invite_by_email(self, teacher_token, room, address):
        sent = self.request(
            "POST",
            "/_synapse/client/pangea/v1/invite_by_email",
            teacher_token,
            {"room_id": room, "emails": [address]},
        )
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(sent.json()["emailed"], [address], sent.text)

    def add_verified_address(self, op_token, user_id, address):
        added = self.request(
            "PUT",
            "/_synapse/admin/v2/users/" + quote(user_id, safe=""),
            op_token,
            {"threepids": [{"medium": "email", "address": address}]},
        )
        self.assertEqual(added.status_code, 200, added.text)

    def membership(self, teacher_token, room, user_id):
        member = self.request(
            "GET",
            f"/_matrix/client/v3/rooms/{quote(room, safe='')}/state/m.room.member/"
            + quote(user_id, safe=""),
            teacher_token,
        )
        return member.json().get("membership") if member.status_code == 200 else None

    async def users(self):
        await self.homeserver(
            module_config={"app_base_url": APP},
            synapse_config_overrides={"email": self.sink.synapse_email_config()},
        )
        _, op = await self.new_user("operator", admin=True)
        teacher, teacher_token = await self.new_user("teacher")
        student, _ = await self.new_user("student", password=PASSWORD)
        return op, teacher_token, student

    async def test_an_invitation_to_an_address_an_account_holds_invites_it_once(self):
        op, teacher_token, student = await self.users()
        verified = f"ann+{self.tag}@school.example"
        self.add_verified_address(op, student, verified)
        room = await self.course(teacher_token)

        # As the teacher typed it; Synapse stores verified addresses in lower case.
        self.invite_by_email(teacher_token, room, f"Ann+{self.tag}@School.example")
        self.assertEqual(self.membership(teacher_token, room, student), "invite")

        # Declined, and not invited again at the next sign-in.
        _, student_token = await self.login_user(self.localpart("student"), PASSWORD)
        left = self.request(
            "POST",
            f"/_matrix/client/v3/rooms/{quote(room, safe='')}/leave",
            student_token,
            {},
        )
        self.assertEqual(left.status_code, 200, left.text)
        await self.login_user(self.localpart("student"), PASSWORD)
        self.assertEqual(self.membership(teacher_token, room, student), "leave")

    async def test_verifying_the_invited_address_invites_the_account(self):
        op, teacher_token, student = await self.users()
        address = f"bob+{self.tag}@school.example"
        room = await self.course(teacher_token)
        self.invite_by_email(teacher_token, room, address)
        self.assertIsNone(self.membership(teacher_token, room, student))

        self.add_verified_address(op, student, address)
        self.assertEqual(self.membership(teacher_token, room, student), "invite")
        _, student_token = await self.login_user(self.localpart("student"), PASSWORD)
        self.assertTrue(await self.accept_room_invitation(room, student_token))
        self.assertEqual(self.membership(teacher_token, room, student), "join")
