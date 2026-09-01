from urllib.parse import quote

import requests

from .base_e2e import BaseSynapseE2ETest


class TestAssignRoomMembershipE2E(BaseSynapseE2ETest):
    def _endpoint(self) -> str:
        return f"{self.server_url}/_synapse/client/pangea/v1/assign_room_membership"

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    def _joined_rooms_url(self) -> str:
        return f"{self.server_url}/_matrix/client/v3/joined_rooms"

    def _member_state_url(self, room_id: str, user_id: str) -> str:
        room_id_path = quote(room_id, safe="")
        user_id_path = quote(user_id, safe="")
        return (
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}"
            f"/state/m.room.member/{user_id_path}"
        )

    def _push_rule_url(self) -> str:
        return (
            f"{self.server_url}"
            f"/_matrix/client/v3/pushrules/global/override/p.rule.analytics_invite"
        )

    def _messages_url(self, room_id: str) -> str:
        room_id_path = quote(room_id, safe="")
        return (
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}"
            f"/messages?dir=b&limit=50"
        )

    async def _create_analytics_room(self, owner_token: str) -> str:
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            headers=self._headers(owner_token),
            json={
                "visibility": "private",
                "preset": "private_chat",
                "creation_content": {"type": "p.analytics", "lang_code": "es"},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["room_id"]

    async def _invite_reason(
        self, room_id: str, user_id: str, reader_token: str
    ) -> str | None:
        response = requests.get(
            self._messages_url(room_id), headers=self._headers(reader_token)
        )
        self.assertEqual(response.status_code, 200, response.text)
        for event in response.json()["chunk"]:
            if (
                event.get("type") == "m.room.member"
                and event.get("state_key") == user_id
                and event.get("content", {}).get("membership") == "invite"
            ):
                return event["content"].get("reason")
        return None

    def _ban_url(self, room_id: str) -> str:
        room_id_path = quote(room_id, safe="")
        return f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/ban"

    async def test_admin_only(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "owner", "pw", False)
            await self.register_user(config_path, synapse_dir, "alice", "pw", False)
            _, owner_token = await self.login_user("owner", "pw")
            alice_user_id, alice_token = await self.login_user("alice", "pw")
            room_id = await self.create_private_room(owner_token)

            response = requests.post(
                self._endpoint(),
                headers=self._headers(alice_token),
                json={
                    "room_id": room_id,
                    "user_ids": [alice_user_id],
                    "force_join": False,
                },
            )

            self.assertEqual(response.status_code, 403)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_invites_users_without_joining_when_force_join_false(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "admin", "pw", True)
            await self.register_user(config_path, synapse_dir, "owner", "pw", False)
            await self.register_user(config_path, synapse_dir, "alice", "pw", False)
            await self.register_user(config_path, synapse_dir, "bob", "pw", False)
            _, admin_token = await self.login_user("admin", "pw")
            _, owner_token = await self.login_user("owner", "pw")
            alice_user_id, alice_token = await self.login_user("alice", "pw")
            bob_user_id, bob_token = await self.login_user("bob", "pw")
            room_id = await self.create_private_room(owner_token)

            response = requests.post(
                self._endpoint(),
                headers=self._headers(admin_token),
                json={
                    "room_id": room_id,
                    "user_ids": [alice_user_id, bob_user_id],
                    "force_join": False,
                },
            )

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["room_id"], room_id)
            self.assertFalse(data["force_join"])
            self.assertEqual(
                data["results"],
                [
                    {
                        "user_id": alice_user_id,
                        "success": True,
                        "action": "invited",
                    },
                    {
                        "user_id": bob_user_id,
                        "success": True,
                        "action": "invited",
                    },
                ],
            )

            alice_member_state = requests.get(
                self._member_state_url(room_id, alice_user_id),
                headers=self._headers(owner_token),
            )
            self.assertEqual(alice_member_state.status_code, 200)
            self.assertEqual(alice_member_state.json()["membership"], "invite")

            bob_member_state = requests.get(
                self._member_state_url(room_id, bob_user_id),
                headers=self._headers(owner_token),
            )
            self.assertEqual(bob_member_state.status_code, 200)
            self.assertEqual(bob_member_state.json()["membership"], "invite")

            alice_joined = requests.get(
                self._joined_rooms_url(), headers=self._headers(alice_token)
            )
            self.assertEqual(alice_joined.status_code, 200)
            self.assertNotIn(room_id, alice_joined.json()["joined_rooms"])

            bob_joined = requests.get(
                self._joined_rooms_url(), headers=self._headers(bob_token)
            )
            self.assertEqual(bob_joined.status_code, 200)
            self.assertNotIn(room_id, bob_joined.json()["joined_rooms"])
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_force_joins_users_when_force_join_true(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "admin", "pw", True)
            await self.register_user(config_path, synapse_dir, "owner", "pw", False)
            await self.register_user(config_path, synapse_dir, "alice", "pw", False)
            await self.register_user(config_path, synapse_dir, "bob", "pw", False)
            _, admin_token = await self.login_user("admin", "pw")
            _, owner_token = await self.login_user("owner", "pw")
            alice_user_id, alice_token = await self.login_user("alice", "pw")
            bob_user_id, bob_token = await self.login_user("bob", "pw")
            room_id = await self.create_private_room(owner_token)

            response = requests.post(
                self._endpoint(),
                headers=self._headers(admin_token),
                json={
                    "room_id": room_id,
                    "user_ids": [alice_user_id, bob_user_id],
                    "force_join": True,
                },
            )

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["force_join"])
            self.assertEqual(
                data["results"],
                [
                    {
                        "user_id": alice_user_id,
                        "success": True,
                        "action": "joined",
                    },
                    {
                        "user_id": bob_user_id,
                        "success": True,
                        "action": "joined",
                    },
                ],
            )

            alice_joined = requests.get(
                self._joined_rooms_url(), headers=self._headers(alice_token)
            )
            self.assertEqual(alice_joined.status_code, 200)
            self.assertIn(room_id, alice_joined.json()["joined_rooms"])

            bob_joined = requests.get(
                self._joined_rooms_url(), headers=self._headers(bob_token)
            )
            self.assertEqual(bob_joined.status_code, 200)
            self.assertIn(room_id, bob_joined.json()["joined_rooms"])
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_force_join_silences_the_invite_for_analytics_rooms_only(self):
        """The bot's retroactive-grant script reaches analytics rooms this way.

        An ordinary room is the control: it must not gain the marker or the rule,
        so the suppression cannot leak into real invites.
        """
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "admin", "pw", True)
            await self.register_user(config_path, synapse_dir, "student", "pw", False)
            await self.register_user(config_path, synapse_dir, "teacher", "pw", False)
            _, admin_token = await self.login_user("admin", "pw")
            _, student_token = await self.login_user("student", "pw")
            teacher_user_id, teacher_token = await self.login_user("teacher", "pw")

            ordinary_room_id = await self.create_private_room(student_token)
            analytics_room_id = await self._create_analytics_room(student_token)

            ordinary = requests.post(
                self._endpoint(),
                headers=self._headers(admin_token),
                json={
                    "room_id": ordinary_room_id,
                    "user_ids": [teacher_user_id],
                    "force_join": True,
                },
            )
            self.assertEqual(ordinary.status_code, 200, ordinary.text)

            self.assertIsNone(
                await self._invite_reason(
                    ordinary_room_id, teacher_user_id, student_token
                )
            )
            control = requests.get(
                self._push_rule_url(), headers=self._headers(teacher_token)
            )
            self.assertEqual(control.status_code, 404, control.text)

            analytics = requests.post(
                self._endpoint(),
                headers=self._headers(admin_token),
                json={
                    "room_id": analytics_room_id,
                    "user_ids": [teacher_user_id],
                    "force_join": True,
                },
            )
            self.assertEqual(analytics.status_code, 200, analytics.text)
            self.assertEqual(
                analytics.json()["results"],
                [{"user_id": teacher_user_id, "success": True, "action": "joined"}],
            )

            self.assertEqual(
                await self._invite_reason(
                    analytics_room_id, teacher_user_id, student_token
                ),
                "p.analytics_request",
            )
            rule_response = requests.get(
                self._push_rule_url(), headers=self._headers(teacher_token)
            )
            self.assertEqual(rule_response.status_code, 200, rule_response.text)
            self.assertEqual(rule_response.json()["actions"], ["dont_notify"])
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_returns_partial_results_when_one_user_is_banned(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "admin", "pw", True)
            await self.register_user(config_path, synapse_dir, "owner", "pw", False)
            await self.register_user(config_path, synapse_dir, "alice", "pw", False)
            await self.register_user(config_path, synapse_dir, "bob", "pw", False)
            _, admin_token = await self.login_user("admin", "pw")
            _, owner_token = await self.login_user("owner", "pw")
            alice_user_id, alice_token = await self.login_user("alice", "pw")
            bob_user_id, bob_token = await self.login_user("bob", "pw")
            room_id = await self.create_private_room(owner_token)

            invited = await self.invite_user_to_room(
                room_id, alice_user_id, owner_token
            )
            self.assertTrue(invited)
            joined = await self.accept_room_invitation(room_id, alice_token)
            self.assertTrue(joined)

            invited_bob = await self.invite_user_to_room(
                room_id, bob_user_id, owner_token
            )
            self.assertTrue(invited_bob)
            ban_response = requests.post(
                self._ban_url(room_id),
                headers=self._headers(owner_token),
                json={"user_id": bob_user_id},
            )
            self.assertEqual(ban_response.status_code, 200)

            banned_member_state = requests.get(
                self._member_state_url(room_id, bob_user_id),
                headers=self._headers(owner_token),
            )
            self.assertEqual(banned_member_state.status_code, 200)
            self.assertEqual(banned_member_state.json()["membership"], "ban")

            response = requests.post(
                self._endpoint(),
                headers=self._headers(admin_token),
                json={
                    "room_id": room_id,
                    "user_ids": [alice_user_id, bob_user_id],
                    "force_join": True,
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["results"],
                [
                    {
                        "user_id": alice_user_id,
                        "success": True,
                        "action": "already_joined",
                    },
                    {
                        "user_id": bob_user_id,
                        "success": False,
                        "action": "failed",
                        "error": "User is banned from room",
                    },
                ],
            )

            alice_joined = requests.get(
                self._joined_rooms_url(), headers=self._headers(alice_token)
            )
            self.assertEqual(alice_joined.status_code, 200)
            self.assertIn(room_id, alice_joined.json()["joined_rooms"])

            bob_joined = requests.get(
                self._joined_rooms_url(), headers=self._headers(bob_token)
            )
            self.assertEqual(bob_joined.status_code, 200)
            self.assertNotIn(room_id, bob_joined.json()["joined_rooms"])
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_rejects_duplicate_and_non_local_user_ids(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "admin", "pw", True)
            await self.register_user(config_path, synapse_dir, "owner", "pw", False)
            await self.register_user(config_path, synapse_dir, "alice", "pw", False)
            _, admin_token = await self.login_user("admin", "pw")
            _, owner_token = await self.login_user("owner", "pw")
            alice_user_id, _ = await self.login_user("alice", "pw")
            room_id = await self.create_private_room(owner_token)

            duplicate_response = requests.post(
                self._endpoint(),
                headers=self._headers(admin_token),
                json={
                    "room_id": room_id,
                    "user_ids": [alice_user_id, alice_user_id],
                    "force_join": False,
                },
            )
            self.assertEqual(duplicate_response.status_code, 400)

            remote_response = requests.post(
                self._endpoint(),
                headers=self._headers(admin_token),
                json={
                    "room_id": room_id,
                    "user_ids": [alice_user_id, "@remote:elsewhere.example"],
                    "force_join": False,
                },
            )
            self.assertEqual(remote_response.status_code, 400)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_returns_404_for_missing_room(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        try:
            await self.register_user(config_path, synapse_dir, "admin", "pw", True)
            await self.register_user(config_path, synapse_dir, "alice", "pw", False)
            _, admin_token = await self.login_user("admin", "pw")
            alice_user_id, _ = await self.login_user("alice", "pw")

            response = requests.post(
                self._endpoint(),
                headers=self._headers(admin_token),
                json={
                    "room_id": "!missing:my.domain.name",
                    "user_ids": [alice_user_id],
                    "force_join": False,
                },
            )

            self.assertEqual(response.status_code, 404)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
