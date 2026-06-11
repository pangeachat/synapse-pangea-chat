import asyncio
import logging

import psycopg2
import requests
import yaml

from .base_e2e import BaseSynapseE2ETest

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    filename="synapse.log",
    filemode="w",
)


class TestUserActivityE2E(BaseSynapseE2ETest):
    def _db_args_from_config(self, config_path: str) -> dict:
        with open(config_path, "r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)
        return config["database"]["args"]

    def _replace_last_login_ts(
        self,
        config_path: str,
        user_id: str,
        timestamp_ms: int | None,
    ) -> None:
        db_args = self._db_args_from_config(config_path)
        with psycopg2.connect(**db_args) as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM user_ips WHERE user_id = %s", (user_id,))
                if timestamp_ms is not None:
                    cursor.execute(
                        """
                        INSERT INTO user_ips (
                            user_id, access_token, device_id, ip, user_agent, last_seen
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            user_id,
                            f"test-access-token:{user_id}",
                            None,
                            "127.0.0.1",
                            "test-user-activity-sort",
                            timestamp_ms,
                        ),
                    )

    def _set_event_origin_server_ts(
        self,
        config_path: str,
        event_id: str,
        timestamp_ms: int,
    ) -> None:
        db_args = self._db_args_from_config(config_path)
        with psycopg2.connect(**db_args) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE events SET origin_server_ts = %s WHERE event_id = %s",
                    (timestamp_ms, event_id),
                )
                self.assertEqual(cursor.rowcount, 1)

    def _user_activity_docs(
        self,
        admin_token: str,
        params: dict[str, str],
    ) -> list[dict]:
        response = requests.get(
            f"{self.server_url}/_synapse/client/pangea/v1/user_activity",
            params=params,
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=30,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["docs"]

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
        """Admin users should get paginated user activity data."""
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

            # Paginated response shape
            self.assertIn("docs", data)
            self.assertIsInstance(data["docs"], list)
            self.assertIn("page", data)
            self.assertIn("limit", data)
            self.assertIn("totalDocs", data)
            self.assertIn("maxPage", data)

            # Should have at least 2 users (admin + user1)
            user_ids = [u["user_id"] for u in data["docs"]]
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

    async def test_user_activity_sorting_and_latest_activity_pagination(self):
        """Sort params order users globally before pagination and expose the sort key."""
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

            for username, password, admin in [
                ("admin", "adminpw", True),
                ("loginonly", "pw1", False),
                ("messageonly", "pw2", False),
                ("both", "pw3", False),
                ("never", "pw4", False),
                ("tie", "pw5", False),
            ]:
                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user=username,
                    password=password,
                    admin=admin,
                )

            admin_id, admin_token = await self.login_user("admin", "adminpw")
            loginonly_id, _ = await self.login_user("loginonly", "pw1")
            messageonly_id, messageonly_token = await self.login_user(
                "messageonly", "pw2"
            )
            both_id, both_token = await self.login_user("both", "pw3")
            never_id = "@never:my.domain.name"
            tie_id, _ = await self.login_user("tie", "pw5")

            messageonly_room_id = await self.create_private_room(messageonly_token)
            messageonly_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{messageonly_room_id}"
                "/send/m.room.message/txn-messageonly-sort",
                json={"msgtype": "m.text", "body": "message-only activity"},
                headers={"Authorization": f"Bearer {messageonly_token}"},
                timeout=30,
            )
            self.assertEqual(messageonly_response.status_code, 200)
            messageonly_event_id = messageonly_response.json()["event_id"]

            both_room_id = await self.create_private_room(both_token)
            both_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{both_room_id}"
                "/send/m.room.message/txn-both-sort",
                json={"msgtype": "m.text", "body": "both login and message"},
                headers={"Authorization": f"Bearer {both_token}"},
                timeout=30,
            )
            self.assertEqual(both_response.status_code, 200)
            both_event_id = both_response.json()["event_id"]

            # Seed deterministic activity facts. This avoids relying on Synapse's
            # periodic user_ips flush timing while still exercising the real HTTP
            # endpoint and real Postgres-backed queries.
            self._replace_last_login_ts(config_path, admin_id, 1_000)
            self._replace_last_login_ts(config_path, loginonly_id, 3_000)
            self._replace_last_login_ts(config_path, messageonly_id, None)
            self._set_event_origin_server_ts(config_path, messageonly_event_id, 7_000)
            self._replace_last_login_ts(config_path, both_id, 9_000)
            self._set_event_origin_server_ts(config_path, both_event_id, 5_000)
            self._replace_last_login_ts(config_path, tie_id, 7_000)
            self._replace_last_login_ts(config_path, never_id, None)

            sorted_user_ids = ",".join(
                [loginonly_id, messageonly_id, both_id, never_id, tie_id]
            )
            expected_latest_activity = {
                both_id: 9_000,
                loginonly_id: 3_000,
                messageonly_id: 7_000,
                never_id: 0,
                tie_id: 7_000,
            }

            default_docs = self._user_activity_docs(
                admin_token,
                {"user_ids": sorted_user_ids},
            )
            self.assertEqual(
                [doc["user_id"] for doc in default_docs],
                [both_id, loginonly_id, messageonly_id, never_id, tie_id],
            )
            self.assertEqual(
                {doc["user_id"]: doc["latest_activity_ts"] for doc in default_docs},
                expected_latest_activity,
            )

            user_id_desc_docs = self._user_activity_docs(
                admin_token,
                {
                    "user_ids": sorted_user_ids,
                    "sort_by": "user_id",
                    "sort_order": "desc",
                },
            )
            self.assertEqual(
                [doc["user_id"] for doc in user_id_desc_docs],
                [tie_id, never_id, messageonly_id, loginonly_id, both_id],
            )

            last_login_desc_docs = self._user_activity_docs(
                admin_token,
                {
                    "user_ids": sorted_user_ids,
                    "sort_by": "last_login_ts",
                    "sort_order": "desc",
                },
            )
            self.assertEqual(
                [doc["user_id"] for doc in last_login_desc_docs],
                [both_id, tie_id, loginonly_id, messageonly_id, never_id],
            )

            last_message_desc_docs = self._user_activity_docs(
                admin_token,
                {
                    "user_ids": sorted_user_ids,
                    "sort_by": "last_message_ts",
                    "sort_order": "desc",
                },
            )
            self.assertEqual(
                [doc["user_id"] for doc in last_message_desc_docs],
                [messageonly_id, both_id, loginonly_id, never_id, tie_id],
            )

            latest_desc_docs = self._user_activity_docs(
                admin_token,
                {
                    "user_ids": sorted_user_ids,
                    "sort_by": "latest_activity",
                    "sort_order": "desc",
                },
            )
            self.assertEqual(
                [doc["user_id"] for doc in latest_desc_docs],
                [both_id, messageonly_id, tie_id, loginonly_id, never_id],
            )
            self.assertEqual(
                [doc["latest_activity_ts"] for doc in latest_desc_docs],
                [9_000, 7_000, 7_000, 3_000, 0],
            )

            latest_asc_docs = self._user_activity_docs(
                admin_token,
                {
                    "user_ids": sorted_user_ids,
                    "sort_by": "latest_activity",
                    "sort_order": "asc",
                },
            )
            self.assertEqual(
                [doc["user_id"] for doc in latest_asc_docs],
                [never_id, loginonly_id, messageonly_id, tie_id, both_id],
            )

            page_response = requests.get(
                f"{self.server_url}/_synapse/client/pangea/v1/user_activity",
                params={
                    "user_ids": sorted_user_ids,
                    "sort_by": "latest_activity",
                    "sort_order": "desc",
                    "limit": "2",
                    "page": "2",
                },
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(page_response.status_code, 200, page_response.text)
            page_data = page_response.json()
            self.assertEqual(page_data["page"], 2)
            self.assertEqual(page_data["limit"], 2)
            self.assertEqual(page_data["totalDocs"], 5)
            self.assertEqual(page_data["maxPage"], 3)
            self.assertEqual(
                [doc["user_id"] for doc in page_data["docs"]],
                [tie_id, loginonly_id],
            )

            unfiltered_page_1 = requests.get(
                f"{self.server_url}/_synapse/client/pangea/v1/user_activity",
                params={
                    "sort_by": "latest_activity",
                    "sort_order": "desc",
                    "limit": "3",
                    "page": "1",
                },
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(unfiltered_page_1.status_code, 200, unfiltered_page_1.text)
            unfiltered_page_1_data = unfiltered_page_1.json()
            self.assertEqual(unfiltered_page_1_data["totalDocs"], 6)
            self.assertEqual(
                [doc["user_id"] for doc in unfiltered_page_1_data["docs"]],
                [both_id, messageonly_id, tie_id],
            )

            unfiltered_page_2 = requests.get(
                f"{self.server_url}/_synapse/client/pangea/v1/user_activity",
                params={
                    "sort_by": "latest_activity",
                    "sort_order": "desc",
                    "limit": "3",
                    "page": "2",
                },
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(unfiltered_page_2.status_code, 200, unfiltered_page_2.text)
            self.assertEqual(
                [doc["user_id"] for doc in unfiltered_page_2.json()["docs"]],
                [loginonly_id, admin_id, never_id],
            )

            invalid_sort_response = requests.get(
                f"{self.server_url}/_synapse/client/pangea/v1/user_activity",
                params={"user_ids": sorted_user_ids, "sort_by": "unknown"},
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(invalid_sort_response.status_code, 400)
            self.assertIn("sort_by", invalid_sort_response.json()["error"])

            invalid_order_response = requests.get(
                f"{self.server_url}/_synapse/client/pangea/v1/user_activity",
                params={"user_ids": sorted_user_ids, "sort_order": "newest"},
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(invalid_order_response.status_code, 400)
            self.assertIn("sort_order", invalid_order_response.json()["error"])

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

            # Synapse batches user_ips writes every 5 s, so wait long
            # enough for the flush to complete before querying.
            await asyncio.sleep(6)

            # Query user activity (paginated)
            url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
            response = requests.get(url, headers=headers_admin, timeout=30)
            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Paginated response shape
            self.assertIn("docs", data)
            self.assertIn("page", data)
            self.assertIn("limit", data)
            self.assertIn("totalDocs", data)
            self.assertIn("maxPage", data)

            # Find the learner in the paginated docs
            learner_list = [
                u for u in data["docs"] if u["user_id"] == "@learner:my.domain.name"
            ]
            self.assertEqual(len(learner_list), 1)
            learner_data = learner_list[0]

            self.assertGreater(learner_data["last_message_ts"], 0)
            self.assertGreater(learner_data["last_login_ts"], 0)

            # User docs should NOT contain courses (moved to user_courses endpoint)
            self.assertNotIn("courses", learner_data)
            self.assertNotIn("most_recent_course_room_id", learner_data)

            # Query user_courses endpoint for the learner
            courses_url = (
                f"{self.server_url}/_synapse/client/pangea/v1/user_courses"
                f"?user_id=@learner:my.domain.name"
            )
            courses_resp = requests.get(courses_url, headers=headers_admin, timeout=30)
            self.assertEqual(courses_resp.status_code, 200)
            courses_data = courses_resp.json()

            # Paginated response shape
            self.assertIn("docs", courses_data)
            self.assertIn("page", courses_data)
            self.assertIn("limit", courses_data)
            self.assertIn("totalDocs", courses_data)
            self.assertIn("maxPage", courses_data)
            self.assertEqual(courses_data["user_id"], "@learner:my.domain.name")

            # Learner should have the course in their courses list
            course_rooms = [
                c
                for c in courses_data["docs"]
                if c["room_id"] == course_room_id and c["is_course"]
            ]
            self.assertEqual(len(course_rooms), 1)
            # Course entry should include most_recent_activity_ts
            self.assertIn("most_recent_activity_ts", course_rooms[0])

            # Query course activities endpoint
            activities_url = (
                f"{self.server_url}/_synapse/client/pangea/v1/course_activities"
                f"?course_room_id={course_room_id}"
            )
            activities_resp = requests.get(
                activities_url, headers=headers_admin, timeout=30
            )
            self.assertEqual(activities_resp.status_code, 200)
            activities_data = activities_resp.json()

            self.assertEqual(activities_data["course_room_id"], course_room_id)
            self.assertIn("activities", activities_data)
            self.assertGreater(len(activities_data["activities"]), 0)

            activity_room_entry = [
                a
                for a in activities_data["activities"]
                if a["room_id"] == activity_room_id
            ]
            self.assertEqual(len(activity_room_entry), 1)
            self.assertEqual(activity_room_entry[0]["activity_id"], "activity-456")

            # Test exclude_user_id filter — admin is a member, so excluding admin
            # should still return the activity (learner is not a member of it though)
            exclude_url = (
                f"{self.server_url}/_synapse/client/pangea/v1/course_activities"
                f"?course_room_id={course_room_id}"
                f"&exclude_user_id=@learner:my.domain.name"
            )
            exclude_resp = requests.get(exclude_url, headers=headers_admin, timeout=30)
            self.assertEqual(exclude_resp.status_code, 200)
            exclude_data = exclude_resp.json()
            # Learner did NOT join the activity room, so excluding learner
            # should still return the activity
            exclude_activity_ids = [a["room_id"] for a in exclude_data["activities"]]
            self.assertIn(activity_room_id, exclude_activity_ids)

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

    async def test_user_ids_filter(self):
        """user_ids param restricts results and totalDocs to the given users."""
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
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="user2",
                password="pw2",
                admin=False,
            )
            _, admin_token = await self.login_user("admin", "adminpw")

            url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
            response = requests.get(
                url,
                params={"user_ids": "@user1:my.domain.name,@user2:my.domain.name"},
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            returned_ids = {u["user_id"] for u in data["docs"]}
            self.assertEqual(
                returned_ids,
                {"@user1:my.domain.name", "@user2:my.domain.name"},
            )
            self.assertEqual(data["totalDocs"], 2)
            self.assertNotIn("@admin:my.domain.name", returned_ids)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_course_ids_filter(self):
        """course_ids param returns only members of those course rooms."""
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
                user="member",
                password="pw1",
                admin=False,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="nonmember",
                password="pw2",
                admin=False,
            )
            _, admin_token = await self.login_user("admin", "adminpw")
            _, member_token = await self.login_user("member", "pw1")

            # Create a course room
            course_resp = requests.post(
                f"{self.server_url}/_matrix/client/v3/createRoom",
                json={
                    "preset": "public_chat",
                    "initial_state": [
                        {
                            "type": "pangea.course_plan",
                            "state_key": "",
                            "content": {"uuid": "c1"},
                        },
                    ],
                },
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(course_resp.status_code, 200)
            course_room_id = course_resp.json()["room_id"]

            # member joins the course
            requests.post(
                f"{self.server_url}/_matrix/client/v3/join/{course_room_id}",
                headers={"Authorization": f"Bearer {member_token}"},
                timeout=30,
            )

            url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
            response = requests.get(
                url,
                params={"course_ids": course_room_id},
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            returned_ids = {u["user_id"] for u in data["docs"]}
            # admin (creator) and member joined; nonmember did not
            self.assertIn("@member:my.domain.name", returned_ids)
            self.assertNotIn("@nonmember:my.domain.name", returned_ids)
            self.assertEqual(data["totalDocs"], len(returned_ids))

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_user_ids_course_ids_intersection(self):
        """When both user_ids and course_ids are supplied the result is the intersection."""
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
                user="userA",
                password="pwA",
                admin=False,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="userB",
                password="pwB",
                admin=False,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="userC",
                password="pwC",
                admin=False,
            )
            _, admin_token = await self.login_user("admin", "adminpw")
            user_a_id, token_a = await self.login_user("userA", "pwA")
            user_b_id, _ = await self.login_user("userB", "pwB")
            _, token_c = await self.login_user("userC", "pwC")

            # Create course; A and C join, B does not
            course_resp = requests.post(
                f"{self.server_url}/_matrix/client/v3/createRoom",
                json={
                    "preset": "public_chat",
                    "initial_state": [
                        {
                            "type": "pangea.course_plan",
                            "state_key": "",
                            "content": {"uuid": "c2"},
                        },
                    ],
                },
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(course_resp.status_code, 200)
            course_room_id = course_resp.json()["room_id"]
            requests.post(
                f"{self.server_url}/_matrix/client/v3/join/{course_room_id}",
                headers={"Authorization": f"Bearer {token_a}"},
                timeout=30,
            )
            requests.post(
                f"{self.server_url}/_matrix/client/v3/join/{course_room_id}",
                headers={"Authorization": f"Bearer {token_c}"},
                timeout=30,
            )

            # user_ids = A,B; course has A,C -> intersection = A only.
            url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
            response = requests.get(
                url,
                params={
                    "user_ids": f"{user_a_id},{user_b_id}",
                    "course_ids": course_room_id,
                },
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            returned_ids = {u["user_id"] for u in data["docs"]}
            self.assertEqual(returned_ids, {user_a_id})
            self.assertEqual(data["totalDocs"], 1)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_inactive_days_filter(self):
        """inactive_days excludes recently-active users and includes inactive ones."""
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
                user="inactive_user",
                password="pw1",
                admin=False,
            )
            _, admin_token = await self.login_user("admin", "adminpw")
            inactive_user_id, inactive_token = await self.login_user(
                "inactive_user", "pw1"
            )

            # Send a real message so the inactivity filter has deterministic
            # activity data even when Synapse does not flush user_ips in tests.
            room_id = await self.create_private_room(inactive_token)
            message_resp = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/test-inactive-days",
                json={"msgtype": "m.text", "body": "recent activity"},
                headers={"Authorization": f"Bearer {inactive_token}"},
                timeout=30,
            )
            self.assertEqual(message_resp.status_code, 200)

            # inactive_days=3650 (10 years) excludes users with recent messages.
            url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
            response = requests.get(
                url,
                params={
                    "user_ids": inactive_user_id,
                    "inactive_days": "3650",
                },
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            returned_ids = {u["user_id"] for u in data["docs"]}
            self.assertNotIn(inactive_user_id, returned_ids)
            self.assertEqual(data["totalDocs"], 0)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_inactive_days_never_active_included(self):
        """Users with no login or message history are included with inactive_days."""
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
                user="never_active",
                password="pw1",
                admin=False,
            )
            # Do NOT log in as never_active — no user_ips or events rows
            _, admin_token = await self.login_user("admin", "adminpw")

            url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
            response = requests.get(
                url,
                params={
                    "user_ids": "@never_active:my.domain.name",
                    "inactive_days": "1",
                },
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            returned_ids = {u["user_id"] for u in data["docs"]}
            # last_login_ts=0 and last_message_ts=0 both pass the threshold → included
            self.assertIn("@never_active:my.domain.name", returned_ids)
            self.assertEqual(data["totalDocs"], 1)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_notification_cooldown_requires_bot_config(self):
        """notification_cooldown_ms returns 400 when bot user ID is not configured."""
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
            # No user_activity_notification_bot_user_id in module config

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin",
                password="adminpw",
                admin=True,
            )
            _, admin_token = await self.login_user("admin", "adminpw")

            url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
            response = requests.get(
                url,
                params={"notification_cooldown_ms": "60000"},
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn(
                "user_activity_notification_bot_user_id",
                response.json().get("error", ""),
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_notification_cooldown_excludes_recently_notified(self):
        """Users with a recent p.room.notice in their bot DM are excluded."""
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            bot_user_id = "@bot:my.domain.name"
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config={
                    "user_activity_notification_bot_user_id": bot_user_id,
                }
            )

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
                user="bot",
                password="botpw",
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
            _, bot_token = await self.login_user("bot", "botpw")
            _, learner_token = await self.login_user("learner", "pw1")

            # Create a DM room between bot and learner
            dm_resp = requests.post(
                f"{self.server_url}/_matrix/client/v3/createRoom",
                json={
                    "preset": "trusted_private_chat",
                    "is_direct": True,
                    "invite": ["@learner:my.domain.name"],
                },
                headers={"Authorization": f"Bearer {bot_token}"},
                timeout=30,
            )
            self.assertEqual(dm_resp.status_code, 200)
            dm_room_id = dm_resp.json()["room_id"]

            # Learner accepts invite
            requests.post(
                f"{self.server_url}/_matrix/client/v3/join/{dm_room_id}",
                headers={"Authorization": f"Bearer {learner_token}"},
                timeout=30,
            )

            # Set m.direct account data for learner so the filter can find the DM
            requests.put(
                f"{self.server_url}/_matrix/client/v3/user/@learner:my.domain.name/account_data/m.direct",
                json={bot_user_id: [dm_room_id]},
                headers={"Authorization": f"Bearer {learner_token}"},
                timeout=30,
            )

            # Bot sends a p.room.notice in the DM (simulates a recent notification)
            send_resp = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{dm_room_id}"
                f"/send/p.room.notice/txn-notice-1",
                json={"body": "Hey there!"},
                headers={"Authorization": f"Bearer {bot_token}"},
                timeout=30,
            )
            self.assertEqual(send_resp.status_code, 200)

            # Filter with a large cooldown — learner was just notified
            url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
            response = requests.get(
                url,
                params={
                    "user_ids": "@learner:my.domain.name",
                    "notification_cooldown_ms": str(24 * 60 * 60 * 1000),  # 1 day
                },
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            returned_ids = {u["user_id"] for u in data["docs"]}
            self.assertNotIn("@learner:my.domain.name", returned_ids)
            self.assertEqual(data["totalDocs"], 0)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_notification_cooldown_includes_expired(self):
        """Users whose last bot notice is older than the cooldown are included."""
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            bot_user_id = "@bot:my.domain.name"
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config={
                    "user_activity_notification_bot_user_id": bot_user_id,
                }
            )

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
                user="bot",
                password="botpw",
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
            _, bot_token = await self.login_user("bot", "botpw")
            _, learner_token = await self.login_user("learner", "pw1")

            # Create DM, learner joins
            dm_resp = requests.post(
                f"{self.server_url}/_matrix/client/v3/createRoom",
                json={
                    "preset": "trusted_private_chat",
                    "is_direct": True,
                    "invite": ["@learner:my.domain.name"],
                },
                headers={"Authorization": f"Bearer {bot_token}"},
                timeout=30,
            )
            self.assertEqual(dm_resp.status_code, 200)
            dm_room_id = dm_resp.json()["room_id"]
            requests.post(
                f"{self.server_url}/_matrix/client/v3/join/{dm_room_id}",
                headers={"Authorization": f"Bearer {learner_token}"},
                timeout=30,
            )
            requests.put(
                f"{self.server_url}/_matrix/client/v3/user/@learner:my.domain.name/account_data/m.direct",
                json={bot_user_id: [dm_room_id]},
                headers={"Authorization": f"Bearer {learner_token}"},
                timeout=30,
            )

            # Bot sends a p.room.notice in the DM
            requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{dm_room_id}"
                f"/send/p.room.notice/txn-notice-2",
                json={"body": "Hey there!"},
                headers={"Authorization": f"Bearer {bot_token}"},
                timeout=30,
            )

            # Use a tiny cooldown (1ms) — the notice was sent >1ms ago
            url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
            response = requests.get(
                url,
                params={
                    "user_ids": "@learner:my.domain.name",
                    "notification_cooldown_ms": "1",
                },
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            returned_ids = {u["user_id"] for u in data["docs"]}
            self.assertIn("@learner:my.domain.name", returned_ids)
            self.assertEqual(data["totalDocs"], 1)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
