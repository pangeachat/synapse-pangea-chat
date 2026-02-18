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
