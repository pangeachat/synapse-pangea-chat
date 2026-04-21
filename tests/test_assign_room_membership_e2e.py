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
