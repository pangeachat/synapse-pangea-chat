"""The Safety page's Synapse half against a real Synapse and Postgres.

What the unit tests cannot pin: Synapse's own event handler deciding
visibility and room binding for a report, the course rule over real room
state, the incident table on Postgres, and a Tier 1 block and a Tier 2
redaction each landing on the course whose chat it was - and a direct
message's on none - read back through the endpoint a course admin uses.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List
from urllib.parse import quote

import psycopg2
import requests

from .base_e2e import BaseSynapseE2ETest
from .mock_moderation_server import MockModerationServer

_SYNAPSE_CONFIG = {
    "rc_joins": {"local": {"per_second": 9999, "burst_count": 9999}},
    "rc_message": {"per_second": 9999, "burst_count": 9999},
}


class TestSafetyIncidentsE2E(BaseSynapseE2ETest):
    # The homeserver's config carries the mock's port, so the mock lives as
    # long as the class's shared homeserver.
    moderation: MockModerationServer

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.moderation = MockModerationServer().start()
        cls.addClassCleanup(cls.moderation.stop)

    def _report_url(self) -> str:
        return f"{self.server_url}/_synapse/client/pangea/v1/report"

    def _incidents_url(self) -> str:
        return f"{self.server_url}/_synapse/client/pangea/v1/safety_incidents"

    def _headers(self, token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _create_room(self, token: str, **extra: Any) -> str:
        body: Dict[str, Any] = {"visibility": "private", "preset": "public_chat"}
        body.update(extra)
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json=body,
            headers=self._headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["room_id"]

    def _course(self, token: str) -> str:
        return self._create_room(
            token,
            creation_content={"type": "m.space"},
            initial_state=[
                {
                    "type": "pangea.course_plan",
                    "state_key": "",
                    "content": {"uuid": "plan-1"},
                }
            ],
            name="Course",
        )

    def _join(self, room_id: str, token: str) -> None:
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/join/{quote(room_id, safe='')}",
            json={},
            headers=self._headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def _leave(self, room_id: str, token: str) -> None:
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/rooms/{quote(room_id, safe='')}/leave",
            json={},
            headers=self._headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def _send(self, room_id: str, token: str, body: str) -> requests.Response:
        return requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
            f"/send/m.room.message/{uuid.uuid4().hex}",
            json={"msgtype": "m.text", "body": body},
            headers=self._headers(token),
        )

    def _report(
        self, token: str, room_id: str, event_id: str, report_id: str, reason: str
    ) -> requests.Response:
        return requests.post(
            self._report_url(),
            json={
                "report_id": report_id,
                "room_id": room_id,
                "event_id": event_id,
                "reason": reason,
            },
            headers=self._headers(token),
        )

    def _incidents(self, token: str, space_id: str) -> requests.Response:
        return requests.get(
            self._incidents_url(),
            params={"space_id": space_id},
            headers=self._headers(token),
        )

    def _wait_for_incident(
        self,
        token: str,
        space_id: str,
        predicate: Any,
        timeout_s: float = 30.0,
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last: List[Dict[str, Any]] = []
        while time.monotonic() < deadline:
            resp = self._incidents(token, space_id)
            self.assertEqual(resp.status_code, 200, resp.text)
            last = resp.json()["incidents"]
            for incident in last:
                if predicate(incident):
                    return incident
            time.sleep(0.5)
        self.fail(f"no matching incident; last seen: {last}")

    def _stored_course_ids(self, incident_id: str, timeout_s: float = 30.0) -> str:
        """The row's stored `course_ids`, once the row is written."""
        deadline = time.monotonic() + timeout_s
        conn = psycopg2.connect(self.database_url)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                while time.monotonic() < deadline:
                    cur.execute(
                        "SELECT course_ids FROM pangea_safety_incidents "
                        "WHERE incident_id = %s",
                        (incident_id,),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        return str(row[0])
                    time.sleep(0.5)
        finally:
            conn.close()
        self.fail(f"{incident_id} was never written")

    async def test_reports_moderation_and_the_course_admin_read(self) -> None:
        await self.homeserver(
            module_config={
                "moderation": {
                    "tier1_enabled": True,
                    "tier1_phone_regions": ["US"],
                    "tier2_enabled": True,
                    "choreo_base_url": self.moderation.base_url,
                    "choreo_access_token": "syt_mock_service_token",
                },
            },
            synapse_config_overrides=_SYNAPSE_CONFIG,
        )
        (teacher_id, teacher) = await self.new_user("teacher")
        (learner_id, learner) = await self.new_user("learner")
        (_, outsider) = await self.new_user("outsider")

        course = self._course(teacher)
        other_course = self._course(teacher)
        self._join(course, learner)
        server = teacher_id.split(":", 1)[1]
        chat = self._create_room(
            teacher,
            name="Week 3 chat",
            preset="private_chat",
            initial_state=[
                {
                    "type": "m.room.history_visibility",
                    "state_key": "",
                    "content": {"history_visibility": "joined"},
                },
                {
                    "type": "m.space.parent",
                    "state_key": course,
                    "content": {"via": [server], "canonical": True},
                },
            ],
        )
        linked = requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/{quote(course, safe='')}"
            f"/state/m.space.child/{quote(chat, safe='')}",
            json={"via": [server]},
            headers=self._headers(teacher),
        )
        self.assertEqual(linked.status_code, 200, linked.text)
        requests.post(
            f"{self.server_url}/_matrix/client/v3/rooms/{quote(chat, safe='')}/invite",
            json={"user_id": learner_id},
            headers=self._headers(teacher),
        )
        self._join(chat, learner)
        sent = self._send(chat, learner, "you are rudely unkind")
        self.assertEqual(sent.status_code, 200, sent.text)
        event_id = sent.json()["event_id"]
        outsider_room = self._create_room(outsider)
        elsewhere = self._send(outsider_room, outsider, "elsewhere").json()["event_id"]

        # --- A browser's preflight is answered, with CORS, by Synapse ---
        for url in (self._report_url(), self._incidents_url()):
            preflight = requests.options(
                url,
                headers={
                    "Origin": "https://app.pangea.chat",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )
            self.assertEqual(preflight.status_code, 204, preflight.text)
            self.assertIn("Access-Control-Allow-Origin", preflight.headers)

        # --- The report endpoint ---
        report_id = str(uuid.uuid4())
        resp = self._report(teacher, chat, event_id, report_id, "unkind\x00!")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"incident_id": f"report:{report_id}"})
        retry = self._report(teacher, chat, event_id, report_id, "unkind\x00!")
        self.assertEqual((retry.status_code, retry.json()), (200, resp.json()))
        # Unknown event, an event from another room, and an event the
        # caller cannot see: an outsider who was never in the room.
        self.assertEqual(
            self._report(teacher, chat, "$nope", str(uuid.uuid4()), "x").status_code,
            404,
        )
        self.assertEqual(
            self._report(teacher, chat, elsewhere, str(uuid.uuid4()), "x").status_code,
            403,
        )
        self.assertEqual(
            self._report(outsider, chat, event_id, str(uuid.uuid4()), "x").status_code,
            403,
        )

        # --- A direct message between two learners of the course ---
        (classmate_id, classmate) = await self.new_user("classmate")
        self._join(course, classmate)
        dm = self._create_room(learner, preset="private_chat", is_direct=True)
        requests.post(
            f"{self.server_url}/_matrix/client/v3/rooms/{quote(dm, safe='')}/invite",
            json={"user_id": classmate_id},
            headers=self._headers(learner),
        )
        self._join(dm, classmate)
        dm_flagged = self._send(dm, learner, "FLAGME in a direct message")
        self.assertEqual(dm_flagged.status_code, 200, dm_flagged.text)
        dm_flagged_id = dm_flagged.json()["event_id"]
        dm_report_id = str(uuid.uuid4())
        resp = self._report(classmate, dm, dm_flagged_id, dm_report_id, "rude")
        self.assertEqual(resp.status_code, 200, resp.text)

        # --- Tier 1 and Tier 2 in the course's chat land on the course ---
        blocked = self._send(chat, learner, "text me at 415-555-2671")
        self.assertEqual(blocked.status_code, 403, blocked.text)
        flagged = self._send(chat, learner, "FLAGME you are awful")
        self.assertEqual(flagged.status_code, 200, flagged.text)
        flagged_id = flagged.json()["event_id"]

        # --- The read endpoint ---
        reported = self._wait_for_incident(
            teacher, course, lambda i: i["incident_id"] == f"report:{report_id}"
        )
        self.assertEqual(reported["subject_id"], learner_id)
        self.assertEqual(reported["reporter_id"], teacher_id)
        self.assertEqual(reported["text"], "you are rudely unkind")
        self.assertEqual(reported["reason"], "unkind␀!")
        self.assertEqual(reported["action"], "reported")
        self.assertEqual(reported["room_name"], "Week 3 chat")
        self.assertNotIn("course_ids", reported)
        block = self._wait_for_incident(
            teacher, course, lambda i: i["action"] == "blocked"
        )
        self.assertEqual(block["text"], "text me at 415-555-2671")
        self.assertIsNone(block["event_id"])
        removed = self._wait_for_incident(
            teacher,
            course,
            lambda i: i["event_id"] == flagged_id and i["outcome"] == "removed",
        )
        self.assertEqual(removed["action"], "redacted")
        self.assertEqual(removed["text"], "FLAGME you are awful")
        rows = self._incidents(teacher, course).json()["incidents"]
        self.assertEqual(
            [(r["created_ms"], r["incident_id"]) for r in rows],
            sorted((r["created_ms"], r["incident_id"]) for r in rows),
        )
        # The teacher's other course gets none of it.
        self.assertEqual(self._incidents(teacher, other_course).json()["incidents"], [])
        # The direct message is no course's chat: its flag and its report are
        # recorded, on no course, and the learner's course lists neither.
        self.assertEqual(
            self._stored_course_ids(f"mod:{dm_flagged_id}"), "[]", "flag in a DM"
        )
        self.assertEqual(
            self._stored_course_ids(f"report:{dm_report_id}"), "[]", "DM report"
        )
        rows = self._incidents(teacher, course).json()["incidents"]
        self.assertFalse([r for r in rows if r["event_id"] == dm_flagged_id])

        # --- Refusals: the same 403 for each ---
        denied = self._incidents(learner, course)
        self.assertEqual(denied.status_code, 403, denied.text)
        for token, room in ((outsider, course), (teacher, chat)):
            resp = self._incidents(token, room)
            self.assertEqual(resp.status_code, 403, resp.text)
            self.assertEqual(resp.json(), denied.json())
        self._leave(course, teacher)
        departed = self._incidents(teacher, course)
        self.assertEqual(departed.status_code, 403, departed.text)
        self.assertEqual(requests.get(self._incidents_url()).status_code, 401)

        # --- Neither text nor reason reaches the server log ---
        log_text = "\n".join(self.server_log())
        self.assertIn("safety report recorded as", log_text)
        for secret in ("you are rude", "unkind", "FLAGME you are awful"):
            self.assertNotIn(secret, log_text)
