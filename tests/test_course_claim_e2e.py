"""E2E: claiming a room-backed (legacy) requested course, through a real Synapse
and its mail path.

knock-with-code.instructions.md ("Claiming a course"): legacy room-backed
claims remain valid. Whoever uses the claim link first becomes admin; the class
code never grants admin; the class link then goes to the requesting address.
No endpoint makes these courses any more, so each test builds one as the
retired v1 endpoint left it (tests/requested_course.py).

The homeserver sends through a local SMTP sink, so the assertions are on the
mail Synapse actually delivered.
"""

import asyncio
import unittest
from typing import Any, Dict
from urllib.parse import quote

import psycopg2
import requests

from tests.base_e2e import BaseSynapseE2ETest
from tests.requested_course import legacy_course
from tests.smtp_sink import SmtpSink, body_text

KNOCK_WITH_CODE_PATH = "/_synapse/client/pangea/v1/knock_with_code"
APP_BASE_URL = "https://app.example.test"


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

    def _codes(self) -> tuple[str, str]:
        """A class code and a claim code unique to this test."""
        self._minted = getattr(self, "_minted", 0) + 1
        suffix = f"{self.tag}{self._minted}".replace("t", "")
        return (f"c{suffix}zzzzzz")[:7], (f"a{suffix}qqqqqq")[:7]

    def _legacy_course(
        self, bot: str, bot_id: str, admin_code: str, class_code: str
    ) -> str:
        return legacy_course(
            self.server_url,
            self.database_url,
            bot,
            bot_id,
            self.requested,
            admin_code,
            class_code,
        )

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
        bot_id, bot = await self.new_user("bot", admin=True)
        teacher_id, teacher = await self.new_user("teacher")
        student_id, student = await self.new_user("student")
        latecomer_id, latecomer = await self.new_user("latecomer")

        class_code, admin_code = self._codes()
        room_id = self._legacy_course(bot, bot_id, admin_code, class_code)
        legacy_status = requests.get(
            f"{self.server_url}/_synapse/client/pangea/v2/course_invitations/{quote(room_id, safe='')}",
            headers={"Authorization": f"Bearer {bot}"},
            timeout=10,
        )
        self.assertEqual(legacy_status.status_code, 200, legacy_status.text)
        self.assertEqual(legacy_status.json()["status"], "prepared")
        self.assertNotIn(self.requested, legacy_status.text)

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
        self.assertEqual(len(self.sink.messages_to(self.requested)), 0)

        # The teacher tries their class link first, as a member...
        await self._join_with_code(teacher, class_code, room_id)
        self.assertEqual(self._power_level(bot, room_id, teacher_id), 0)
        self.assertEqual(len(self.sink.messages_to(self.requested)), 0)

        # ...then the claim link makes them admin and sends email 2.
        response = self._post(
            KNOCK_WITH_CODE_PATH, teacher, {"access_code": admin_code}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["already_joined"], [room_id])
        self.assertEqual(self._power_level(bot, room_id, teacher_id), 100)

        # The module's own share kit (#936): the course on the dashboard, signed,
        # and no class link, code or QR code.
        claimed = self.sink.wait_for(self.requested, "Spanish 1 is ready")
        self.assertIsNotNone(claimed)
        assert claimed is not None
        claimed_text = body_text(claimed)
        self.assertIn("Open your course in the educator dashboard", claimed_text)
        self.assertIn("/_synapse/client/pangea/v1/n?t=", claimed_text)
        self.assertNotIn(f"{APP_BASE_URL}/{class_code}", claimed_text)
        self.assertNotIn(class_code, claimed_text)
        images = [p for p in claimed.walk() if p.get_content_type() == "image/png"]
        self.assertEqual(images, [])
        self.assertNotIn(admin_code, claimed_text)

        # The address is cleared once it has done its job.
        requested_email, claimed_by, notice_sent_at_ms = self._claim_row(room_id)
        self.assertIsNone(requested_email)
        self.assertEqual(claimed_by, teacher_id)
        self.assertIsNotNone(notice_sent_at_ms)

        # The claim link is spent.
        spent = self._post(KNOCK_WITH_CODE_PATH, latecomer, {"access_code": admin_code})
        self.assertEqual(spent.status_code, 404, spent.text)
        self.assertEqual(len(self.sink.messages_to(self.requested)), 1)

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
        self.assertEqual(len(self.sink.messages_to(self.requested)), 1)


if __name__ == "__main__":
    unittest.main()
