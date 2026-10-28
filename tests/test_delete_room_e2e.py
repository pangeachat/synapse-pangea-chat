import asyncio
import logging
from typing import Any, cast

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
    async def test_delete_room(self):
        await self.homeserver()

        # Register users (all as non-server-admins)
        user_ids = {}
        tokens = {}
        for name in ["roomadmin", "user2", "user3", "anotheradmin"]:
            user_ids[name], tokens[name] = await self.new_user(name)
        # "roomadmin" creates private room
        admin_token = tokens["roomadmin"]
        room_id = await self.create_private_room(admin_token)
        # "roomadmin" invites others
        for invite_user in ["user2", "user3"]:
            invited = await self.invite_user_to_room(
                room_id, user_ids[invite_user], admin_token
            )
            self.assertTrue(invited)
        # Others accept invite
        for invite_user in ["user2", "user3"]:
            accepted = await self.accept_room_invitation(room_id, tokens[invite_user])
            self.assertTrue(accepted)

        # Verify all users are in the room
        for joined_user in ["roomadmin", "user2", "user3"]:
            member_url = f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{user_ids[joined_user]}"
            resp = requests.get(
                member_url,
                headers={"Authorization": f"Bearer {tokens[joined_user]}"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json().get("membership"), "join")

        # Non-room-admin tries to delete (should fail)
        delete_url = f"{self.server_url}/_synapse/client/pangea/v1/delete_room"
        response = requests.post(
            delete_url,
            json={"room_id": room_id},
            headers={"Authorization": f"Bearer {tokens['user2']}"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("highest power level", response.json().get("error", ""))
        # Room creator deletes (should succeed)
        response = requests.post(
            delete_url,
            json={"room_id": room_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Deleted")
        # Assert other users are no longer in the room
        for remove_user in ["user2", "user3"]:
            resp = requests.get(
                f"{self.server_url}/_matrix/client/v3/joined_rooms",
                headers={"Authorization": f"Bearer {tokens[remove_user]}"},
            )
            self.assertEqual(resp.status_code, 200)
            joined_rooms = resp.json().get("joined_rooms", [])
            self.assertEqual(len(joined_rooms), 0)

        # Assert the leave events carry the room-deleted marker so clients
        # can tell deletion apart from a voluntary leave (client#8237)
        for remove_user in ["user2", "user3"]:
            member_url = f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{user_ids[remove_user]}"
            resp = requests.get(
                member_url,
                headers={"Authorization": f"Bearer {tokens[remove_user]}"},
            )
            self.assertEqual(resp.status_code, 200)
            content = resp.json()
            self.assertEqual(content.get("membership"), "leave")
            self.assertTrue(content.get("pangea.room_deleted"))
            self.assertEqual(content.get("reason"), "This space has been deleted")

        # "roomadmin" creates another private room and invite anotheradmin
        room_id = await self.create_private_room(admin_token)
        for admin in ["anotheradmin"]:
            invited = await self.invite_user_to_room(
                room_id, user_ids[admin], admin_token
            )
            self.assertTrue(invited)

        # "anotheradmin" accept invite
        for invited_admin in ["anotheradmin"]:
            accepted = await self.accept_room_invitation(room_id, tokens[invited_admin])
            self.assertTrue(accepted)

        # Update anotheradmin to be room admin
        for updated_admin in ["anotheradmin"]:
            await self.set_member_power_level(
                room_id, user_ids[updated_admin], 100, admin_token
            )

        # Verify all users are in the room and is admin
        for admin_member in ["roomadmin", "anotheradmin"]:
            member_url = f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{user_ids[admin_member]}"
            resp = requests.get(
                member_url,
                headers={"Authorization": f"Bearer {tokens[admin_member]}"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json().get("membership"), "join")

            power_level = await self.get_member_power_level(
                room_id, user_ids[admin_member], admin_token
            )
            self.assertEqual(power_level, 100)

        # roomadmin deletes (should succeed)
        response = requests.post(
            delete_url,
            json={"room_id": room_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Deleted")
        # Assert other users are no longer in the room
        for ex_admin in ["anotheradmin"]:
            resp = requests.get(
                f"{self.server_url}/_matrix/client/v3/joined_rooms",
                headers={"Authorization": f"Bearer {tokens[ex_admin]}"},
            )
            self.assertEqual(resp.status_code, 200)
            joined_rooms = resp.json().get("joined_rooms", [])
            self.assertEqual(len(joined_rooms), 0)

    async def test_server_admin_deletes_after_room_admin_left(self):
        await self.homeserver()

        creator_id, creator_token = await self.new_user("creator")
        bot_id, bot_token = await self.new_user("bot", admin=True)
        member_id, member_token = await self.new_user("member")
        room_id = await self.create_private_room(creator_token)
        for user_id, token in [(bot_id, bot_token), (member_id, member_token)]:
            self.assertTrue(
                await self.invite_user_to_room(room_id, user_id, creator_token)
            )
            self.assertTrue(await self.accept_room_invitation(room_id, token))
            self.assertTrue(
                await self.set_member_power_level(room_id, user_id, 50, creator_token)
            )

        # The creator's 100 stays in m.room.power_levels after they leave
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/leave",
            json={},
            headers={"Authorization": f"Bearer {creator_token}"},
        )
        self.assertEqual(resp.status_code, 200)

        delete_url = f"{self.server_url}/_synapse/client/pangea/v1/delete_room"
        # Everyone else still counts the departed creator
        response = requests.post(
            delete_url,
            json={"room_id": room_id},
            headers={"Authorization": f"Bearer {member_token}"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("highest power level", response.json().get("error", ""))

        # A server admin is compared only with the members still in the room
        response = requests.post(
            delete_url,
            json={"room_id": room_id},
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Deleted")
        resp = requests.get(
            f"{self.server_url}/_matrix/client/v3/joined_rooms",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(room_id, resp.json().get("joined_rooms", []))

    async def set_member_power_level(
        self, room_id: str, user_id: str, power_level: int, access_token: str
    ):
        base_url = f"{self.server_url}/_matrix/client/v3"
        headers = {"Authorization": f"Bearer {access_token}"}

        # 1. Fetch current power levels
        power_levels_url = f"{base_url}/rooms/{room_id}/state/m.room.power_levels"
        get_response = requests.get(power_levels_url, headers=headers)
        if get_response.status_code != 200:
            print(
                f"Error fetching power levels: {get_response.status_code} {get_response.text}"
            )
            return False

        power_levels = get_response.json()

        # 2. Update the user's power level
        if "users" not in power_levels:
            power_levels["users"] = {}
        power_levels["users"][user_id] = power_level

        # 3. Send the updated state
        put_response = requests.put(
            power_levels_url, headers=headers, json=power_levels
        )
        if put_response.status_code not in (200, 201):
            print(
                f"Error updating power levels: {put_response.status_code} {put_response.text}"
            )
            return False

        return True

    async def get_member_power_level(
        self, room_id: str, user_id: str, access_token: str
    ):
        base_url = f"{self.server_url}/_matrix/client/v3"
        headers = {"Authorization": f"Bearer {access_token}"}

        # 1. Fetch current power levels
        power_levels_url = f"{base_url}/rooms/{room_id}/state/m.room.power_levels"
        get_response = requests.get(power_levels_url, headers=headers)
        if get_response.status_code != 200:
            print(
                f"Error fetching power levels: {get_response.status_code} {get_response.text}"
            )
            return False

        power_levels = get_response.json()

        user_permissions = power_levels.get("users", {})

        users_default = power_levels.get("user_defaults", 0)

        return user_permissions.get(user_id, users_default)

    async def create_space(self, access_token: str, name: str = "Test Space") -> str:
        """Create a new space and return its room_id."""
        headers = {"Authorization": f"Bearer {access_token}"}
        create_space_url = f"{self.server_url}/_matrix/client/v3/createRoom"
        create_space_data = {
            "visibility": "private",
            "preset": "private_chat",
            "creation_content": {"type": "m.space"},
            "name": name,
        }
        response = requests.post(
            create_space_url,
            json=cast(Any, create_space_data),
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["room_id"]

    async def add_child_to_space(
        self, space_id: str, child_room_id: str, access_token: str
    ) -> bool:
        """Add a child room to a space."""
        headers = {"Authorization": f"Bearer {access_token}"}
        space_child_url = f"{self.server_url}/_matrix/client/v3/rooms/{space_id}/state/m.space.child/{child_room_id}"
        space_child_data = {
            "via": ["my.domain.name"],
            "order": "01",
        }
        response = requests.put(
            space_child_url,
            json=space_child_data,
            headers=headers,
        )
        return response.status_code in (200, 201)

    async def add_parent_to_room(
        self, room_id: str, parent_space_id: str, access_token: str
    ) -> bool:
        """Add a parent space to a room."""
        headers = {"Authorization": f"Bearer {access_token}"}
        space_parent_url = f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/state/m.space.parent/{parent_space_id}"
        space_parent_data = {
            "via": ["my.domain.name"],
            "canonical": True,
        }
        response = requests.put(
            space_parent_url,
            json=space_parent_data,
            headers=headers,
        )
        return response.status_code in (200, 201)

    async def get_space_children(self, space_id: str, access_token: str) -> list:
        """Get the children of a space by looking at m.space.child state events with non-empty content."""
        headers = {"Authorization": f"Bearer {access_token}"}
        # Get all state events from the space
        state_url = f"{self.server_url}/_matrix/client/v3/rooms/{space_id}/state"
        response = requests.get(state_url, headers=headers)
        if response.status_code != 200:
            return []

        state_events = response.json()
        space_child_events = []
        for event in state_events:
            if event.get("type") == "m.space.child" and event.get("content"):
                # Only include events with non-empty content (active relationships)
                space_child_events.append(event)
        return space_child_events

    async def get_room_space_parents(self, room_id: str, access_token: str) -> dict:
        """Get the space parent events for a room."""
        headers = {"Authorization": f"Bearer {access_token}"}
        # Get all m.space.parent events from the room state
        state_url = f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/state"
        response = requests.get(state_url, headers=headers)
        if response.status_code != 200:
            return {}

        state_events = response.json()
        space_parent_events = {}
        for event in state_events:
            if event.get("type") == "m.space.parent":
                space_parent_events[event["state_key"]] = event
        return space_parent_events

    async def test_delete_room_with_space_relationships(self):
        """Test that space relationships are properly cleaned up when a room is deleted."""
        await self.homeserver()

        # Register users
        space_admin_user_id, space_admin_token = await self.new_user("spaceadmin")
        room_creator_user_id, room_creator_token = await self.new_user("roomcreator")

        # Create a space
        space_id = await self.create_space(space_admin_token, "Test Space")

        # Create a regular room
        room_id = await self.create_private_room(room_creator_token)

        # Invite room creator to space and vice versa to allow space relationship setup
        await self.invite_user_to_room(
            space_id, room_creator_user_id, space_admin_token
        )
        await self.accept_room_invitation(space_id, room_creator_token)

        # Give room creator admin powers in the space so they can modify space relationships
        await self.set_member_power_level(
            space_id, room_creator_user_id, 100, space_admin_token
        )

        await self.invite_user_to_room(room_id, space_admin_user_id, room_creator_token)
        await self.accept_room_invitation(room_id, space_admin_token)

        # Set up space relationships
        # Add the room as a child of the space
        child_added = await self.add_child_to_space(
            space_id, room_id, space_admin_token
        )
        self.assertTrue(child_added, "Failed to add child to space")

        # Add the space as a parent of the room
        parent_added = await self.add_parent_to_room(
            room_id, space_id, room_creator_token
        )
        self.assertTrue(parent_added, "Failed to add parent to room")

        # Verify relationships exist before deletion
        space_children = await self.get_space_children(space_id, space_admin_token)
        child_room_ids = [child["state_key"] for child in space_children]
        self.assertIn(room_id, child_room_ids, "Room should be a child of the space")

        room_parents = await self.get_room_space_parents(room_id, room_creator_token)
        self.assertIn(space_id, room_parents, "Space should be a parent of the room")

        # Delete the room using our API
        delete_url = f"{self.server_url}/_synapse/client/pangea/v1/delete_room"
        response = requests.post(
            delete_url,
            json={"room_id": room_id},
            headers={"Authorization": f"Bearer {room_creator_token}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Deleted")

        # Wait a moment for the cleanup to complete
        await asyncio.sleep(1)

        # Verify space relationships are cleaned up
        space_children_after = await self.get_space_children(
            space_id, space_admin_token
        )
        child_room_ids_after = [child["state_key"] for child in space_children_after]

        self.assertNotIn(
            room_id,
            child_room_ids_after,
            "Room should no longer be a child of the space after deletion",
        )
