import requests

from synapse_pangea_chat.direct_push.is_rate_limited import request_log

from .base_e2e import BaseSynapseE2ETest


class TestDirectPushE2E(BaseSynapseE2ETest):
    """E2E tests for direct push endpoint."""

    @staticmethod
    def _module_config(
        send_push_sygnal_url: str
        | None = "https://sygnal.example.test/_matrix/push/v1/notify",
    ):
        config = {}
        if send_push_sygnal_url is not None:
            config["send_push_sygnal_url"] = send_push_sygnal_url
        return config

    def setUp(self):
        super().setUp()
        request_log.clear()

    def tearDown(self):
        request_log.clear()
        super().tearDown()

    async def test_send_push_admin_only(self):
        """Non-admin users get 403."""
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse(module_config=self._module_config())

        try:
            await self.register_user(
                config_path, synapse_dir, "alice", "pw", admin=False
            )
            _, token = await self.login_user("alice", "pw")

            response = requests.post(
                f"{self.server_url}/_synapse/client/pangea/v1/send_push",
                json={
                    "user_id": "@alice:my.domain.name",
                    "room_id": "!room:test",
                    "body": "Test",
                },
                headers={"Authorization": f"Bearer {token}"},
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

    async def test_send_push_missing_room_id_is_accepted(self):
        """Missing room_id still accepts the request."""
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse(module_config=self._module_config())

        try:
            await self.register_user(
                config_path, synapse_dir, "admin", "pw", admin=True
            )
            _, admin_token = await self.login_user("admin", "pw")

            response = requests.post(
                f"{self.server_url}/_synapse/client/pangea/v1/send_push",
                json={"user_id": "@alice:my.domain.name", "body": "Test"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["attempted"], 0)
            self.assertEqual(data["sent"], 0)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_send_push_no_pushers(self):
        """User with no pushers returns 200 with attempted: 0."""
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse(module_config=self._module_config())

        try:
            await self.register_user(
                config_path, synapse_dir, "alice", "pw", admin=False
            )
            await self.register_user(
                config_path, synapse_dir, "admin", "pw", admin=True
            )
            _, admin_token = await self.login_user("admin", "pw")

            response = requests.post(
                f"{self.server_url}/_synapse/client/pangea/v1/send_push",
                json={
                    "user_id": "@alice:my.domain.name",
                    "room_id": "!room:test",
                    "body": "Test",
                },
                headers={"Authorization": f"Bearer {admin_token}"},
            )

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["attempted"], 0)
            self.assertEqual(data["sent"], 0)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_send_push_with_real_http_pusher_does_not_500(self):
        """A stored HTTP pusher should not crash direct push lookup."""
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse(module_config=self._module_config())

        try:
            await self.register_user(
                config_path, synapse_dir, "alice", "pw", admin=False
            )
            await self.register_user(
                config_path, synapse_dir, "admin", "pw", admin=True
            )
            _, alice_token = await self.login_user("alice", "pw")
            _, admin_token = await self.login_user("admin", "pw")

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
                    "user_id": "@alice:my.domain.name",
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
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_send_push_rate_limit(self):
        """Admin users are rate limited after 10 requests."""
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse(module_config=self._module_config())

        try:
            await self.register_user(
                config_path, synapse_dir, "admin", "pw", admin=True
            )
            _, admin_token = await self.login_user("admin", "pw")

            for i in range(10):
                response = requests.post(
                    f"{self.server_url}/_synapse/client/pangea/v1/send_push",
                    json={
                        "user_id": "@alice:my.domain.name",
                        "room_id": "!room:test",
                        "body": f"Test {i}",
                    },
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                self.assertEqual(response.status_code, 200)

            response = requests.post(
                f"{self.server_url}/_synapse/client/pangea/v1/send_push",
                json={
                    "user_id": "@alice:my.domain.name",
                    "room_id": "!room:test",
                    "body": "Rate limited",
                },
                headers={"Authorization": f"Bearer {admin_token}"},
            )

            self.assertEqual(response.status_code, 429)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_send_push_requires_sygnal_url_config(self):
        """Configured pushers return an explicit 500 when Sygnal URL is unset."""
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse(module_config=self._module_config(None))

        try:
            await self.register_user(
                config_path, synapse_dir, "alice", "pw", admin=False
            )
            await self.register_user(
                config_path, synapse_dir, "admin", "pw", admin=True
            )
            _, alice_token = await self.login_user("alice", "pw")
            _, admin_token = await self.login_user("admin", "pw")

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
                    "user_id": "@alice:my.domain.name",
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
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
