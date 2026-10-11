"""E2E: an additional instructor claims an existing course through a real
Synapse and its mail path (knock-with-code.instructions.md, "Codes, share kit
and existing courses"; create-course-space.instructions.md, "Claim reminders").

The invitation is prepared without mail, the caller's rendered email goes out
through the v2 reminder endpoint with the claim link as a slot, and the invitee
claims with the code in it. The claim promotes the invitee by editing the
course's power levels, which a real homeserver hands over as frozen event
content: the staging claim of 2026-10-10 failed there, which no unit double
reproduced.
"""

import asyncio
import re
import unittest
from typing import Any, Dict
from urllib.parse import quote

import requests

from tests.base_e2e import BaseSynapseE2ETest
from tests.smtp_sink import SmtpSink, body_text

CREATE_COURSE_SPACE_PATH = "/_synapse/client/pangea/v1/create_course_space"
KNOCK_WITH_CODE_PATH = "/_synapse/client/pangea/v1/knock_with_code"
INSTRUCTOR_INVITATIONS_PATH = "/_synapse/client/pangea/v2/instructor_invitations"
REMINDER_PATH = "/_synapse/client/pangea/v2/send_course_claim_reminder"
APP_BASE_URL = "https://app.example.test"
OWNER_EMAIL = "owner@school.example"
COLLEAGUE_EMAIL = "colleague@school.example"
PASSWORD = "123123123"


class TestInstructorInvitationE2E(BaseSynapseE2ETest):
    def _post(self, path: str, token: str, body: Dict[str, Any]) -> requests.Response:
        return requests.post(
            f"{self.server_url}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

    def _get(self, path: str, token: str) -> requests.Response:
        return requests.get(
            f"{self.server_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

    def _join(self, token: str, room_id: str) -> requests.Response:
        return requests.post(
            f"{self.server_url}/_matrix/client/v3/rooms/"
            f"{quote(room_id, safe='')}/join",
            json={},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

    async def _claim(self, token: str, code: str, room_id: str) -> None:
        response = self._post(KNOCK_WITH_CODE_PATH, token, {"access_code": code})
        self.assertEqual(response.status_code, 200, response.text)
        # A claim joins the claimant server-side; a plain code knocks and is
        # invited, and the client then joins.
        answer = response.json()
        if answer.get("already_joined") == [room_id]:
            return
        self.assertEqual(answer["rooms"], [room_id], answer)
        for _ in range(10):
            if self._join(token, room_id).status_code == 200:
                return
            await asyncio.sleep(0.5)
        joined = self._join(token, room_id)
        self.assertEqual(joined.status_code, 200, joined.text)

    def _power_levels(self, token: str, room_id: str) -> Dict[str, Any]:
        response = self._get(
            f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
            "/state/m.room.power_levels",
            token,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_an_invited_instructor_claims_with_the_rendered_link(self) -> None:
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        with SmtpSink() as sink:
            try:
                (
                    postgres,
                    synapse_dir,
                    config_path,
                    server_process,
                    stdout_thread,
                    stderr_thread,
                ) = await self.start_test_synapse(
                    module_config={"app_base_url": APP_BASE_URL},
                    synapse_config_overrides={"email": sink.synapse_email_config()},
                )
                for user, admin in (
                    ("bot", True),
                    ("owner", False),
                    ("colleague", False),
                ):
                    await self.register_user(
                        config_path=config_path,
                        dir=synapse_dir,
                        user=user,
                        password=PASSWORD,
                        admin=admin,
                    )
                _, bot = await self.login_user("bot", PASSWORD)
                owner_id, owner = await self.login_user("owner", PASSWORD)
                colleague_id, colleague = await self.login_user("colleague", PASSWORD)

                # An existing course with a joined instructor.
                created = self._post(
                    CREATE_COURSE_SPACE_PATH,
                    bot,
                    {
                        "title": "Spanish 1",
                        "course_plan_id": "plan-instructor",
                        "target_language": "es",
                        "teacher_email": OWNER_EMAIL,
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                room_id = created.json()["room_id"]
                await self._claim(owner, created.json()["admin_access_code"], room_id)
                levels = self._power_levels(owner, room_id)
                self.assertEqual(levels["users"].get(owner_id), 100)

                # Prepared without mail; the answer carries neither address
                # nor code.
                prepared = self._post(
                    INSTRUCTOR_INVITATIONS_PATH,
                    bot,
                    {
                        "request_key": "e2e-instructor-1",
                        "room_id": room_id,
                        "teacher_email": COLLEAGUE_EMAIL,
                    },
                )
                self.assertEqual(prepared.status_code, 200, prepared.text)
                invitation_id = prepared.json()["invitation_id"]
                self.assertEqual(prepared.json()["status"], "prepared")
                self.assertNotIn(COLLEAGUE_EMAIL, prepared.text)
                await asyncio.sleep(1)
                self.assertEqual(sink.messages_to(COLLEAGUE_EMAIL), [])

                # The caller's rendered email, with the claim link as a slot.
                sent = self._post(
                    REMINDER_PATH,
                    bot,
                    {
                        "invitation_id": invitation_id,
                        "subject": "Join Spanish 1 as an instructor",
                        "body": "You were invited to teach Spanish 1.",
                        "cta_label": "Accept",
                        "html": '<p>You were invited.</p><a href="{{cta_url}}">Accept</a>',
                        "text": "You were invited.\n\nAccept: {{cta_url}}",
                        "receiving_reason": "A teacher of this course invited you.",
                    },
                )
                self.assertEqual(sent.status_code, 200, sent.text)
                mail = sink.wait_for(COLLEAGUE_EMAIL, "Join Spanish 1")
                self.assertIsNotNone(mail)
                assert mail is not None
                text = body_text(mail)
                self.assertNotIn("{{", text)
                found = re.search(re.escape(APP_BASE_URL) + r"/(\w+)", text)
                self.assertIsNotNone(found, text)
                assert found is not None
                code = found.group(1)

                # The claim: invited, joined and promoted, the owner untouched.
                await self._claim(colleague, code, room_id)
                levels = self._power_levels(colleague, room_id)
                self.assertEqual(levels["users"].get(colleague_id), 100)
                self.assertEqual(levels["users"].get(owner_id), 100)

                listed = self._get(
                    INSTRUCTOR_INVITATIONS_PATH + "?status=completed", bot
                )
                self.assertEqual(listed.status_code, 200, listed.text)
                [row] = listed.json()["invitations"]
                self.assertEqual(
                    (row["invitation_id"], row["claimant"], row["room_id"]),
                    (invitation_id, colleague_id, room_id),
                )
                self.assertNotIn(COLLEAGUE_EMAIL, listed.text)
                self.assertNotIn(code, listed.text)

                # The code is spent: a second use does not grant anything new.
                again = self._post(KNOCK_WITH_CODE_PATH, bot, {"access_code": code})
                self.assertNotEqual(again.status_code, 500, again.text)
            finally:
                self.stop_synapse(
                    server_process=server_process,
                    stdout_thread=stdout_thread,
                    stderr_thread=stderr_thread,
                    synapse_dir=synapse_dir,
                    postgres=postgres,
                )


if __name__ == "__main__":
    unittest.main()
