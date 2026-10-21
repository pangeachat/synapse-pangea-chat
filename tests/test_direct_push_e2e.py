import requests

from .base_e2e import SERVER_NAME, BaseSynapseE2ETest
from .recording_sygnal import RecordingSygnal

SYGNAL_URL = "https://sygnal.example.test/_matrix/push/v1/notify"


class TestDirectPushE2E(BaseSynapseE2ETest):
    """E2E tests for direct push endpoint.

    The send_push limit is kept per requesting admin, and each test registers
    its own admin, so no test can spend another's allowance.
    """

    async def test_send_push_admin_only(self):
        """Non-admin users get 403."""
        await self.homeserver(module_config={"send_push_sygnal_url": SYGNAL_URL})
        alice_id, token = await self.new_user("alice")

        response = requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/send_push",
            json={
                "user_id": alice_id,
                "room_id": "!room:test",
                "body": "Test",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 403)

    async def test_send_push_missing_room_id_is_accepted(self):
        """Missing room_id still accepts the request."""
        await self.homeserver(module_config={"send_push_sygnal_url": SYGNAL_URL})
        _, admin_token = await self.new_user("admin", admin=True)

        response = requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/send_push",
            json={
                "user_id": f"@{self.localpart('alice')}:{SERVER_NAME}",
                "body": "Test",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["attempted"], 0)
        self.assertEqual(data["sent"], 0)

    async def test_send_push_no_pushers(self):
        """User with no pushers returns 200 with attempted: 0."""
        await self.homeserver(module_config={"send_push_sygnal_url": SYGNAL_URL})
        alice_id, _ = await self.new_user("alice")
        _, admin_token = await self.new_user("admin", admin=True)

        response = requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/send_push",
            json={
                "user_id": alice_id,
                "room_id": "!room:test",
                "body": "Test",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["attempted"], 0)
        self.assertEqual(data["sent"], 0)

    async def test_send_push_with_real_http_pusher_does_not_500(self):
        """A stored HTTP pusher should not crash direct push lookup."""
        await self.homeserver(module_config={"send_push_sygnal_url": SYGNAL_URL})
        alice_id, alice_token = await self.new_user("alice")
        _, admin_token = await self.new_user("admin", admin=True)

        pusher_response = requests.post(
            f"{self.server_url}/_matrix/client/v3/pushers/set",
            json={
                "kind": "http",
                "app_id": "com.talktolearn.chat",
                "app_display_name": "Pangea Chat",
                "device_display_name": "Test iPhone",
                "pushkey": "pushkey-1",
                "lang": "en",
                "data": {
                    "url": "https://sygnal.staging.pangea.chat/_matrix/push/v1/notify"
                },
            },
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        self.assertEqual(pusher_response.status_code, 200)

        response = requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/send_push",
            json={
                "user_id": alice_id,
                "room_id": "!room:test",
                "body": "Test",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["attempted"], 1)
        self.assertIn("devices", data)
        self.assertIn(
            "pushkey-1", [device["pushkey"] for device in data["devices"].values()]
        )

    async def test_send_push_rate_limit(self):
        """Admin users are rate limited after 10 requests."""
        await self.homeserver(module_config={"send_push_sygnal_url": SYGNAL_URL})
        _, admin_token = await self.new_user("admin", admin=True)
        target_id = f"@{self.localpart('alice')}:{SERVER_NAME}"

        for i in range(10):
            response = requests.post(
                f"{self.server_url}/_synapse/client/pangea/v1/send_push",
                json={
                    "user_id": target_id,
                    "room_id": "!room:test",
                    "body": f"Test {i}",
                },
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            self.assertEqual(response.status_code, 200)

        response = requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/send_push",
            json={
                "user_id": target_id,
                "room_id": "!room:test",
                "body": "Rate limited",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        self.assertEqual(response.status_code, 429)


class TestDirectPushWithoutSygnalUrlE2E(BaseSynapseE2ETest):
    """Direct push on a homeserver whose module has no Sygnal URL configured."""

    async def test_send_push_requires_sygnal_url_config(self):
        """Configured pushers return an explicit 500 when Sygnal URL is unset."""
        await self.homeserver()
        alice_id, alice_token = await self.new_user("alice")
        _, admin_token = await self.new_user("admin", admin=True)

        pusher_response = requests.post(
            f"{self.server_url}/_matrix/client/v3/pushers/set",
            json={
                "kind": "http",
                "app_id": "com.talktolearn.chat",
                "app_display_name": "Pangea Chat",
                "device_display_name": "Test iPhone",
                "pushkey": "pushkey-1",
                "lang": "en",
                "data": {
                    "url": "https://sygnal.staging.pangea.chat/_matrix/push/v1/notify"
                },
            },
            headers={"Authorization": f"Bearer {alice_token}"},
        )
        self.assertEqual(pusher_response.status_code, 200)

        response = requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/send_push",
            json={
                "user_id": alice_id,
                "room_id": "!room:test",
                "body": "Test",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {"error": "send_push_sygnal_url is not configured"},
        )


class TestDirectPushSygnalChoiceE2E(BaseSynapseE2ETest):
    """Which Sygnal a push goes to (notice-delivery.instructions.md, "Which
    Sygnal"): the one the device registered when it is allowed, else the
    default."""

    default_sygnal: RecordingSygnal
    other_sygnal: RecordingSygnal

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.default_sygnal = RecordingSygnal().start()
        cls.addClassCleanup(cls.default_sygnal.stop)
        cls.other_sygnal = RecordingSygnal().start()
        cls.addClassCleanup(cls.other_sygnal.stop)

    async def test_each_device_is_pushed_through_its_own_allowed_sygnal(self):
        await self.homeserver(
            module_config={
                "send_push_sygnal_url": self.default_sygnal.url,
                "send_push_allowed_sygnal_urls": [self.other_sygnal.url],
            }
        )
        alice_id, alice_token = await self.new_user("alice")
        admin_id, admin_token = await self.new_user("admin", admin=True)
        switched_key = f"switched-{self.tag}"
        unlisted_key = f"unlisted-{self.tag}"

        for pushkey, url in (
            (switched_key, self.other_sygnal.url),
            (unlisted_key, "https://sygnal.unlisted.test/_matrix/push/v1/notify"),
        ):
            pusher_response = requests.post(
                f"{self.server_url}/_matrix/client/v3/pushers/set",
                json={
                    "kind": "http",
                    "app_id": "com.talktolearn.chat",
                    "app_display_name": "Pangea Chat",
                    "device_display_name": "Test iPhone",
                    "pushkey": pushkey,
                    "lang": "en",
                    "data": {"url": url},
                },
                headers={"Authorization": f"Bearer {alice_token}"},
            )
            self.assertEqual(pusher_response.status_code, 200)

        response = requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/send_push",
            json={"user_id": alice_id, "body": "Test"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sent"], 2)
        self.assertEqual(len(self.other_sygnal.wait_for(switched_key, 1, 5)), 1)
        self.assertEqual(len(self.default_sygnal.wait_for(unlisted_key, 1, 5)), 1)
        self.assertEqual(self.default_sygnal.received(switched_key), [])
        self.assertEqual(self.other_sygnal.received(unlisted_key), [])
        # The push is sent as the admin that asked for it, which the client
        # checks against its environment's bot before following a notice path.
        self.assertEqual(
            self.other_sygnal.notifications(switched_key)[0]["sender"], admin_id
        )
