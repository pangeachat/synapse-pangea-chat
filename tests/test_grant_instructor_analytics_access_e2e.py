from urllib.parse import quote

import requests

from .base_e2e import BaseSynapseE2ETest


class TestGrantInstructorAnalyticsAccessE2E(BaseSynapseE2ETest):
    def _endpoint(self) -> str:
        return (
            f"{self.server_url}"
            f"/_synapse/client/pangea/v1/grant_instructor_analytics_access"
        )

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    def _member_state_url(self, room_id: str, user_id: str) -> str:
        room_id_path = quote(room_id, safe="")
        user_id_path = quote(user_id, safe="")
        return (
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}"
            f"/state/m.room.member/{user_id_path}"
        )

    def _create_room_url(self) -> str:
        return f"{self.server_url}/_matrix/client/v3/createRoom"

    def _state_url(self, room_id: str, event_type: str, state_key: str = "") -> str:
        room_id_path = quote(room_id, safe="")
        event_type_path = quote(event_type, safe="")
        state_key_path = quote(state_key, safe="")
        return (
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}"
            f"/state/{event_type_path}/{state_key_path}"
        )

    def _join_url(self, room_id: str) -> str:
        room_id_path = quote(room_id, safe="")
        return f"{self.server_url}/_matrix/client/v3/join/{room_id_path}"

    async def _create_course_space(
        self, instructor_token: str, *, require_analytics_access: bool
    ) -> str:
        response = requests.post(
            self._create_room_url(),
            headers=self._headers(instructor_token),
            json={
                "visibility": "public",
                "preset": "public_chat",
                "creation_content": {"type": "m.space"},
                "initial_state": [
                    {
                        "type": "pangea.course_settings",
                        "state_key": "",
                        "content": {
                            "require_analytics_access": require_analytics_access
                        },
                    }
                ],
                "name": "Test Course",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["room_id"]

    async def _create_analytics_room(self, student_token: str) -> str:
        response = requests.post(
            self._create_room_url(),
            headers=self._headers(student_token),
            json={
                "visibility": "private",
                "preset": "private_chat",
                "creation_content": {
                    "type": "p.analytics",
                    "lang_code": "es",
                },
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["room_id"]

    async def _join_room(self, room_id: str, access_token: str) -> None:
        response = requests.post(
            self._join_url(room_id), headers=self._headers(access_token)
        )
        self.assertEqual(response.status_code, 200, response.text)

    async def _set_user_power_level(
        self, room_id: str, target_user_id: str, power_level: int, admin_token: str
    ) -> None:
        get_response = requests.get(
            self._state_url(room_id, "m.room.power_levels"),
            headers=self._headers(admin_token),
        )
        self.assertEqual(get_response.status_code, 200, get_response.text)
        content = get_response.json()
        users = dict(content.get("users", {}))
        users[target_user_id] = power_level
        content["users"] = users

        put_response = requests.put(
            self._state_url(room_id, "m.room.power_levels"),
            headers=self._headers(admin_token),
            json=content,
        )
        self.assertEqual(put_response.status_code, 200, put_response.text)

    async def test_force_joins_instructor_when_toggle_on(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "teacher", "pw", False)
            await self.register_user(config_path, synapse_dir, "student", "pw", False)
            teacher_user_id, teacher_token = await self.login_user("teacher", "pw")
            _, student_token = await self.login_user("student", "pw")

            course_id = await self._create_course_space(
                teacher_token, require_analytics_access=True
            )
            await self._join_room(course_id, student_token)
            analytics_room_id = await self._create_analytics_room(student_token)

            response = requests.post(
                self._endpoint(),
                headers=self._headers(student_token),
                json={"course_id": course_id, "room_id": analytics_room_id},
            )

            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertEqual(data["course_id"], course_id)
            self.assertEqual(data["room_id"], analytics_room_id)
            self.assertEqual(
                data["instructors_joined"],
                [{"user_id": teacher_user_id, "action": "joined"}],
            )
            self.assertEqual(data["errors"], [])

            membership = requests.get(
                self._member_state_url(analytics_room_id, teacher_user_id),
                headers=self._headers(student_token),
            )
            self.assertEqual(membership.status_code, 200, membership.text)
            self.assertEqual(membership.json()["membership"], "join")
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_returns_already_joined_when_instructor_is_member(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "teacher", "pw", False)
            await self.register_user(config_path, synapse_dir, "student", "pw", False)
            teacher_user_id, teacher_token = await self.login_user("teacher", "pw")
            _, student_token = await self.login_user("student", "pw")

            course_id = await self._create_course_space(
                teacher_token, require_analytics_access=True
            )
            await self._join_room(course_id, student_token)
            analytics_room_id = await self._create_analytics_room(student_token)

            first = requests.post(
                self._endpoint(),
                headers=self._headers(student_token),
                json={"course_id": course_id, "room_id": analytics_room_id},
            )
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(
                first.json()["instructors_joined"],
                [{"user_id": teacher_user_id, "action": "joined"}],
            )

            second = requests.post(
                self._endpoint(),
                headers=self._headers(student_token),
                json={"course_id": course_id, "room_id": analytics_room_id},
            )
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(
                second.json()["instructors_joined"],
                [{"user_id": teacher_user_id, "action": "already_joined"}],
            )
            self.assertEqual(second.json()["errors"], [])
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_403_when_caller_not_in_course(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "teacher", "pw", False)
            await self.register_user(config_path, synapse_dir, "student", "pw", False)
            _, teacher_token = await self.login_user("teacher", "pw")
            _, student_token = await self.login_user("student", "pw")

            course_id = await self._create_course_space(
                teacher_token, require_analytics_access=True
            )
            # Student does NOT join the course.
            analytics_room_id = await self._create_analytics_room(student_token)

            response = requests.post(
                self._endpoint(),
                headers=self._headers(student_token),
                json={"course_id": course_id, "room_id": analytics_room_id},
            )

            self.assertEqual(response.status_code, 403, response.text)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_403_when_toggle_off(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "teacher", "pw", False)
            await self.register_user(config_path, synapse_dir, "student", "pw", False)
            _, teacher_token = await self.login_user("teacher", "pw")
            _, student_token = await self.login_user("student", "pw")

            course_id = await self._create_course_space(
                teacher_token, require_analytics_access=False
            )
            await self._join_room(course_id, student_token)
            analytics_room_id = await self._create_analytics_room(student_token)

            response = requests.post(
                self._endpoint(),
                headers=self._headers(student_token),
                json={"course_id": course_id, "room_id": analytics_room_id},
            )

            self.assertEqual(response.status_code, 403, response.text)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_403_when_target_room_is_not_analytics(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "teacher", "pw", False)
            await self.register_user(config_path, synapse_dir, "student", "pw", False)
            _, teacher_token = await self.login_user("teacher", "pw")
            _, student_token = await self.login_user("student", "pw")

            course_id = await self._create_course_space(
                teacher_token, require_analytics_access=True
            )
            await self._join_room(course_id, student_token)
            non_analytics_room_id = await self.create_private_room(student_token)

            response = requests.post(
                self._endpoint(),
                headers=self._headers(student_token),
                json={"course_id": course_id, "room_id": non_analytics_room_id},
            )

            self.assertEqual(response.status_code, 403, response.text)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_403_when_caller_did_not_create_analytics_room(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "teacher", "pw", False)
            await self.register_user(config_path, synapse_dir, "studentA", "pw", False)
            await self.register_user(config_path, synapse_dir, "studentB", "pw", False)
            _, teacher_token = await self.login_user("teacher", "pw")
            _, student_a_token = await self.login_user("studentA", "pw")
            _, student_b_token = await self.login_user("studentB", "pw")

            course_id = await self._create_course_space(
                teacher_token, require_analytics_access=True
            )
            await self._join_room(course_id, student_a_token)
            await self._join_room(course_id, student_b_token)

            # Student A creates the analytics room; Student B tries to grant.
            analytics_room_id = await self._create_analytics_room(student_a_token)

            response = requests.post(
                self._endpoint(),
                headers=self._headers(student_b_token),
                json={"course_id": course_id, "room_id": analytics_room_id},
            )

            self.assertEqual(response.status_code, 403, response.text)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_grants_only_highest_power_level_cohort(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "teacher", "pw", False)
            await self.register_user(config_path, synapse_dir, "ta", "pw", False)
            await self.register_user(config_path, synapse_dir, "student", "pw", False)
            teacher_user_id, teacher_token = await self.login_user("teacher", "pw")
            ta_user_id, ta_token = await self.login_user("ta", "pw")
            _, student_token = await self.login_user("student", "pw")

            course_id = await self._create_course_space(
                teacher_token, require_analytics_access=True
            )
            await self._join_room(course_id, ta_token)
            await self._set_user_power_level(course_id, ta_user_id, 50, teacher_token)
            await self._join_room(course_id, student_token)
            analytics_room_id = await self._create_analytics_room(student_token)

            response = requests.post(
                self._endpoint(),
                headers=self._headers(student_token),
                json={"course_id": course_id, "room_id": analytics_room_id},
            )

            self.assertEqual(response.status_code, 200, response.text)
            joined = response.json()["instructors_joined"]
            self.assertEqual(joined, [{"user_id": teacher_user_id, "action": "joined"}])

            ta_membership = requests.get(
                self._member_state_url(analytics_room_id, ta_user_id),
                headers=self._headers(student_token),
            )
            # TA should NOT be a member — endpoint state lookup should 404.
            self.assertEqual(ta_membership.status_code, 404, ta_membership.text)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_400_for_invalid_room_ids(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "student", "pw", False)
            _, student_token = await self.login_user("student", "pw")

            missing_course = requests.post(
                self._endpoint(),
                headers=self._headers(student_token),
                json={"room_id": "!something:my.domain.name"},
            )
            self.assertEqual(missing_course.status_code, 400, missing_course.text)

            invalid_course = requests.post(
                self._endpoint(),
                headers=self._headers(student_token),
                json={
                    "course_id": "not-a-room-id",
                    "room_id": "!something:my.domain.name",
                },
            )
            self.assertEqual(invalid_course.status_code, 400, invalid_course.text)

            invalid_room = requests.post(
                self._endpoint(),
                headers=self._headers(student_token),
                json={
                    "course_id": "!something:my.domain.name",
                    "room_id": "not-a-room-id",
                },
            )
            self.assertEqual(invalid_room.status_code, 400, invalid_room.text)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_401_when_unauthenticated(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            response = requests.post(
                self._endpoint(),
                json={
                    "course_id": "!course:my.domain.name",
                    "room_id": "!analytics:my.domain.name",
                },
            )
            self.assertEqual(response.status_code, 401, response.text)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
