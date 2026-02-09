import asyncio
import logging
from time import perf_counter
from typing import Union

import requests

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.room_code.constants import (
    ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    JOIN_RULE_CONTENT_KEY,
    KNOCK_JOIN_RULE_VALUE,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_INVITE,
)
from synapse_pangea_chat.room_code.is_rate_limited import is_rate_limited

from .base_e2e import BaseSynapseE2ETest

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,  # Set the logging level
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",  # Log format
    filename="synapse.log",  # File to log to
    filemode="w",  # Append mode (use 'w' to overwrite each time)
)


class TestE2E(BaseSynapseE2ETest):
    async def set_room_knockable_with_code(
        self,
        room_id: str,
        access_token: str,
        access_code: Union[str, None] = None,
    ):
        headers = {"Authorization": f"Bearer {access_token}"}
        set_join_rules_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.join_rules"
        state_event_content = {
            JOIN_RULE_CONTENT_KEY: KNOCK_JOIN_RULE_VALUE,
            ACCESS_CODE_JOIN_RULE_CONTENT_KEY: access_code,
        }
        response = requests.put(
            set_join_rules_url,
            json=state_event_content,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        event_id = response.json()["event_id"]
        self.assertIsInstance(event_id, str)
        return event_id

    async def knock_with_code(self, access_code: str, access_token: str):
        knock_with_code_url = (
            "http://localhost:8008/_synapse/client/pangea/v1/knock_with_code"
        )
        response = requests.post(
            knock_with_code_url,
            json={"access_code": access_code},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200)

    async def knock_without_access_token(self):
        knock_with_code_url = (
            "http://localhost:8008/_synapse/client/pangea/v1/knock_with_code"
        )
        response = requests.post(
            knock_with_code_url,
            json={"access_code": "invalid"},
        )
        self.assertEqual(response.status_code, 403)

    async def knock_with_invalid_code(self, access_token: str):
        knock_with_code_url = (
            "http://localhost:8008/_synapse/client/pangea/v1/knock_with_code"
        )
        response = requests.post(
            knock_with_code_url,
            json={"access_code": "invalid"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 400)

    async def wait_for_room_invitation(
        self, room_id: str, user_id: str, access_token: str
    ) -> bool:
        room_state_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{user_id}"
        total_wait_time = 0
        max_wait_time = 3
        wait_interval = 1
        received_invitation = False
        while total_wait_time < max_wait_time and not received_invitation:
            response = requests.get(
                room_state_url, headers={"Authorization": f"Bearer {access_token}"}
            )
            if (
                response.status_code == 200
                and response.json().get(MEMBERSHIP_CONTENT_KEY) == MEMBERSHIP_INVITE
            ):
                received_invitation = True
                break

            print(
                f"User 2 has not been invited to the room yet, retrying {total_wait_time}/{max_wait_time}..."
            )
            await asyncio.sleep(wait_interval)
            total_wait_time += wait_interval
        return received_invitation

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
        event_id = response.json()["event_id"]
        self.assertIsInstance(event_id, str)
        return event_id

    async def join_room(self, room_id: str, access_token: str):
        headers = {"Authorization": f"Bearer {access_token}"}
        join_room_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/join"
        response = requests.post(join_room_url, json={}, headers=headers)
        self.assertEqual(response.status_code, 200)
        room_id_response = response.json()["room_id"]
        self.assertIsInstance(room_id_response, str)
        return room_id_response

    async def leave_room(self, room_id: str, access_token: str):
        headers = {"Authorization": f"Bearer {access_token}"}
        leave_room_url = (
            f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/leave"
        )
        response = requests.post(leave_room_url, json={}, headers=headers)
        self.assertEqual(response.status_code, 200)

    async def test_e2e_knock_with_code_admin_left(self) -> None:
        """
        Test knock with code when ALL admins (users with power level >= invite power)
        have left the room.

        Scenario:
        1. User1 (admin, power level 100) creates the room
        2. User2 (non-admin, power level 0) is invited and joins the room
        3. User1 (the only admin) leaves the room
        4. User3 knocks with the correct access code
        5. Expected: User2 should be promoted to have invite power, then invite User3
        """
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            access_code = "vldcde1"

            # Start Synapse server
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse()

            # Register test users
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test1",
                password="123123123",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test2",
                password="123123123",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test3",
                password="123123123",
                admin=True,
            )

            # Login to obtain access tokens
            user_1_id, user_1_access_token = await self.login_user(
                user="test1", password="123123123"
            )
            user_2_id, user_2_access_token = await self.login_user(
                user="test2", password="123123123"
            )
            user_3_id, user_3_access_token = await self.login_user(
                user="test3", password="123123123"
            )

            # Create room - User1 is the creator with power level 100
            room_id = await self.create_private_room(user_1_access_token)

            # Invite User2 to the room (they will have default power level 0)
            await self.invite_user_to_room(
                room_id=room_id, user_id=user_2_id, access_token=user_1_access_token
            )
            await self.join_room(room_id=room_id, access_token=user_2_access_token)

            # Set power levels explicitly:
            # - user1 = 100 (admin, can invite since invite power is 50)
            # - user2 = 0 (non-admin, cannot invite)
            # This ensures user2 has power level 0 which is below invite power (50)
            await self.set_room_power_levels(
                room_id=room_id,
                access_token=user_1_access_token,
                user_power_levels={
                    user_1_id: 100,
                    user_2_id: 0,
                },
            )

            # Set room to be knockable with access code BEFORE user1 leaves
            # (only user1 has power to change room state)
            await self.set_room_knockable_with_code(
                room_id=room_id,
                access_token=user_1_access_token,
                access_code=access_code,
            )

            # User1 (the only admin with invite power) leaves the room
            # Now only User2 remains, with power level 0 (below invite power of 50)
            await self.leave_room(room_id=room_id, access_token=user_1_access_token)

            # User3 knocks with the correct access code
            # Expected behavior: User2 (power level 0) should be promoted to power level 50
            # to be able to invite User3
            await self.knock_with_code(access_code, user_3_access_token)

            # Wait for the invite - should work because User2 gets promoted to invite User3
            received_invitation = await self.wait_for_room_invitation(
                room_id=room_id,
                user_id=user_3_id,
                access_token=user_2_access_token,
            )
            if not received_invitation:
                self.fail(
                    "User 3 was not invited to the room. "
                    "Expected: User2 should be promoted and invite User3 after all admins left."
                )
            else:
                logger.info(
                    "User 3 was invited to the room successfully after all admins left - "
                    "User2 was promoted to invite power level"
                )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_e2e_knock_with_code_admin_left_default_power(self) -> None:
        """
        Test knock with code when admin leaves and remaining user has DEFAULT power level
        (not explicitly set in the users dict).

        Scenario:
        1. User1 (admin, power level 100) creates the room
        2. User2 is invited and joins (has default power level, NOT explicitly set)
        3. Power levels only set user1=100, user2 is NOT in the users dict
        4. User1 leaves the room
        5. User3 knocks with the correct access code
        6. Expected: User2 (with default power level) should be found, promoted, and invite User3
        """
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            access_code = "vldcde1"

            # Start Synapse server
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse()

            # Register test users
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test1",
                password="123123123",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test2",
                password="123123123",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test3",
                password="123123123",
                admin=True,
            )

            # Login to obtain access tokens
            user_1_id, user_1_access_token = await self.login_user(
                user="test1", password="123123123"
            )
            user_2_id, user_2_access_token = await self.login_user(
                user="test2", password="123123123"
            )
            user_3_id, user_3_access_token = await self.login_user(
                user="test3", password="123123123"
            )

            # Create room - User1 is the creator with power level 100
            room_id = await self.create_private_room(user_1_access_token)

            # Invite User2 to the room (they will have default power level)
            await self.invite_user_to_room(
                room_id=room_id, user_id=user_2_id, access_token=user_1_access_token
            )
            await self.join_room(room_id=room_id, access_token=user_2_access_token)

            # Set power levels with ONLY user1 explicitly set
            # User2 is NOT in the users dict, so they have default power level (0)
            await self.set_room_power_levels(
                room_id=room_id,
                access_token=user_1_access_token,
                user_power_levels={
                    user_1_id: 100,
                    # user_2_id is NOT set - they have default power level
                },
            )

            # Set room to be knockable with access code BEFORE user1 leaves
            await self.set_room_knockable_with_code(
                room_id=room_id,
                access_token=user_1_access_token,
                access_code=access_code,
            )

            # User1 (the only admin) leaves the room
            # Now only User2 remains, with DEFAULT power level (not in users dict)
            await self.leave_room(room_id=room_id, access_token=user_1_access_token)

            # User3 knocks with the correct access code
            # Expected: User2 (default power level) should be found, promoted, and invite User3
            await self.knock_with_code(access_code, user_3_access_token)

            # Wait for the invite
            received_invitation = await self.wait_for_room_invitation(
                room_id=room_id,
                user_id=user_3_id,
                access_token=user_2_access_token,
            )
            if not received_invitation:
                self.fail(
                    "User 3 was not invited to the room. "
                    "Expected: User2 (with default power level) should be found, promoted, and invite User3."
                )
            else:
                logger.info(
                    "User 3 was invited successfully - User2 with default power level was found and promoted"
                )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_e2e_knock_with_code(self) -> None:
        postgres = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        synapse_dir = None
        try:
            # Create a temporary directory for the Synapse server
            access_code = "vldcde1"
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse()
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test1",
                password="123123123",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test2",
                password="123123123",
                admin=True,
            )

            # Login to obtain access token of both users
            user_1_id, user_1_access_token = await self.login_user(
                user="test1", password="123123123"
            )
            user_2_id, user_2_access_token = await self.login_user(
                user="test2", password="123123123"
            )

            room_id = await self.create_private_room(user_1_access_token)

            await self.set_room_knockable_with_code(
                room_id=room_id,
                access_token=user_1_access_token,
                access_code=access_code,
            )

            # Invoke knock with code endpoint
            await self.knock_with_invalid_code(user_2_access_token)
            await self.knock_with_code(access_code, user_2_access_token)

            # Wait for the invite
            received_invitation = await self.wait_for_room_invitation(
                room_id=room_id,
                user_id=user_2_id,
                access_token=user_1_access_token,
            )
            if not received_invitation:
                self.fail("User 2 was not invited to the room")
            else:
                print("User 2 was invited to the room")

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def get_access_token_without_access_code(self):
        get_access_token_url = (
            "http://localhost:8008/_synapse/client/pangea/v1/request_room_code"
        )
        response = requests.get(url=get_access_token_url)
        self.assertEqual(response.status_code, 403)

    async def get_access_token(self, access_token: str):
        t0 = perf_counter()
        get_access_token_url = (
            "http://localhost:8008/_synapse/client/pangea/v1/request_room_code"
        )
        response = requests.get(
            url=get_access_token_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200)
        t1 = perf_counter()
        print(f"Time taken to get access code: {t1 - t0} seconds")
        access_code = response.json()["access_code"]
        self.assertIsInstance(access_code, str)

    async def test_e2e_get_access_code(self) -> None:
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

            # Register and login
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test1",
                password="123123123",
                admin=True,
            )
            user_1_id, user_access_token = await self.login_user(
                user="test1", password="123123123"
            )

            # Get access code
            await self.get_access_token_without_access_code()
            await self.get_access_token(user_access_token)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_rate_limit(self) -> None:
        user_id = "foobar"
        config = PangeaChatConfig(
            knock_with_code_requests_per_burst=3,
            knock_with_code_burst_duration_seconds=5,
        )
        for _ in range(config.knock_with_code_requests_per_burst):
            self.assertFalse(is_rate_limited(user_id, config))
            await asyncio.sleep(1)
        self.assertTrue(is_rate_limited(user_id, config))
        await asyncio.sleep(config.knock_with_code_burst_duration_seconds + 1)
        self.assertFalse(is_rate_limited(user_id, config))

    async def get_room_power_levels(self, room_id: str, access_token: str) -> dict:
        """Get the current power levels state for a room."""
        headers = {"Authorization": f"Bearer {access_token}"}
        power_levels_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels"
        response = requests.get(power_levels_url, headers=headers)
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_e2e_knock_with_code_promotes_user_to_admin(self) -> None:
        """
        Test that when the admin (user A) leaves a room and rejoins with a code,
        the remaining member (user B) who doesn't have sufficient power to invite
        is promoted to admin so they can send the invite.

        Scenario:
        1. User A creates a room and invites User B
        2. User A has admin power (100), User B has default power (0)
        3. User A leaves the room
        4. User A rejoins with access code
        5. Expected: get_inviter_user should promote User B to have invite power,
           then return User B as the inviter
        """
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            access_code = "promo1t"  # 7 chars, alphanumeric with at least 1 digit

            # Start Synapse server
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse()

            # Register test users
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="userA",
                password="123123123",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="userB",
                password="123123123",
                admin=True,
            )

            # Login to obtain access tokens
            user_a_id, user_a_access_token = await self.login_user(
                user="userA", password="123123123"
            )
            user_b_id, user_b_access_token = await self.login_user(
                user="userB", password="123123123"
            )

            # Step 1: User A creates a room
            room_id = await self.create_private_room(user_a_access_token)

            # Step 2: User A invites User B and User B joins
            await self.invite_user_to_room(
                room_id=room_id, user_id=user_b_id, access_token=user_a_access_token
            )
            await self.join_room(room_id=room_id, access_token=user_b_access_token)

            # Set power levels explicitly: User A = 100 (admin), User B = 0 (no invite power)
            # invite power required = 50 (default for private rooms)
            await self.set_room_power_levels(
                room_id=room_id,
                access_token=user_a_access_token,
                user_power_levels={
                    user_a_id: 100,
                    user_b_id: 0,
                },
            )

            # Verify initial power levels: User A should be admin, User B should have low power
            power_levels = await self.get_room_power_levels(
                room_id=room_id, access_token=user_a_access_token
            )
            user_a_power = power_levels.get("users", {}).get(user_a_id, 0)
            user_b_power = power_levels.get("users", {}).get(user_b_id, 0)
            invite_power_required = power_levels.get("invite", 0)

            # User A should have admin power (100)
            self.assertGreaterEqual(user_a_power, invite_power_required)
            # User B should NOT have invite power initially (0 < 50)
            self.assertLess(user_b_power, invite_power_required)
            logger.info(
                f"Initial power levels - User A: {user_a_power}, User B: {user_b_power}, "
                f"Invite required: {invite_power_required}"
            )

            # Set room to be knockable with access code (before User A leaves)
            await self.set_room_knockable_with_code(
                room_id=room_id,
                access_token=user_a_access_token,
                access_code=access_code,
            )

            # Step 3: User A leaves the room
            await self.leave_room(room_id=room_id, access_token=user_a_access_token)

            # Step 4: User A rejoins using the access code
            # This should trigger the new logic where User B is promoted to admin
            await self.knock_with_code(access_code, user_a_access_token)

            # Step 5: Wait for User A to receive an invitation
            received_invitation = await self.wait_for_room_invitation(
                room_id=room_id,
                user_id=user_a_id,
                access_token=user_b_access_token,
            )

            if not received_invitation:
                self.fail(
                    "User A was not invited to the room. "
                    "Expected User B to be promoted to admin and send the invite."
                )
            else:
                logger.info("User A was successfully invited back to the room!")

            # Verify that User B now has sufficient power to invite (was promoted)
            power_levels_after = await self.get_room_power_levels(
                room_id=room_id, access_token=user_b_access_token
            )
            user_b_power_after = power_levels_after.get("users", {}).get(user_b_id, 0)
            invite_power_required_after = power_levels_after.get("invite", 0)

            self.assertGreaterEqual(
                user_b_power_after,
                invite_power_required_after,
                f"User B should have been promoted to have invite power. "
                f"User B power: {user_b_power_after}, Invite required: {invite_power_required_after}",
            )
            logger.info(
                f"Final power levels - User B: {user_b_power_after}, "
                f"Invite required: {invite_power_required_after}"
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
