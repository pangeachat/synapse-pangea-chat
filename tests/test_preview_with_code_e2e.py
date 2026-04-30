import logging
from typing import Any, Dict, Optional

import requests

from .base_e2e import BaseSynapseE2ETest

logger = logging.getLogger(__name__)


PREVIEW_WITH_CODE_URL = (
    "http://localhost:8008/_synapse/client/pangea/v1/preview_with_code"
)


class TestE2EPreviewWithCode(BaseSynapseE2ETest):
    async def _create_course_room(
        self,
        access_token: str,
        name: str = "Spanish 101",
        topic: str = "Beginner Spanish course",
    ) -> str:
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json={
                "visibility": "private",
                "preset": "private_chat",
                "name": name,
                "topic": topic,
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["room_id"]

    async def _set_state_event(
        self,
        room_id: str,
        access_token: str,
        event_type: str,
        content: Dict[str, Any],
        state_key: str = "",
    ) -> str:
        headers = {"Authorization": f"Bearer {access_token}"}
        url = (
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/state/"
            f"{event_type}/{state_key}"
        )
        response = requests.put(url, json=content, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["event_id"]

    async def _set_join_rules_with_code(
        self,
        room_id: str,
        access_token: str,
        access_code: Optional[str] = None,
        admin_access_code: Optional[str] = None,
    ) -> str:
        content: Dict[str, Any] = {"join_rule": "knock"}
        if access_code is not None:
            content["access_code"] = access_code
        if admin_access_code is not None:
            content["admin_access_code"] = admin_access_code
        return await self._set_state_event(
            room_id=room_id,
            access_token=access_token,
            event_type="m.room.join_rules",
            content=content,
        )

    async def _set_power_levels(
        self,
        room_id: str,
        access_token: str,
        users_power_levels: Dict[str, int],
    ) -> str:
        return await self._set_state_event(
            room_id=room_id,
            access_token=access_token,
            event_type="m.room.power_levels",
            content={
                "users": users_power_levels,
                "users_default": 0,
                "events": {},
                "events_default": 0,
                "state_default": 50,
                "ban": 50,
                "kick": 50,
                "redact": 50,
                "invite": 50,
            },
        )

    async def _get_membership(
        self, room_id: str, user_id: str, access_token: str
    ) -> Optional[str]:
        url = (
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/state/"
            f"m.room.member/{user_id}"
        )
        response = requests.get(
            url, headers={"Authorization": f"Bearer {access_token}"}
        )
        if response.status_code == 200:
            return response.json().get("membership")
        return None

    def _post_preview(
        self,
        body: Any,
        access_token: Optional[str] = None,
        send_content_type: bool = True,
    ) -> requests.Response:
        headers: Dict[str, str] = {}
        if access_token is not None:
            headers["Authorization"] = f"Bearer {access_token}"
        if send_content_type:
            headers["Content-Type"] = "application/json"
        return requests.post(PREVIEW_WITH_CODE_URL, json=body, headers=headers)

    async def test_preview_with_code_happy_path(self) -> None:
        """Happy path: returns top-level metadata, PL=100 admins, and pangea state-event bag.
        Also verifies the call has no membership side effects on the caller.
        """
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse()

            for username in ("admin1", "admin2", "ta1", "student"):
                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user=username,
                    password="123123123",
                    admin=True,
                )

            admin1_id, admin1_token = await self.login_user("admin1", "123123123")
            admin2_id, admin2_token = await self.login_user("admin2", "123123123")
            ta1_id, ta1_token = await self.login_user("ta1", "123123123")
            student_id, student_token = await self.login_user("student", "123123123")

            access_code = "abc1234"
            room_id = await self._create_course_room(
                admin1_token,
                name="Spanish 101",
                topic="Beginner Spanish course",
            )

            await self._set_state_event(
                room_id=room_id,
                access_token=admin1_token,
                event_type="m.room.avatar",
                content={"url": "mxc://example.org/courseAvatar"},
            )

            await self.invite_user_to_room(
                room_id=room_id, user_id=admin2_id, access_token=admin1_token
            )
            await self.accept_room_invitation(
                room_id=room_id, access_token=admin2_token
            )
            await self.invite_user_to_room(
                room_id=room_id, user_id=ta1_id, access_token=admin1_token
            )
            await self.accept_room_invitation(room_id=room_id, access_token=ta1_token)

            await self._set_power_levels(
                room_id=room_id,
                access_token=admin1_token,
                users_power_levels={
                    admin1_id: 100,
                    admin2_id: 100,
                    ta1_id: 50,
                },
            )

            await self._set_state_event(
                room_id=room_id,
                access_token=admin1_token,
                event_type="pangea.course_plan",
                content={"uuid": "course-uuid-123"},
            )
            await self._set_state_event(
                room_id=room_id,
                access_token=admin1_token,
                event_type="pangea.teacher_mode",
                content={"enabled": True, "activitiesToUnlockTopic": 3},
            )
            await self._set_state_event(
                room_id=room_id,
                access_token=admin1_token,
                event_type="pangea.analytics_settings",
                content={"blockedConstructs": []},
            )

            await self._set_join_rules_with_code(
                room_id=room_id,
                access_token=admin1_token,
                access_code=access_code,
            )

            self.assertIsNone(
                await self._get_membership(room_id, student_id, admin1_token),
                "Student should not have any membership before the call",
            )

            response = self._post_preview(
                {"access_code": access_code}, access_token=student_token
            )
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertIn("rooms", data)
            self.assertEqual(len(data["rooms"]), 1)

            room = data["rooms"][0]
            self.assertEqual(room["room_id"], room_id)
            self.assertEqual(room["name"], "Spanish 101")
            self.assertEqual(room["topic"], "Beginner Spanish course")
            self.assertEqual(room["avatar_url"], "mxc://example.org/courseAvatar")

            admin_user_ids = {a["user_id"] for a in room["admins"]}
            self.assertEqual(admin_user_ids, {admin1_id, admin2_id})
            self.assertNotIn(ta1_id, admin_user_ids)
            self.assertNotIn(student_id, admin_user_ids)
            for entry in room["admins"]:
                self.assertIn("avatar_url", entry)

            state_events = room["state_events"]
            self.assertIn("pangea.course_plan", state_events)
            self.assertEqual(
                state_events["pangea.course_plan"]["default"]["content"],
                {"uuid": "course-uuid-123"},
            )
            self.assertIn("pangea.teacher_mode", state_events)
            self.assertEqual(
                state_events["pangea.teacher_mode"]["default"]["content"],
                {"enabled": True, "activitiesToUnlockTopic": 3},
            )
            self.assertIn("pangea.analytics_settings", state_events)
            self.assertNotIn(
                "pangea.course_chat_list",
                state_events,
                "Unset pangea types should not appear in the bag",
            )
            self.assertNotIn(
                "m.room.name",
                state_events,
                "m.room.* state events live in top-level fields, not the bag",
            )

            self.assertIsNone(
                await self._get_membership(room_id, student_id, admin1_token),
                "preview_with_code must be read-only — no invite/join side effects",
            )
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_preview_with_code_admin_code_returns_preview(self) -> None:
        """Admin codes resolve to a preview the same as join codes."""
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse()

            for username in ("creator", "viewer"):
                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user=username,
                    password="123123123",
                    admin=True,
                )
            creator_id, creator_token = await self.login_user("creator", "123123123")
            _, viewer_token = await self.login_user("viewer", "123123123")

            admin_code = "admn123"
            room_id = await self._create_course_room(
                creator_token, name="Hidden room", topic="Admin code only"
            )
            await self._set_power_levels(
                room_id=room_id,
                access_token=creator_token,
                users_power_levels={creator_id: 100},
            )
            await self._set_join_rules_with_code(
                room_id=room_id,
                access_token=creator_token,
                admin_access_code=admin_code,
            )

            response = self._post_preview(
                {"access_code": admin_code}, access_token=viewer_token
            )
            self.assertEqual(response.status_code, 200, response.text)
            rooms = response.json()["rooms"]
            self.assertEqual(len(rooms), 1)
            self.assertEqual(rooms[0]["room_id"], room_id)
            self.assertEqual({a["user_id"] for a in rooms[0]["admins"]}, {creator_id})
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_preview_with_code_validation(self) -> None:
        """Validation: bad bodies, missing auth, unknown codes."""
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config={"preview_with_code_requests_per_burst": 100}
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="caller",
                password="123123123",
                admin=True,
            )
            _, caller_token = await self.login_user("caller", "123123123")

            response = self._post_preview({"access_code": "abc1234"})
            self.assertEqual(response.status_code, 403, response.text)

            response = self._post_preview({}, access_token=caller_token)
            self.assertEqual(response.status_code, 400)
            self.assertIn("access_code", response.json()["error"])

            response = self._post_preview(
                {"access_code": 123}, access_token=caller_token
            )
            self.assertEqual(response.status_code, 400)

            for bad in ("short", "toolong1", "abcdefg", "abc!234"):
                response = self._post_preview(
                    {"access_code": bad}, access_token=caller_token
                )
                self.assertEqual(
                    response.status_code,
                    400,
                    f"expected 400 for invalid access code {bad!r}",
                )

            response = self._post_preview(
                {"access_code": "zzz9999"}, access_token=caller_token
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("No rooms found", response.json()["error"])

            response = requests.post(
                PREVIEW_WITH_CODE_URL,
                data="not json",
                headers={
                    "Authorization": f"Bearer {caller_token}",
                    "Content-Type": "text/plain",
                },
            )
            self.assertEqual(response.status_code, 400)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_preview_with_code_rate_limit(self) -> None:
        """Per-user rate limiter returns 429 once the burst is exceeded."""
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

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
                    "preview_with_code_requests_per_burst": 2,
                    "preview_with_code_burst_duration_seconds": 60,
                }
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="caller",
                password="123123123",
                admin=True,
            )
            _, caller_token = await self.login_user("caller", "123123123")

            for i in range(2):
                response = self._post_preview(
                    {"access_code": "zzz9999"}, access_token=caller_token
                )
                self.assertEqual(
                    response.status_code,
                    400,
                    f"request {i} should pass the rate limiter",
                )

            response = self._post_preview(
                {"access_code": "zzz9999"}, access_token=caller_token
            )
            self.assertEqual(
                response.status_code,
                429,
                "third request in burst should be rate limited",
            )
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
