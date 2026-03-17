"""E2E tests for admin access code knock flow.

Tests the admin_access_code feature:
- Knock with admin code → invite + promote to admin + burn code
- Subsequent knock with same admin code → fails (code burned)
- Regular access_code still works after admin code is burned
"""

import asyncio
import logging
from typing import Any, Dict

import requests

from synapse_pangea_chat.room_code.constants import (
    ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    JOIN_RULE_CONTENT_KEY,
    KNOCK_JOIN_RULE_VALUE,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_INVITE,
)

from .base_e2e import BaseSynapseE2ETest

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    filename="synapse.log",
    filemode="a",
)


class TestAdminCodeE2E(BaseSynapseE2ETest):
    """E2E tests for admin access code knock-with-code flow."""

    async def set_room_knockable_with_both_codes(
        self,
        room_id: str,
        access_token: str,
        access_code: str,
        admin_access_code: str,
    ):
        """Set room join rules with both student and admin access codes."""
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.join_rules"
        content = {
            JOIN_RULE_CONTENT_KEY: KNOCK_JOIN_RULE_VALUE,
            ACCESS_CODE_JOIN_RULE_CONTENT_KEY: access_code,
            ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY: admin_access_code,
        }
        response = requests.put(url, json=content, headers=headers)
        self.assertEqual(response.status_code, 200)

    async def knock_with_code(
        self, access_code: str, access_token: str
    ) -> requests.Response:
        """Send a knock-with-code request. Returns the response."""
        url = "http://localhost:8008/_synapse/client/pangea/v1/knock_with_code"
        response = requests.post(
            url,
            json={"access_code": access_code},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return response

    async def get_join_rules(self, room_id: str, access_token: str) -> Dict[str, Any]:
        """Get the current join rules for a room."""
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.join_rules"
        response = requests.get(url, headers=headers)
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def get_user_power_level(
        self, room_id: str, user_id: str, access_token: str
    ) -> int:
        """Get a user's power level in a room."""
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.power_levels"
        response = requests.get(url, headers=headers)
        self.assertEqual(response.status_code, 200)
        power_levels = response.json()
        users = power_levels.get("users", {})
        users_default = power_levels.get("users_default", 0)
        return users.get(user_id, users_default)

    async def wait_for_room_invitation(
        self, room_id: str, user_id: str, access_token: str
    ) -> bool:
        """Wait for a user to be invited to a room."""
        url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/m.room.member/{user_id}"
        total_wait_time = 0
        max_wait_time = 5
        wait_interval = 1
        while total_wait_time < max_wait_time:
            response = requests.get(
                url, headers={"Authorization": f"Bearer {access_token}"}
            )
            if (
                response.status_code == 200
                and response.json().get(MEMBERSHIP_CONTENT_KEY) == MEMBERSHIP_INVITE
            ):
                return True
            await asyncio.sleep(wait_interval)
            total_wait_time += wait_interval
        return False

    async def wait_until_admin_code_burned(
        self,
        room_id: str,
        access_token: str,
        max_wait_time: int = 5,
    ) -> bool:
        """Poll join rules until admin_access_code is removed."""
        total_wait_time = 0
        wait_interval = 1
        while total_wait_time < max_wait_time:
            join_rules = await self.get_join_rules(
                room_id=room_id, access_token=access_token
            )
            if ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY not in join_rules:
                return True
            await asyncio.sleep(wait_interval)
            total_wait_time += wait_interval
        return False

    async def join_room(self, room_id: str, access_token: str):
        """Join a room."""
        headers = {"Authorization": f"Bearer {access_token}"}
        url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/join"
        response = requests.post(url, json={}, headers=headers)
        self.assertEqual(response.status_code, 200)

    async def test_admin_code_promotes_and_burns(self) -> None:
        """Knock with admin code: user is invited, promoted to power 100,
        and admin_access_code is removed from join rules."""
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            student_code = "stud3n1"
            admin_code = "adm1nc1"

            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse()

            # Register users
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="creator",
                password="123123123",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="teacher",
                password="123123123",
                admin=True,
            )

            creator_id, creator_token = await self.login_user(
                user="creator", password="123123123"
            )
            teacher_id, teacher_token = await self.login_user(
                user="teacher", password="123123123"
            )

            # Create room and set knockable with both codes
            room_id = await self.create_private_room(creator_token)
            await self.set_room_knockable_with_both_codes(
                room_id=room_id,
                access_token=creator_token,
                access_code=student_code,
                admin_access_code=admin_code,
            )

            # Teacher knocks with admin code
            response = await self.knock_with_code(admin_code, teacher_token)
            self.assertEqual(response.status_code, 200)

            # Wait for invitation
            invited = await self.wait_for_room_invitation(
                room_id=room_id,
                user_id=teacher_id,
                access_token=creator_token,
            )
            self.assertTrue(invited, "Teacher should be invited via admin code")

            # Teacher joins
            await self.join_room(room_id=room_id, access_token=teacher_token)

            # Verify teacher was promoted to power level 100
            # Allow a small delay for state to propagate
            await asyncio.sleep(1)
            power = await self.get_user_power_level(
                room_id=room_id,
                user_id=teacher_id,
                access_token=creator_token,
            )
            self.assertEqual(
                power, 100, "Teacher should be promoted to power level 100"
            )

            # Verify admin code was burned from join rules
            join_rules = await self.get_join_rules(
                room_id=room_id, access_token=creator_token
            )
            self.assertNotIn(
                ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
                join_rules,
                "admin_access_code should be removed after use",
            )
            # Student code should still be present
            self.assertEqual(
                join_rules.get(ACCESS_CODE_JOIN_RULE_CONTENT_KEY),
                student_code,
                "Student access_code should remain after admin code is burned",
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_burned_admin_code_rejected(self) -> None:
        """After admin code is burned, a second user trying it gets no match."""
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            student_code = "stud3n2"
            admin_code = "adm1nc2"

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
                user="creator",
                password="123123123",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="teacher1",
                password="123123123",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="teacher2",
                password="123123123",
                admin=True,
            )

            creator_id, creator_token = await self.login_user(
                user="creator", password="123123123"
            )
            teacher1_id, teacher1_token = await self.login_user(
                user="teacher1", password="123123123"
            )
            teacher2_id, teacher2_token = await self.login_user(
                user="teacher2", password="123123123"
            )

            room_id = await self.create_private_room(creator_token)
            await self.set_room_knockable_with_both_codes(
                room_id=room_id,
                access_token=creator_token,
                access_code=student_code,
                admin_access_code=admin_code,
            )

            # First teacher uses admin code — should succeed
            response = await self.knock_with_code(admin_code, teacher1_token)
            self.assertEqual(response.status_code, 200)

            invited = await self.wait_for_room_invitation(
                room_id=room_id,
                user_id=teacher1_id,
                access_token=creator_token,
            )
            self.assertTrue(invited, "First teacher should be invited")

            burned = await self.wait_until_admin_code_burned(
                room_id=room_id,
                access_token=creator_token,
                max_wait_time=10,
            )
            self.assertTrue(burned, "Admin code should be burned after first use")

            # Second teacher tries same admin code — should fail (code burned)
            response2 = await self.knock_with_code(admin_code, teacher2_token)
            self.assertEqual(response2.status_code, 400, response2.text)
            self.assertIn("No rooms found", response2.json().get("error", ""))

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_student_code_still_works_after_admin_burn(self) -> None:
        """Student access_code continues to work after admin code is burned."""
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            student_code = "stud3n3"
            admin_code = "adm1nc3"

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
                user="creator",
                password="123123123",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="teacher",
                password="123123123",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="student",
                password="123123123",
                admin=True,
            )

            creator_id, creator_token = await self.login_user(
                user="creator", password="123123123"
            )
            teacher_id, teacher_token = await self.login_user(
                user="teacher", password="123123123"
            )
            student_id, student_token = await self.login_user(
                user="student", password="123123123"
            )

            room_id = await self.create_private_room(creator_token)
            await self.set_room_knockable_with_both_codes(
                room_id=room_id,
                access_token=creator_token,
                access_code=student_code,
                admin_access_code=admin_code,
            )

            # Teacher uses admin code (burns it)
            response = await self.knock_with_code(admin_code, teacher_token)
            self.assertEqual(response.status_code, 200)

            await self.wait_for_room_invitation(
                room_id=room_id,
                user_id=teacher_id,
                access_token=creator_token,
            )
            await asyncio.sleep(2)

            # Student uses regular access code — should still work
            response2 = await self.knock_with_code(student_code, student_token)
            self.assertEqual(
                response2.status_code,
                200,
                "Student code should still work after admin code is burned",
            )

            invited = await self.wait_for_room_invitation(
                room_id=room_id,
                user_id=student_id,
                access_token=creator_token,
            )
            self.assertTrue(
                invited,
                "Student should be invited via student code after admin code burn",
            )

            # Verify student is NOT promoted (regular code, not admin)
            await self.join_room(room_id=room_id, access_token=student_token)
            await asyncio.sleep(1)
            power = await self.get_user_power_level(
                room_id=room_id,
                user_id=student_id,
                access_token=creator_token,
            )
            self.assertLess(
                power, 100, "Student should NOT be promoted to admin via regular code"
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
