"""The Safety page's Synapse half against a real Synapse and Postgres.

What the unit tests cannot pin: Synapse's own event handler deciding
visibility and room binding for a report, the course rule over real room
state, the incident table on Postgres, and a Tier 1 block and a Tier 2
redaction each landing on the learner's course - read back through the
endpoint a course admin uses.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests

from .base_e2e import BaseSynapseE2ETest
from .mock_moderation_server import MockModerationServer

_SYNAPSE_CONFIG = {
    "rc_login": {
        "address": {"per_second": 9999, "burst_count": 9999},
        "account": {"per_second": 9999, "burst_count": 9999},
    },
    "rc_joins": {"local": {"per_second": 9999, "burst_count": 9999}},
    "rc_message": {"per_second": 9999, "burst_count": 9999},
}

_REPORT = "http://localhost:8008/_synapse/client/pangea/v1/report"
_INCIDENTS = "http://localhost:8008/_synapse/client/pangea/v1/safety_incidents"


class TestSafetyIncidentsE2E(BaseSynapseE2ETest):
    def _headers(self, token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def _user(
        self, config_path: str, synapse_dir: str, name: str
    ) -> Tuple[str, str]:
        await self.register_user(config_path, synapse_dir, name, f"{name}-pw", False)
        return await self.login_user(name, f"{name}-pw")

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
            _REPORT,
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
            _INCIDENTS, params={"space_id": space_id}, headers=self._headers(token)
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

    async def test_reports_moderation_and_the_course_admin_read(self) -> None:
        mock = MockModerationServer().start()
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config={
                    "moderation": {
                        "tier1_enabled": True,
                        "tier1_phone_regions": ["US"],
                        "tier2_enabled": True,
                        "choreo_base_url": mock.base_url,
                        "choreo_access_token": "syt_mock_service_token",
                    },
                },
                synapse_config_overrides=_SYNAPSE_CONFIG,
            )
            (teacher_id, teacher) = await self._user(
                config_path, synapse_dir, "teacher"
            )
            (learner_id, learner) = await self._user(
                config_path, synapse_dir, "learner"
            )
            (_, outsider) = await self._user(config_path, synapse_dir, "outsider")

            course = self._course(teacher)
            other_course = self._course(teacher)
            self._join(course, learner)
            chat = self._create_room(
                teacher,
                preset="private_chat",
                initial_state=[
                    {
                        "type": "m.room.history_visibility",
                        "state_key": "",
                        "content": {"history_visibility": "joined"},
                    }
                ],
            )
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
            elsewhere = self._send(outsider_room, outsider, "elsewhere").json()[
                "event_id"
            ]

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
                self._report(
                    teacher, chat, "$nope", str(uuid.uuid4()), "x"
                ).status_code,
                404,
            )
            self.assertEqual(
                self._report(
                    teacher, chat, elsewhere, str(uuid.uuid4()), "x"
                ).status_code,
                403,
            )
            self.assertEqual(
                self._report(
                    outsider, chat, event_id, str(uuid.uuid4()), "x"
                ).status_code,
                403,
            )

            # --- Tier 1 and Tier 2 land on the learner's course ---
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
            self.assertEqual(
                self._incidents(teacher, other_course).json()["incidents"], []
            )

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
            self.assertEqual(requests.get(_INCIDENTS).status_code, 401)

            # --- Neither text nor reason reaches the server log ---
            log_text = "\n".join(self.server_stdout_lines + self.server_stderr_lines)
            self.assertIn("safety report recorded as", log_text)
            for secret in ("you are rude", "unkind", "FLAGME you are awful"):
                self.assertNotIn(secret, log_text)
        finally:
            mock.stop()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
