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


class TestUserActivityE2E(BaseSynapseE2ETest):
    async def test_user_activity_requires_admin(self):
        """Non-admin users should get 403 from the user_activity endpoint."""
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

            # Register a non-admin user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="regular_user",
                password="pw1",
                admin=False,
            )
            _, token = await self.login_user("regular_user", "pw1")

            # Try to access user_activity endpoint
            url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            self.assertEqual(response.status_code, 403)
            self.assertIn("admin", response.json().get("error", "").lower())

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_user_activity_admin_access(self):
        """Admin users should get user activity data."""
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

            # Register an admin user and a regular user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin",
                password="adminpw",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="user1",
                password="pw1",
                admin=False,
            )

            _, admin_token = await self.login_user("admin", "adminpw")
            await self.login_user("user1", "pw1")

            # Access user_activity endpoint as admin
            url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("users", data)
            self.assertIsInstance(data["users"], list)

            # Should have at least 2 users (admin + user1)
            user_ids = [u["user_id"] for u in data["users"]]
            self.assertIn("@admin:my.domain.name", user_ids)
            self.assertIn("@user1:my.domain.name", user_ids)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_user_activity_with_rooms_and_messages(self):
        """Verify the endpoint returns correct activity data including
        room memberships, last message timestamps, and course/activity info."""
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

            # Register admin + regular user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin",
                password="adminpw",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="learner",
                password="pw1",
                admin=False,
            )

            _, admin_token = await self.login_user("admin", "adminpw")
            _, learner_token = await self.login_user("learner", "pw1")

            # Create a "course" room (space with pangea.course_plan state event)
            headers_admin = {"Authorization": f"Bearer {admin_token}"}
            create_room_url = f"{self.server_url}/_matrix/client/v3/createRoom"

            # Create course space
            course_resp = requests.post(
                create_room_url,
                json={
                    "visibility": "public",
                    "preset": "public_chat",
                    "creation_content": {"type": "m.space"},
                    "initial_state": [
                        {
                            "type": "pangea.course_plan",
                            "state_key": "",
                            "content": {"uuid": "course-123"},
                        }
                    ],
                    "name": "Test Course",
                },
                headers=headers_admin,
                timeout=30,
            )
            self.assertEqual(course_resp.status_code, 200)
            course_room_id = course_resp.json()["room_id"]

            # Create an activity room with pangea.activity_plan state event
            activity_resp = requests.post(
                create_room_url,
                json={
                    "visibility": "private",
                    "preset": "private_chat",
                    "initial_state": [
                        {
                            "type": "pangea.activity_plan",
                            "state_key": "",
                            "content": {"activity_id": "activity-456"},
                        },
                        {
                            "type": "m.space.parent",
                            "state_key": course_room_id,
                            "content": {"via": ["my.domain.name"]},
                        },
                    ],
                    "name": "Activity Room 1",
                },
                headers=headers_admin,
                timeout=30,
            )
            self.assertEqual(activity_resp.status_code, 200)
            activity_room_id = activity_resp.json()["room_id"]

            # Learner joins the course
            join_url = f"{self.server_url}/_matrix/client/v3/join/{course_room_id}"
            join_resp = requests.post(
                join_url,
                headers={"Authorization": f"Bearer {learner_token}"},
                timeout=30,
            )
            self.assertEqual(join_resp.status_code, 200)

            # Learner sends a message in the course
            send_url = f"{self.server_url}/_matrix/client/v3/rooms/{course_room_id}/send/m.room.message/txn1"
            send_resp = requests.put(
                send_url,
                json={"msgtype": "m.text", "body": "Hello from learner!"},
                headers={"Authorization": f"Bearer {learner_token}"},
                timeout=30,
            )
            self.assertEqual(send_resp.status_code, 200)

            # Wait a moment for events to be processed
            await asyncio.sleep(1)

            # Query user activity
            url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
            response = requests.get(url, headers=headers_admin, timeout=30)
            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Find the learner in the results
            learner_data = None
            for user in data["users"]:
                if user["user_id"] == "@learner:my.domain.name":
                    learner_data = user
                    break

            self.assertIsNotNone(learner_data)
            self.assertGreater(learner_data["last_message_ts"], 0)
            self.assertGreater(learner_data["last_login_ts"], 0)

            # Learner should have the course in their courses list
            course_rooms = [
                c
                for c in learner_data["courses"]
                if c["room_id"] == course_room_id and c["is_course"]
            ]
            self.assertEqual(len(course_rooms), 1)

            # Most recent course should be the one they sent a message in
            self.assertEqual(learner_data["most_recent_course_room_id"], course_room_id)

            # Activity rooms by course should include our activity room
            activity_rooms = learner_data.get("activity_rooms_by_course", {})
            course_activities = activity_rooms.get(course_room_id, [])
            self.assertGreater(len(course_activities), 0)

            activity_room_entry = [
                a for a in course_activities if a["room_id"] == activity_room_id
            ]
            self.assertEqual(len(activity_room_entry), 1)
            self.assertEqual(activity_room_entry[0]["activity_id"], "activity-456")

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_user_activity_unauthorized(self):
        """Requests without auth should get 401."""
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                _config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse()

            url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
            response = requests.get(url, timeout=30)
            self.assertEqual(response.status_code, 401)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
