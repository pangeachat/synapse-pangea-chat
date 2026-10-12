"""E2E: claiming a requested course, through a real Synapse and its mail path.

knock-with-code.instructions.md ("Claiming a course"): the requesting address
gets the claim link first and nothing for students; whoever uses it first
becomes admin; the class code never grants admin; the class link then goes to
the requesting address. create-course-space: the address
stays out of room state.

The homeserver sends through a local SMTP sink, so the assertions are on the
mail Synapse actually delivered.
"""

import asyncio
import json
import unittest
from typing import Any, Dict
from urllib.parse import quote

import psycopg2
import requests

from synapse_pangea_chat.email_invite.course_claim_emails import class_qr_png
from tests.base_e2e import BaseSynapseE2ETest
from tests.smtp_sink import SmtpSink, body_text

CREATE_COURSE_SPACE_PATH = "/_synapse/client/pangea/v1/create_course_space"
KNOCK_WITH_CODE_PATH = "/_synapse/client/pangea/v1/knock_with_code"
APP_BASE_URL = "https://app.example.test"
REMINDER_PATH = "/_synapse/client/pangea/v1/send_course_claim_reminder"
PREVIEW_PATH = "/_synapse/client/pangea/v1/preview_with_code"


class TestCourseClaimE2E(BaseSynapseE2ETest):
    sink: SmtpSink

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.sink = SmtpSink().start()
        cls.addClassCleanup(cls.sink.stop)

    def setUp(self) -> None:
        super().setUp()
        # The sink keeps every test's mail, so each test asks only for its own.
        self.requested = f"requester+{self.tag}@school.example"

    async def _homeserver(self) -> None:
        await self.homeserver(
            module_config={"app_base_url": APP_BASE_URL},
            synapse_config_overrides={"email": self.sink.synapse_email_config()},
        )

    def _post(self, path: str, token: str, body: Dict[str, Any]) -> requests.Response:
        return requests.post(
            f"{self.server_url}{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

    def _room_get(self, token: str, room_id: str, suffix: str) -> Any:
        response = requests.get(
            f"{self.server_url}/_matrix/client/v3/rooms/"
            f"{quote(room_id, safe='')}/{suffix}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _join(self, token: str, room_id: str) -> None:
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/rooms/"
            f"{quote(room_id, safe='')}/join",
            json={},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)

    async def _join_with_code(self, token: str, code: str, room_id: str) -> None:
        response = self._post(KNOCK_WITH_CODE_PATH, token, {"access_code": code})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["rooms"], [room_id])
        # The invite is issued server-side; give it a moment to land.
        for _ in range(10):
            try:
                self._join(token, room_id)
                return
            except AssertionError:
                await asyncio.sleep(0.5)
        self._join(token, room_id)

    def _power_level(self, token: str, room_id: str, user_id: str) -> int:
        levels = self._room_get(token, room_id, "state/m.room.power_levels")
        return levels.get("users", {}).get(user_id, levels.get("users_default", 0))

    def _claim_row(self, room_id: str) -> Any:
        with psycopg2.connect(self.database_url) as conn, conn.cursor() as cur:
            # The table is created on first use, so a homeserver that never
            # recorded an address may not have it at all.
            cur.execute("SELECT to_regclass('pangea_course_claim')")
            if cur.fetchone()[0] is None:
                return None
            cur.execute(
                "SELECT requested_email, claimed_by, notice_sent_at_ms "
                "FROM pangea_course_claim WHERE room_id = %s",
                (room_id,),
            )
            return cur.fetchone()

    async def test_claim_link_then_class_link(self) -> None:
        await self._homeserver()
        _, bot = await self.new_user("bot", admin=True)
        teacher_id, teacher = await self.new_user("teacher")
        student_id, student = await self.new_user("student")
        latecomer_id, latecomer = await self.new_user("latecomer")

        response = self._post(
            CREATE_COURSE_SPACE_PATH,
            bot,
            {
                "title": "Spanish 1",
                "description": "Companion practice for Lessons 1 to 6.",
                "course_plan_id": "plan-claim",
                "target_language": "es",
                "teacher_email": self.requested,
                "request_summary": "Spanish 1 practice for my class",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        created = response.json()
        room_id = created["room_id"]
        legacy_status = requests.get(
            f"{self.server_url}/_synapse/client/pangea/v2/course_invitations/{quote(room_id, safe='')}",
            headers={"Authorization": f"Bearer {bot}"},
            timeout=10,
        )
        self.assertEqual(legacy_status.status_code, 200, legacy_status.text)
        self.assertEqual(legacy_status.json()["status"], "prepared")
        self.assertNotIn(self.requested, legacy_status.text)
        class_code = created["student_access_code"]
        admin_code = created["admin_access_code"]
        self.assertTrue(created["emailed"])
        self.assertEqual(created["admin_join_url"], f"{APP_BASE_URL}/{admin_code}")

        # Email 1: the claim link and nothing that belongs with students.
        ready = self.sink.wait_for(self.requested, "Your quest is ready")
        self.assertIsNotNone(ready)
        assert ready is not None
        ready_text = body_text(ready)
        self.assertIn(f"{APP_BASE_URL}/{admin_code}", ready_text)
        # The link is the only claim path in mail (decided 2026-10-09):
        # no printed code, no store links, and the re-tap line instead.
        self.assertNotIn(
            admin_code, ready_text.replace(f"{APP_BASE_URL}/{admin_code}", "")
        )
        self.assertNotIn("apps.apple.com", ready_text)
        self.assertNotIn("play.google.com", ready_text)
        self.assertIn("Tap the link again after you sign in", ready_text)
        self.assertIn("Spanish 1 practice for my class", ready_text)
        self.assertNotIn(class_code, ready_text)

        # Neither the requesting address nor the claim code is in room
        # state: every member can read it.
        state = self._room_get(bot, room_id, "state")
        self.assertNotIn(self.requested, json.dumps(state))
        self.assertNotIn(admin_code, json.dumps(state).lower())
        join_rules = self._room_get(bot, room_id, "state/m.room.join_rules")
        self.assertNotIn("admin_access_code", join_rules)

        # The claim code previews the course, though it is in no state.
        preview = self._post(
            "/_synapse/client/pangea/v1/preview_with_code",
            latecomer,
            {"access_code": admin_code},
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIn(room_id, preview.text)

        # The class code never grants admin, whatever the order.
        await self._join_with_code(student, class_code, room_id)
        self.assertEqual(self._power_level(bot, room_id, student_id), 0)
        self.assertEqual(len(self.sink.messages_to(self.requested)), 1)

        # The teacher tries their class link first, as a member...
        await self._join_with_code(teacher, class_code, room_id)
        self.assertEqual(self._power_level(bot, room_id, teacher_id), 0)
        self.assertEqual(len(self.sink.messages_to(self.requested)), 1)

        # ...then the claim link makes them admin and sends email 2.
        response = self._post(
            KNOCK_WITH_CODE_PATH, teacher, {"access_code": admin_code}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["already_joined"], [room_id])
        self.assertEqual(self._power_level(bot, room_id, teacher_id), 100)

        claimed = self.sink.wait_for(self.requested, "Invite your students")
        self.assertIsNotNone(claimed)
        assert claimed is not None
        claimed_text = body_text(claimed)
        self.assertIn(f"{APP_BASE_URL}/{class_code}", claimed_text)
        # The module's own template carries the link's QR code, inline.
        images = [p for p in claimed.walk() if p.get_content_type() == "image/png"]
        self.assertEqual(len(images), 1)
        self.assertEqual(
            images[0].get_payload(decode=True),
            class_qr_png(f"{APP_BASE_URL}/{class_code}"),
        )
        self.assertNotIn(admin_code, claimed_text)

        # The address is cleared once it has done its job.
        requested_email, claimed_by, notice_sent_at_ms = self._claim_row(room_id)
        self.assertIsNone(requested_email)
        self.assertEqual(claimed_by, teacher_id)
        self.assertIsNotNone(notice_sent_at_ms)

        # The claim link is spent.
        spent = self._post(KNOCK_WITH_CODE_PATH, latecomer, {"access_code": admin_code})
        self.assertEqual(spent.status_code, 404, spent.text)
        self.assertEqual(len(self.sink.messages_to(self.requested)), 2)

        # A later admin code, set by the teacher for a co-teacher, is an
        # ordinary grant: it promotes, and it is not a claim.
        co_teacher_code = "c0teach"
        response = requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/"
            f"{quote(room_id, safe='')}/state/m.room.join_rules",
            json={
                "join_rule": "knock",
                "access_code": class_code,
                "admin_access_code": co_teacher_code,
            },
            headers={"Authorization": f"Bearer {teacher}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        await self._join_with_code(latecomer, co_teacher_code, room_id)
        self.assertEqual(self._power_level(bot, room_id, latecomer_id), 100)
        await asyncio.sleep(1)
        self.assertEqual(len(self.sink.messages_to(self.requested)), 2)

    async def test_a_reminder_link_claims_and_spends_the_first(self) -> None:
        """create-course-space "Claim reminders": a reminder carries a new
        claim link; every link works until the claim, which spends them all."""
        await self._homeserver()
        _, bot = await self.new_user("bot", admin=True)
        teacher_id, teacher = await self.new_user("teacher")

        response = self._post(
            CREATE_COURSE_SPACE_PATH,
            bot,
            {"title": "Spanish 1", "teacher_email": self.requested},
        )
        self.assertEqual(response.status_code, 200, response.text)
        room_id = response.json()["room_id"]
        first_code = response.json()["admin_access_code"]
        self.assertIsNotNone(self.sink.wait_for(self.requested, "Your quest is ready"))

        reminder_body = {
            "room_id": room_id,
            "subject": "Your course is waiting",
            "body": "Your course is ready.\n\nOpen it to become its teacher.",
            "cta_label": "Open your course",
        }
        # Only a server admin may send one.
        refused = self._post(REMINDER_PATH, teacher, reminder_body)
        self.assertEqual(refused.status_code, 403, refused.text)

        sent = self._post(REMINDER_PATH, bot, reminder_body)
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(sent.json(), {"sent": True})
        reminder = self.sink.wait_for(self.requested, "Your course is waiting")
        self.assertIsNotNone(reminder)
        assert reminder is not None
        reminder_text = body_text(reminder)
        self.assertIn("Open it to become its teacher.", reminder_text)
        prefix = f"{APP_BASE_URL}/"
        start = reminder_text.index(prefix) + len(prefix)
        reminder_code = reminder_text[start : start + 7]
        self.assertNotEqual(reminder_code.lower(), first_code.lower())
        self.assertNotIn(
            reminder_code, reminder_text.replace(prefix + reminder_code, "")
        )
        self.assertNotIn("apps.apple.com", reminder_text)
        self.assertIn("Tap the link again after you sign in", reminder_text)

        # Both links open the course until it is claimed.
        for code in (first_code, reminder_code):
            preview = self._post(PREVIEW_PATH, bot, {"access_code": code})
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertIn(room_id, preview.text)

        # Claiming with the reminder's link spends the first one too.
        claimed = self._post(
            KNOCK_WITH_CODE_PATH, teacher, {"access_code": reminder_code}
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual(claimed.json()["already_joined"], [room_id])
        self._join(teacher, room_id)
        self.assertEqual(self._power_level(bot, room_id, teacher_id), 100)
        self.assertIsNotNone(self.sink.wait_for(self.requested, "Invite your students"))
        spent = self._post(KNOCK_WITH_CODE_PATH, teacher, {"access_code": first_code})
        self.assertEqual(spent.status_code, 200, spent.text)
        self.assertEqual(spent.json()["already_joined"], [room_id])

        # A claimed course takes no more reminders.
        after = self._post(REMINDER_PATH, bot, reminder_body)
        self.assertEqual(after.status_code, 409, after.text)

    async def test_no_address_means_no_email(self) -> None:
        await self._homeserver()
        _, bot = await self.new_user("bot", admin=True)

        response = self._post(CREATE_COURSE_SPACE_PATH, bot, {"title": "No address"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["emailed"])
        await asyncio.sleep(1)
        self.assertEqual(self.sink.messages_to(self.requested), [])
        # The claim is recorded (it is where the admin code lives), with
        # no address and so no notice ever owed.
        row = self._claim_row(response.json()["room_id"])
        self.assertIsNotNone(row)
        self.assertIsNone(row[0])


if __name__ == "__main__":
    unittest.main()
