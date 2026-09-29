"""Integration tests for the course_member_emails endpoint.

Uses ``BaseSynapseE2ETest`` to spin up a local Synapse + PostgreSQL instance
with the ``PangeaChat`` module loaded.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

from .base_e2e import BaseSynapseE2ETest

_SYNAPSE_CONFIG = {
    "rc_login": {
        "address": {"per_second": 9999, "burst_count": 9999},
        "account": {"per_second": 9999, "burst_count": 9999},
    },
    "rc_joins": {
        "local": {"per_second": 9999, "burst_count": 9999},
    },
    "rc_message": {"per_second": 9999, "burst_count": 9999},
}

_ENDPOINT = "http://localhost:8008/_synapse/client/pangea/v1/course_member_emails"
_ADMIN_USERS_API = "http://localhost:8008/_synapse/admin/v2/users"


def _module_config(
    *,
    enabled: bool = True,
    requests_per_burst: int = 100,
) -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "course_member_emails_requests_per_burst": requests_per_burst,
        "course_member_emails_burst_duration_seconds": 60,
    }
    if enabled:
        config["course_member_emails_enabled"] = True
    return config


class TestCourseMemberEmailsEndpoint(BaseSynapseE2ETest):
    """Tests for ``POST /_synapse/client/pangea/v1/course_member_emails``."""

    # -- helpers --

    def _headers(self, token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _call(self, room_id: Any, token: Optional[str]) -> requests.Response:
        headers = self._headers(token) if token is not None else {}
        return requests.post(_ENDPOINT, json={"room_id": room_id}, headers=headers)

    def _members(self, room_id: str, token: str) -> List[Dict[str, Any]]:
        resp = self._call(room_id, token)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["members"]

    async def _user(
        self, config_path: str, synapse_dir: str, name: str, admin: bool = False
    ) -> Tuple[str, str]:
        await self.register_user(config_path, synapse_dir, name, f"{name}-pw", admin)
        return await self.login_user(name, f"{name}-pw")

    def _bind_email(self, admin_token: str, user_id: str, address: str) -> None:
        resp = requests.put(
            f"{_ADMIN_USERS_API}/{quote(user_id, safe='')}",
            json={"threepids": [{"medium": "email", "address": address}]},
            headers=self._headers(admin_token),
        )
        self.assertIn(resp.status_code, (200, 201), resp.text)

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

    def _set_power(self, room_id: str, token: str, user_id: str, level: int) -> None:
        url = (
            f"{self.server_url}/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
            "/state/m.room.power_levels/"
        )
        resp = requests.get(url, headers=self._headers(token))
        self.assertEqual(resp.status_code, 200, resp.text)
        content = resp.json()
        content.setdefault("users", {})[user_id] = level
        resp = requests.put(url, json=content, headers=self._headers(token))
        self.assertEqual(resp.status_code, 200, resp.text)

    # -- tests --

    async def test_course_admin_reads_joined_students_emails(self) -> None:
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
                module_config=_module_config(),
                synapse_config_overrides=_SYNAPSE_CONFIG,
            )
            (_, root) = await self._user(config_path, synapse_dir, "root", admin=True)
            (teacher_id, teacher) = await self._user(
                config_path, synapse_dir, "teacher"
            )
            (coteacher_id, coteacher) = await self._user(
                config_path, synapse_dir, "coteacher"
            )
            (s1_id, s1) = await self._user(config_path, synapse_dir, "student1")
            (s2_id, s2) = await self._user(config_path, synapse_dir, "student2")
            (s3_id, s3) = await self._user(config_path, synapse_dir, "student3")
            (bot_id, bot) = await self._user(config_path, synapse_dir, "bot")
            (_, outsider) = await self._user(config_path, synapse_dir, "outsider")

            for user_id, address in (
                (teacher_id, "teacher@school.edu"),
                (coteacher_id, "coteacher@school.edu"),
                (s1_id, "Student1@School.edu"),
                (s2_id, "student2@school.edu"),
                (bot_id, "bot@pangea.chat"),
            ):
                self._bind_email(root, user_id, address)

            course = self._create_room(
                teacher, creation_content={"type": "m.space"}, name="Course"
            )
            for token in (coteacher, s1, s2, s3, bot):
                self._join(course, token)
            self._set_power(course, teacher, coteacher_id, 100)

            # The teacher sees each joined student with an email. The student
            # with no email is omitted; the caller, the co-teacher and the bot
            # are never listed.
            self.assertEqual(
                self._members(course, teacher),
                [
                    {"user_id": s1_id, "email": "student1@school.edu"},
                    {"user_id": s2_id, "email": "student2@school.edu"},
                ],
            )
            # The co-teacher is a course admin too, and sees the same students.
            self.assertEqual(
                [m["user_id"] for m in self._members(course, coteacher)],
                [s1_id, s2_id],
            )

            # Every refusal is the same 403 and the same body: a student in the
            # course, an outsider, an unknown room, and a non-space room.
            student_resp = self._call(course, s1)
            self.assertEqual(student_resp.status_code, 403, student_resp.text)
            self.assertNotIn("school.edu", student_resp.text)
            chat = self._create_room(teacher, name="Just a chat")
            self._join(chat, s1)
            for room_id, token in (
                (course, outsider),
                ("!doesnotexist:my.domain.name", teacher),
                (chat, teacher),
            ):
                with self.subTest(room_id=room_id):
                    resp = self._call(room_id, token)
                    self.assertEqual(resp.status_code, 403, resp.text)
                    self.assertEqual(resp.json(), student_resp.json())

            # A student who leaves drops out on the next call.
            self._leave(course, s2)
            self.assertEqual(
                self._members(course, teacher),
                [{"user_id": s1_id, "email": "student1@school.edu"}],
            )

            # In a room version where creators hold unlimited power without a
            # power-levels entry, the creator is still a course admin.
            v12_course = self._create_room(
                teacher,
                room_version="12",
                creation_content={"type": "m.space"},
                name="Course v12",
            )
            self._join(v12_course, s1)
            self.assertEqual(
                self._members(v12_course, teacher),
                [{"user_id": s1_id, "email": "student1@school.edu"}],
            )
            self.assertEqual(self._call(v12_course, s1).status_code, 403)

            # Validation and authentication.
            self.assertEqual(self._call(course, None).status_code, 401)
            for bad in ("", "not-a-room", 42, None):
                with self.subTest(bad=bad):
                    self.assertEqual(self._call(bad, teacher).status_code, 400)
            resp = requests.post(
                _ENDPOINT, json=[course], headers=self._headers(teacher)
            )
            self.assertEqual(resp.status_code, 400, resp.text)

            # Addresses never reach the server log.
            log_text = "\n".join(self.server_stdout_lines + self.server_stderr_lines)
            # (The module's own log line is captured, so the check below is live.)
            self.assertIn("course_member_emails: room=", log_text)
            self.assertNotIn("student1@school.edu", log_text)
            self.assertNotIn("student2@school.edu", log_text)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_off_by_default_and_rate_limited_per_caller(self) -> None:
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
                module_config=_module_config(enabled=False),
                synapse_config_overrides=_SYNAPSE_CONFIG,
            )
            (_, teacher) = await self._user(config_path, synapse_dir, "teacher")
            course = self._create_room(teacher, creation_content={"type": "m.space"})
            # With the flag off the path does not exist.
            self.assertEqual(self._call(course, teacher).status_code, 404)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

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
                module_config=_module_config(requests_per_burst=2),
                synapse_config_overrides=_SYNAPSE_CONFIG,
            )
            (_, teacher) = await self._user(config_path, synapse_dir, "teacher")
            (_, other) = await self._user(config_path, synapse_dir, "otherteacher")
            course = self._create_room(teacher, creation_content={"type": "m.space"})
            other_course = self._create_room(
                other, creation_content={"type": "m.space"}
            )
            self.assertEqual(self._call(course, teacher).status_code, 200)
            self.assertEqual(self._call(course, teacher).status_code, 200)
            self.assertEqual(self._call(course, teacher).status_code, 429)
            # Another caller has their own budget.
            self.assertEqual(self._call(other_course, other).status_code, 200)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
