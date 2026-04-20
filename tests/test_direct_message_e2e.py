import requests

from .base_e2e import BaseSynapseE2ETest


class TestEnsureDirectMessageE2E(BaseSynapseE2ETest):
    def _endpoint(self) -> str:
        return f"{self.server_url}/_synapse/client/pangea/v1/ensure_direct_message"

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    def _m_direct_url(self, user_id: str) -> str:
        return (
            f"{self.server_url}/_matrix/client/v3/user/{user_id}/account_data/m.direct"
        )

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
            await self.register_user(config_path, synapse_dir, "alice", "pw", False)
            await self.register_user(config_path, synapse_dir, "bob", "pw", False)
            _, alice_token = await self.login_user("alice", "pw")

            response = requests.post(
                self._endpoint(),
                headers=self._headers(alice_token),
                json={"user_ids": ["@alice:my.domain.name", "@bob:my.domain.name"]},
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

    async def test_creates_room_and_updates_m_direct_for_both_users(self):
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
            await self.register_user(config_path, synapse_dir, "bob", "pw", False)
            _, admin_token = await self.login_user("admin", "pw")
            alice_user_id, alice_token = await self.login_user("alice", "pw")
            bob_user_id, bob_token = await self.login_user("bob", "pw")

            response = requests.post(
                self._endpoint(),
                headers=self._headers(admin_token),
                json={"user_ids": [alice_user_id, bob_user_id]},
            )

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["created"])
            self.assertFalse(data["reused"])
            room_id = data["room_id"]

            alice_joined = requests.get(
                f"{self.server_url}/_matrix/client/v3/joined_rooms",
                headers=self._headers(alice_token),
            )
            self.assertEqual(alice_joined.status_code, 200)
            self.assertIn(room_id, alice_joined.json()["joined_rooms"])

            bob_joined = requests.get(
                f"{self.server_url}/_matrix/client/v3/joined_rooms",
                headers=self._headers(bob_token),
            )
            self.assertEqual(bob_joined.status_code, 200)
            self.assertIn(room_id, bob_joined.json()["joined_rooms"])

            alice_m_direct = requests.get(
                self._m_direct_url(alice_user_id), headers=self._headers(alice_token)
            )
            self.assertEqual(alice_m_direct.status_code, 200)
            self.assertIn(room_id, alice_m_direct.json()[bob_user_id])

            bob_m_direct = requests.get(
                self._m_direct_url(bob_user_id), headers=self._headers(bob_token)
            )
            self.assertEqual(bob_m_direct.status_code, 200)
            self.assertIn(room_id, bob_m_direct.json()[alice_user_id])
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_reuses_existing_direct_room_and_repairs_m_direct(self):
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
            await self.register_user(config_path, synapse_dir, "bob", "pw", False)
            _, admin_token = await self.login_user("admin", "pw")
            alice_user_id, alice_token = await self.login_user("alice", "pw")
            bob_user_id, bob_token = await self.login_user("bob", "pw")

            create_room_response = requests.post(
                f"{self.server_url}/_matrix/client/v3/createRoom",
                headers=self._headers(alice_token),
                json={
                    "preset": "private_chat",
                    "visibility": "private",
                    "invite": [bob_user_id],
                    "is_direct": True,
                },
            )
            self.assertEqual(create_room_response.status_code, 200)
            room_id = create_room_response.json()["room_id"]

            join_response = requests.post(
                f"{self.server_url}/_matrix/client/v3/join/{room_id}",
                headers=self._headers(bob_token),
            )
            self.assertEqual(join_response.status_code, 200)

            clear_alice = requests.put(
                self._m_direct_url(alice_user_id),
                headers=self._headers(alice_token),
                json={},
            )
            self.assertEqual(clear_alice.status_code, 200)
            clear_bob = requests.put(
                self._m_direct_url(bob_user_id),
                headers=self._headers(bob_token),
                json={},
            )
            self.assertEqual(clear_bob.status_code, 200)

            response = requests.post(
                self._endpoint(),
                headers=self._headers(admin_token),
                json={"user_ids": [alice_user_id, bob_user_id]},
            )

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertFalse(data["created"])
            self.assertTrue(data["reused"])
            self.assertEqual(data["room_id"], room_id)
            self.assertCountEqual(
                data["m_direct_updated_for"], [alice_user_id, bob_user_id]
            )

            alice_m_direct = requests.get(
                self._m_direct_url(alice_user_id), headers=self._headers(alice_token)
            )
            self.assertEqual(alice_m_direct.status_code, 200)
            self.assertIn(room_id, alice_m_direct.json()[bob_user_id])

            bob_m_direct = requests.get(
                self._m_direct_url(bob_user_id), headers=self._headers(bob_token)
            )
            self.assertEqual(bob_m_direct.status_code, 200)
            self.assertIn(room_id, bob_m_direct.json()[alice_user_id])
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_rejects_non_local_or_invalid_user_ids(self):
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
                json={"user_ids": [alice_user_id, "@remote:elsewhere.example"]},
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
