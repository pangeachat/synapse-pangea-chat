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
        Test that knocking on a room via the standard Matrix knock API
        auto-invites and auto-joins the knocker when all admins have left.

        Scenario:
        1. User1 (admin, power level 100) creates a knock room
        2. User2 (non-admin, power level 0) is invited and joins
        3. User1 sets power levels (invite requires 50)
        4. User1 (only admin) leaves the room
        5. User3 knocks on the room via standard Matrix /knock API
        6. Expected: module auto-promotes User2, auto-invites User3,
           and auto-accepts the invite so User3 ends up joined
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

            # Wait for User3 to be auto-joined
            # The module should: detect knock -> promote User2 -> invite User3
            # -> detect invite replaces knock -> auto-join User3
            is_joined = await self.wait_for_membership(
                room_id=room_id,
                user_id=user_ids["user3"],
                access_token=tokens["user2"],
                expected_membership="join",
                max_wait=10,
            )
            self.assertTrue(
                is_joined,
                "User3 should be auto-joined after knocking. "
                "Expected: module promotes User2, invites User3, "
                "and auto-accepts the invite.",
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def create_knock_restricted_room(
        self, access_token: str, parent_room_id: str
    ) -> str:
        """Create a room with knock_restricted join rules allowing members of parent_room_id."""
        headers = {"Authorization": f"Bearer {access_token}"}
        create_room_url = f"http://localhost:8008/_matrix/client/v3/createRoom"
        create_room_data = {
            "visibility": "private",
            "preset": "private_chat",
            "room_version": "10",
            "initial_state": [
                {
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "content": {
                        "join_rule": "knock_restricted",
                        "allow": [
                            {
                                "type": "m.room_membership",
                                "room_id": parent_room_id,
                            },
                        ],
                    },
                }
            ],
        }
        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["room_id"]

    async def get_room_power_levels(self, room_id: str, access_token: str) -> dict:
        """Get current power levels for a room."""
        url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def create_space(self, access_token: str) -> str:
        """Create a space (used as the parent for knock_restricted rooms)."""
        headers = {"Authorization": f"Bearer {access_token}"}
        create_room_url = f"http://localhost:8008/_matrix/client/v3/createRoom"
        create_room_data = {
            "visibility": "private",
            "preset": "private_chat",
            "creation_content": {"type": "m.space"},
        }
        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["room_id"]

    async def test_promote_on_leave_knock_restricted(self):
        """
        Test that when the last admin leaves a knock_restricted room,
        a remaining user is promoted to have invite power, allowing
        restricted joins to succeed.

        Scenario:
        1. User1 (admin, power level 100) creates a space and a
           knock_restricted child room
        2. User2 (non-admin, power level 0) is invited and joins both
        3. Power levels set: user1=100, user2=0, invite requires 50
        4. User1 leaves the child room
        5. Expected: module detects leave in knock_restricted room,
           promotes User2 to power level >= 50
        6. User3 (member of parent space) can join child room via
           restricted join
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

            # User1 creates a parent space
            space_id = await self.create_space(tokens["user1"])

            # User1 creates a knock_restricted child room
            room_id = await self.create_knock_restricted_room(tokens["user1"], space_id)

            # User1 invites User2 to space and child room
            for target_room in [space_id, room_id]:
                invited = await self.invite_user_to_room(
                    target_room, user_ids["user2"], tokens["user1"]
                )
                self.assertTrue(invited)
                accepted = await self.accept_room_invitation(
                    target_room, tokens["user2"]
                )
                self.assertTrue(accepted)

            # User1 invites User3 to space only (so they can do restricted join later)
            invited = await self.invite_user_to_room(
                space_id, user_ids["user3"], tokens["user1"]
            )
            self.assertTrue(invited)
            accepted = await self.accept_room_invitation(space_id, tokens["user3"])
            self.assertTrue(accepted)

            # Set power levels in child room: user1=100, user2=0, invite requires 50
            await self.set_room_power_levels(
                room_id=room_id,
                access_token=tokens["user1"],
                user_power_levels={
                    user_ids["user1"]: 100,
                    user_ids["user2"]: 0,
                },
            )

            # User1 (the only admin) leaves the child room
            await self.leave_room(room_id=room_id, access_token=tokens["user1"])

            # Wait for background process to complete
            await asyncio.sleep(3)

            # Verify that User2 has been promoted to have invite power (>= 50)
            power_levels = await self.get_room_power_levels(room_id, tokens["user2"])
            user2_power = power_levels.get("users", {}).get(user_ids["user2"], 0)
            self.assertGreaterEqual(
                user2_power,
                50,
                f"User2 should have been promoted to power level >= 50, "
                f"but has {user2_power}",
            )

            # User3 (member of parent space) should now be able to do a
            # restricted join on the child room
            join_url = f"http://localhost:8008/_matrix/client/v3/join/{room_id}"
            response = requests.post(
                join_url,
                headers={"Authorization": f"Bearer {tokens['user3']}"},
                timeout=10,
            )
            self.assertEqual(
                response.status_code,
                200,
                f"User3 should be able to join knock_restricted room via "
                f"restricted join after admin left. Response: {response.text}",
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
