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
        await self.homeserver()
        alice_user_id, alice_token = await self.new_user("alice")
        bob_user_id, _ = await self.new_user("bob")

        response = requests.post(
            self._endpoint(),
            headers=self._headers(alice_token),
            json={"user_ids": [alice_user_id, bob_user_id]},
        )

        self.assertEqual(response.status_code, 403)

    async def test_creates_room_and_updates_m_direct_for_both_users(self):
        await self.homeserver()
        _, admin_token = await self.new_user("admin", admin=True)
        alice_user_id, alice_token = await self.new_user("alice")
        bob_user_id, bob_token = await self.new_user("bob")

        response = requests.post(
            self._endpoint(),
            headers=self._headers(admin_token),
            json={"user_ids": [alice_user_id, bob_user_id]},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["created"])
        self.assertFalse(data["reused"])
        self.assertEqual(data["action"], "created_room")
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

    async def test_reuses_existing_direct_room_and_repairs_m_direct(self):
        await self.homeserver()
        _, admin_token = await self.new_user("admin", admin=True)
        alice_user_id, alice_token = await self.new_user("alice")
        bob_user_id, bob_token = await self.new_user("bob")

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

    async def test_rejects_non_local_or_invalid_user_ids(self):
        await self.homeserver()
        _, admin_token = await self.new_user("admin", admin=True)
        alice_user_id, _ = await self.new_user("alice")

        response = requests.post(
            self._endpoint(),
            headers=self._headers(admin_token),
            json={"user_ids": [alice_user_id, "@remote:elsewhere.example"]},
        )

        self.assertEqual(response.status_code, 400)

    async def test_creates_room_with_admin_power_levels(self):
        await self.homeserver()
        _, admin_token = await self.new_user("admin", admin=True)
        alice_user_id, alice_token = await self.new_user("alice")
        bob_user_id, _ = await self.new_user("bob")

        response = requests.post(
            self._endpoint(),
            headers=self._headers(admin_token),
            json={"user_ids": [alice_user_id, bob_user_id]},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["created"])
        # PLs are set at creation time via override — no separate PL event needed
        self.assertFalse(data["power_levels_updated"])
        room_id = data["room_id"]

        pl_response = requests.get(
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels",
            headers=self._headers(alice_token),
        )
        self.assertEqual(pl_response.status_code, 200)
        users_pls = pl_response.json().get("users", {})
        self.assertEqual(users_pls.get(alice_user_id), 100)
        self.assertEqual(users_pls.get(bob_user_id), 100)

    async def test_reused_room_gets_admin_power_levels(self):
        await self.homeserver()
        _, admin_token = await self.new_user("admin", admin=True)
        alice_user_id, alice_token = await self.new_user("alice")
        bob_user_id, bob_token = await self.new_user("bob")

        # Alice creates the DM normally — she gets PL 100, bob stays at 0
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

        # Confirm bob is currently at default PL (not 100)
        pl_before = requests.get(
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels",
            headers=self._headers(alice_token),
        )
        self.assertEqual(pl_before.status_code, 200)
        users_before = pl_before.json().get("users", {})
        self.assertNotEqual(users_before.get(bob_user_id, 0), 100)

        response = requests.post(
            self._endpoint(),
            headers=self._headers(admin_token),
            json={"user_ids": [alice_user_id, bob_user_id]},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["created"])
        self.assertTrue(data["reused"])
        self.assertTrue(data["power_levels_updated"])
        self.assertEqual(data["action"], "repaired_membership_or_power")

        pl_after = requests.get(
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels",
            headers=self._headers(alice_token),
        )
        self.assertEqual(pl_after.status_code, 200)
        users_after = pl_after.json().get("users", {})
        self.assertEqual(users_after.get(alice_user_id), 100)
        self.assertEqual(users_after.get(bob_user_id), 100)
