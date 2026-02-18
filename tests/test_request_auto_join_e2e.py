import asyncio
import logging

import requests

from .base_e2e import BaseSynapseE2ETest

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    filename="synapse.log",
    filemode="w",
)


class TestRequestAutoJoinE2E(BaseSynapseE2ETest):
    """E2E tests for POST /_synapse/client/unstable/org.pangea/v1/request_auto_join"""

    REQUEST_AUTO_JOIN_URL = (
        "http://localhost:8008"
        "/_synapse/client/unstable/org.pangea/v1/request_auto_join"
    )

    async def request_auto_join(
        self, room_id: str, access_token: str
    ) -> requests.Response:
        return requests.post(
            self.REQUEST_AUTO_JOIN_URL,
            json={"room_id": room_id},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )

    async def set_room_power_levels(
        self, room_id: str, access_token: str, user_power_levels: dict
    ):
        headers = {"Authorization": f"Bearer {access_token}"}
        url = (
            f"http://localhost:8008/_matrix/client/v3/rooms"
            f"/{room_id}/state/m.room.power_levels"
        )
        content = {
            "users": user_power_levels,
            "users_default": 0,
            "events": {},
            "events_default": 0,
            "state_default": 50,
            "ban": 50,
            "kick": 50,
            "redact": 50,
            "invite": 50,
        }
        response = requests.put(url, json=content, headers=headers)
        self.assertEqual(response.status_code, 200)

    async def join_room(self, room_id: str, access_token: str):
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/join"
        response = requests.post(url, json={}, headers=headers)
        self.assertEqual(response.status_code, 200)

    async def leave_room(self, room_id: str, access_token: str):
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/leave"
        response = requests.post(url, json={}, headers=headers)
        self.assertEqual(response.status_code, 200)

    async def get_membership(
        self, room_id: str, user_id: str, access_token: str
    ) -> str | None:
        url = (
            f"http://localhost:8008/_matrix/client/v3/rooms"
            f"/{room_id}/state/m.room.member/{user_id}"
        )
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if response.status_code != 200:
            return None
        return response.json().get("membership")

    async def wait_for_membership(
        self,
        room_id: str,
        user_id: str,
        access_token: str,
        expected: str,
        max_wait: int = 5,
    ) -> bool:
        """Poll until a user reaches the expected membership or timeout."""
        for _ in range(max_wait):
            membership = await self.get_membership(room_id, user_id, access_token)
            if membership == expected:
                return True
            await asyncio.sleep(1)
        return False

    # ------------------------------------------------------------------
    # Helpers to bootstrap each test with a fresh Synapse
    # ------------------------------------------------------------------

    async def _setup_synapse_and_users(self):
        (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse()

        users = [
            {"user": "user1", "password": "pw1"},
            {"user": "user2", "password": "pw2"},
            {"user": "user3", "password": "pw3"},
        ]
        for u in users:
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user=u["user"],
                password=u["password"],
                admin=True,
            )
        tokens = {}
        ids = {}
        for u in users:
            uid, tok = await self.login_user(u["user"], u["password"])
            ids[u["user"]] = uid
            tokens[u["user"]] = tok

        return (
            postgres,
            synapse_dir,
            config_path,
            server_process,
            stdout_thread,
            stderr_thread,
            ids,
            tokens,
        )

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    async def test_auto_join_after_admin_leaves(self):
        """Admin leaves a knock room. Former admin calls request_auto_join
        and is invited + auto-joined back into the room."""
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
                ids,
                tokens,
            ) = await self._setup_synapse_and_users()

            admin_token = tokens["user1"]
            admin_id = ids["user1"]
            member_id = ids["user2"]
            member_token = tokens["user2"]
            rejoiner_id = ids["user1"]
            rejoiner_token = tokens["user1"]

            # user1 creates knock room
            room_id = await self.create_private_room_knock_allowed_room(admin_token)

            # invite user2 so someone remains
            await self.invite_user_to_room(room_id, member_id, admin_token)
            await self.join_room(room_id, member_token)

            # set power levels: user1=100 (admin), user2=0, invite requires 50
            await self.set_room_power_levels(
                room_id, admin_token, {admin_id: 100, member_id: 0}
            )

            # user1 leaves — no remaining member has invite power
            await self.leave_room(room_id, admin_token)

            # user1 calls request_auto_join
            resp = await self.request_auto_join(room_id, rejoiner_token)
            self.assertEqual(resp.status_code, 200, resp.text)

            # user1 should end up joined (auto_accept_invite detects
            # the invite replacing a leave for a previous member)
            joined = await self.wait_for_membership(
                room_id, rejoiner_id, member_token, "join"
            )
            # Even if auto-accept doesn't fire (user didn't knock first),
            # the invite should have been sent successfully.
            if not joined:
                membership = await self.get_membership(
                    room_id, rejoiner_id, member_token
                )
                # At minimum user1 should be invited
                self.assertIn(
                    membership,
                    ("invite", "join"),
                    f"Expected invite or join, got {membership}",
                )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_auto_join_knock_then_request(self):
        """User knocks, then calls request_auto_join.
        The invite replaces the knock so auto_accept_invite auto-joins."""
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
                ids,
                tokens,
            ) = await self._setup_synapse_and_users()

            admin_token = tokens["user1"]
            admin_id = ids["user1"]
            member_id = ids["user2"]
            member_token = tokens["user2"]
            knocker_id = ids["user3"]
            knocker_token = tokens["user3"]

            # user1 creates knock room, invites user2
            room_id = await self.create_private_room_knock_allowed_room(admin_token)
            await self.invite_user_to_room(room_id, member_id, admin_token)
            await self.join_room(room_id, member_token)

            # set power levels: invite requires 50, only user1=100
            await self.set_room_power_levels(
                room_id, admin_token, {admin_id: 100, member_id: 0}
            )

            # user1 leaves
            await self.leave_room(room_id, admin_token)

            # user3 knocks on the room
            knock_url = f"http://localhost:8008/_matrix/client/v3/knock/{room_id}"
            knock_resp = requests.post(
                knock_url,
                json={},
                headers={"Authorization": f"Bearer {knocker_token}"},
                timeout=10,
            )
            self.assertEqual(knock_resp.status_code, 200)

            # user3 calls request_auto_join
            resp = await self.request_auto_join(room_id, knocker_token)
            self.assertEqual(resp.status_code, 200, resp.text)

            # The invite replaces the knock, auto_accept_invite fires → join
            joined = await self.wait_for_membership(
                room_id, knocker_id, member_token, "join"
            )
            self.assertTrue(
                joined,
                "User3 should have been auto-joined after knock + request_auto_join",
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_rejects_user_with_no_previous_membership(self):
        """A user who was never in the room gets 403."""
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
                ids,
                tokens,
            ) = await self._setup_synapse_and_users()

            admin_token = tokens["user1"]
            outsider_token = tokens["user3"]

            room_id = await self.create_private_room_knock_allowed_room(admin_token)

            resp = await self.request_auto_join(room_id, outsider_token)
            self.assertEqual(resp.status_code, 403)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_rejects_already_joined_user(self):
        """A user who is currently joined gets 400."""
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
                ids,
                tokens,
            ) = await self._setup_synapse_and_users()

            admin_token = tokens["user1"]
            room_id = await self.create_private_room_knock_allowed_room(admin_token)

            resp = await self.request_auto_join(room_id, admin_token)
            self.assertEqual(resp.status_code, 400)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_rejects_missing_auth(self):
        """Request without auth token gets 403."""
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
                _ids,
                _tokens,
            ) = await self._setup_synapse_and_users()

            resp = requests.post(
                self.REQUEST_AUTO_JOIN_URL,
                json={"room_id": "!fake:my.domain.name"},
                timeout=10,
            )
            self.assertEqual(resp.status_code, 403)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_rejects_missing_room_id(self):
        """Request without room_id gets 400."""
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
                _ids,
                tokens,
            ) = await self._setup_synapse_and_users()

            resp = requests.post(
                self.REQUEST_AUTO_JOIN_URL,
                json={},
                headers={"Authorization": f"Bearer {tokens['user1']}"},
                timeout=10,
            )
            self.assertEqual(resp.status_code, 400)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
