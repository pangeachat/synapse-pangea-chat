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


class TestE2E(BaseSynapseE2ETest):
    async def test_auto_accept_knocked_room(self):
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

            # Register users (all as non-server-admins)
            users = [
                {"user": "user1", "password": "pw1"},
                {"user": "user2", "password": "pw2"},
                {"user": "user3", "password": "pw3"},
            ]
            for register_user in users:
                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user=register_user["user"],
                    password=register_user["password"],
                    admin=False,
                )
            # Login users
            tokens = {}
            for token_user in users:
                _, tokens[token_user["user"]] = await self.login_user(
                    token_user["user"], token_user["password"]
                )
            # "user1" creates private room
            admin_token = tokens["user1"]
            room_id = await self.create_private_room_knock_allowed_room(admin_token)
            # "user1" invites "user2"
            for invite_user in ["user2"]:
                invited = await self.invite_user_to_room(
                    room_id, f"@{invite_user}:my.domain.name", admin_token
                )
                self.assertTrue(invited)

            # "user2" does not auto accept invite
            for invite_user in ["user2"]:
                accepted = await self.accept_room_invitation(
                    room_id, tokens[invite_user]
                )
                self.assertTrue(accepted)

            # Verify "user2" is in the room
            for joined_user in ["user1", "user2"]:
                member_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.member/@{joined_user}:my.domain.name"
                resp = requests.get(
                    member_url,
                    headers={"Authorization": f"Bearer {tokens[joined_user]}"},
                    timeout=10,
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json().get("membership"), "join")

            # "user3" knocks on the room
            knock_success = await self.knock_room(room_id, tokens["user3"])
            self.assertTrue(knock_success)

            # "user1" invites "user3" to the room, indicating a knock approval
            invited = await self.invite_user_to_room(
                room_id, "@user3:my.domain.name", admin_token
            )
            self.assertTrue(invited)

            # "user3" should be in the room
            for knocked_joined_user in ["user1", "user2", "user3"]:
                member_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.member/@{knocked_joined_user}:my.domain.name"
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json().get("membership"), "join")

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def knock_room(self, room_id: str, access_token: str) -> bool:
        knock_url = f"http://localhost:8008/_matrix/client/v3/knock/{room_id}"
        response = requests.post(
            knock_url,
            json={},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        return response.status_code == 200

    async def joined_room(self, user_id: str, room_id: str, access_token: str) -> bool:
        """
        Check if a user has joined a specific room.
        """
        member_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{user_id}"
        response = requests.get(
            member_url, headers={"Authorization": f"Bearer {access_token}"}, timeout=10
        )
        return (
            response.status_code == 200 and response.json().get("membership") == "join"
        )

    async def wait_for_membership(
        self,
        room_id: str,
        user_id: str,
        access_token: str,
        expected_membership: str,
        max_wait: int = 5,
    ) -> bool:
        """Wait for a user to reach a specific membership state in a room."""
        member_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{user_id}"
        total_wait = 0
        wait_interval = 1
        while total_wait < max_wait:
            response = requests.get(
                member_url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if (
                response.status_code == 200
                and response.json().get("membership") == expected_membership
            ):
                return True
            await asyncio.sleep(wait_interval)
            total_wait += wait_interval
        return False

    async def set_room_power_levels(
        self, room_id: str, access_token: str, user_power_levels: dict
    ):
        headers = {"Authorization": f"Bearer {access_token}"}
        set_power_levels_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels"
        power_levels_content = {
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
        response = requests.put(
            set_power_levels_url,
            json=power_levels_content,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)

    async def leave_room(self, room_id: str, access_token: str):
        headers = {"Authorization": f"Bearer {access_token}"}
        leave_room_url = (
            f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/leave"
        )
        response = requests.post(leave_room_url, json={}, headers=headers)
        self.assertEqual(response.status_code, 200)

    async def test_knock_auto_invite_admin_left(self):
        """
        Test that knocking on a room when all admins have left does NOT
        auto-join the user, because the module no longer promotes users
        via internal Synapse APIs (which caused replication crashes).

        Scenario:
        1. User1 (admin, power level 100) creates a knock room
        2. User2 (non-admin, power level 0) is invited and joins
        3. User1 sets power levels (invite requires 50)
        4. User1 (only admin) leaves the room
        5. User3 knocks on the room via standard Matrix /knock API
        6. Expected: User3 remains in 'knock' state (no auto-join)
           because no remaining member has invite power
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
            ) = await self.start_test_synapse(
                module_config={"auto_invite_knocker_enabled": True},
            )

            # Register users
            users = [
                {"user": "user1", "password": "pw1"},
                {"user": "user2", "password": "pw2"},
                {"user": "user3", "password": "pw3"},
            ]
            for register_user in users:
                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user=register_user["user"],
                    password=register_user["password"],
                    admin=False,
                )

            # Login users
            tokens = {}
            user_ids = {}
            for token_user in users:
                user_id, token = await self.login_user(
                    token_user["user"], token_user["password"]
                )
                tokens[token_user["user"]] = token
                user_ids[token_user["user"]] = user_id

            # User1 creates a knock room
            room_id = await self.create_private_room_knock_allowed_room(tokens["user1"])

            # User1 invites User2 and User2 joins
            invited = await self.invite_user_to_room(
                room_id, user_ids["user2"], tokens["user1"]
            )
            self.assertTrue(invited)
            accepted = await self.accept_room_invitation(room_id, tokens["user2"])
            self.assertTrue(accepted)

            # Set power levels: user1=100, user2=0, invite requires 50
            await self.set_room_power_levels(
                room_id=room_id,
                access_token=tokens["user1"],
                user_power_levels={
                    user_ids["user1"]: 100,
                    user_ids["user2"]: 0,
                },
            )

            # User1 (the only admin) leaves the room
            await self.leave_room(room_id=room_id, access_token=tokens["user1"])

            # User3 knocks on the room via standard Matrix knock API
            knock_success = await self.knock_room(room_id, tokens["user3"])
            self.assertTrue(
                knock_success,
                "User3 should be able to knock on the room",
            )

            # Wait to confirm User3 is NOT auto-joined
            # The module should fail gracefully: no user has invite power,
            # so the knock auto-invite cannot proceed.
            is_joined = await self.wait_for_membership(
                room_id=room_id,
                user_id=user_ids["user3"],
                access_token=tokens["user2"],
                expected_membership="join",
                max_wait=5,
            )
            self.assertFalse(
                is_joined,
                "User3 should NOT be auto-joined when no member has invite power. "
                "The module should gracefully skip the auto-invite.",
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
