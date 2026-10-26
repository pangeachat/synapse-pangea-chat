import asyncio
import logging
from typing import Optional

import psycopg2
import requests
import yaml

from .base_e2e import SERVER_NAME, BaseSynapseE2ETest, Homeserver

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    filename="synapse.log",
    filemode="w",
)

NOTIFICATION_BOT_USER_ID = f"@bot:{SERVER_NAME}"


class _RefreshOnRead:
    """These classes' servers rebuild the kept activity table on every read, so the listing's tests
    need no refresh call between seeding and reading; the refresh itself is TestUserActivityRefreshE2E's.
    """

    async def homeserver(self, *, module_config=None, synapse_config_overrides=None):  # type: ignore[override]
        merged = {"user_activity_refresh_on_read": True, **(module_config or {})}
        return await super().homeserver(  # type: ignore[misc]
            module_config=merged, synapse_config_overrides=synapse_config_overrides
        )


class _ActivitySeeds:
    """Deterministic activity facts written straight to the database, so a test never waits on
    Synapse's periodic user_ips flush."""

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
                self.assertEqual(cursor.rowcount, 1)  # type: ignore[attr-defined]


class TestUserActivityE2E(_RefreshOnRead, BaseSynapseE2ETest):
    def _every_user_activity_doc(self, admin_token: str) -> list[dict]:
        """The unfiltered listing, every page of it.

        Earlier tests' users share the class's homeserver, so this test's
        users can sit past the first page.
        """
        docs: list[dict] = []
        page = 1
        while True:
            response = requests.get(
                f"{self.server_url}/_synapse/client/pangea/v1/user_activity",
                params={"page": str(page)},
                headers={"Authorization": f"Bearer {admin_token}"},
                timeout=30,
            )
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            docs.extend(data["docs"])
            if page >= data["maxPage"]:
                return docs
            page += 1

    async def test_user_activity_requires_admin(self):
        """Non-admin users should get 403 from the user_activity endpoint."""
        await self.homeserver()

        # Register a non-admin user
        _, token = await self.new_user("regular_user")

        # Try to access user_activity endpoint
        url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("admin", response.json().get("error", "").lower())

    async def test_user_activity_admin_access(self):
        """Admin users should get paginated user activity data."""
        await self.homeserver()

        # Register an admin user and a regular user
        admin_id, admin_token = await self.new_user("admin", admin=True)
        user1_id, _ = await self.new_user("user1")

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

        # Both users (admin + user1) are listed
        user_ids = [u["user_id"] for u in self._every_user_activity_doc(admin_token)]
        self.assertIn(admin_id, user_ids)
        self.assertIn(user1_id, user_ids)

    async def test_user_activity_with_rooms_and_messages(self):
        """Verify the endpoint returns correct activity data including
        room memberships, last message timestamps, and course/activity info."""
        await self.homeserver()

        # Register admin + regular user
        _, admin_token = await self.new_user("admin", admin=True)
        learner_id, learner_token = await self.new_user("learner")

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
            u
            for u in self._every_user_activity_doc(admin_token)
            if u["user_id"] == learner_id
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
            f"?user_id={learner_id}"
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
        self.assertEqual(courses_data["user_id"], learner_id)

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
            a for a in activities_data["activities"] if a["room_id"] == activity_room_id
        ]
        self.assertEqual(len(activity_room_entry), 1)
        self.assertEqual(activity_room_entry[0]["activity_id"], "activity-456")

        # Test exclude_user_id filter — admin is a member, so excluding admin
        # should still return the activity (learner is not a member of it though)
        exclude_url = (
            f"{self.server_url}/_synapse/client/pangea/v1/course_activities"
            f"?course_room_id={course_room_id}"
            f"&exclude_user_id={learner_id}"
        )
        exclude_resp = requests.get(exclude_url, headers=headers_admin, timeout=30)
        self.assertEqual(exclude_resp.status_code, 200)
        exclude_data = exclude_resp.json()
        # Learner did NOT join the activity room, so excluding learner
        # should still return the activity
        exclude_activity_ids = [a["room_id"] for a in exclude_data["activities"]]
        self.assertIn(activity_room_id, exclude_activity_ids)

    async def test_user_activity_unauthorized(self):
        """Requests without auth should get 401."""
        await self.homeserver()

        url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
        response = requests.get(url, timeout=30)
        self.assertEqual(response.status_code, 401)

    async def test_user_ids_filter(self):
        """user_ids param restricts results and totalDocs to the given users."""
        await self.homeserver()

        admin_id, admin_token = await self.new_user("admin", admin=True)
        user1_id, _ = await self.new_user("user1")
        user2_id, _ = await self.new_user("user2")

        url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
        response = requests.get(
            url,
            params={"user_ids": f"{user1_id},{user2_id}"},
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=30,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()

        returned_ids = {u["user_id"] for u in data["docs"]}
        self.assertEqual(returned_ids, {user1_id, user2_id})
        self.assertEqual(data["totalDocs"], 2)
        self.assertNotIn(admin_id, returned_ids)

    async def test_course_ids_filter(self):
        """course_ids param returns only members of those course rooms."""
        await self.homeserver()

        _, admin_token = await self.new_user("admin", admin=True)
        member_id, member_token = await self.new_user("member")
        nonmember_id, _ = await self.new_user("nonmember")

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
        self.assertIn(member_id, returned_ids)
        self.assertNotIn(nonmember_id, returned_ids)
        self.assertEqual(data["totalDocs"], len(returned_ids))

    async def test_user_ids_course_ids_intersection(self):
        """When both user_ids and course_ids are supplied the result is the intersection."""
        await self.homeserver()

        _, admin_token = await self.new_user("admin", admin=True)
        user_a_id, token_a = await self.new_user("userA")
        user_b_id, _ = await self.new_user("userB")
        _, token_c = await self.new_user("userC")

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

    async def test_inactive_days_filter(self):
        """inactive_days excludes recently-active users and includes inactive ones."""
        await self.homeserver()

        _, admin_token = await self.new_user("admin", admin=True)
        inactive_user_id, inactive_token = await self.new_user("inactive_user")

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

    async def test_inactive_days_never_active_included(self):
        """Users with no login or message history are included with inactive_days."""
        await self.homeserver()

        _, admin_token = await self.new_user("admin", admin=True)
        # Never use never_active's token — no user_ips or events rows
        never_active_id, _ = await self.new_user("never_active")

        url = f"{self.server_url}/_synapse/client/pangea/v1/user_activity"
        response = requests.get(
            url,
            params={
                "user_ids": never_active_id,
                "inactive_days": "1",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=30,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        returned_ids = {u["user_id"] for u in data["docs"]}
        # last_login_ts=0 and last_message_ts=0 both pass the threshold → included
        self.assertIn(never_active_id, returned_ids)
        self.assertEqual(data["totalDocs"], 1)

    async def test_notification_cooldown_requires_bot_config(self):
        """notification_cooldown_ms returns 400 when bot user ID is not configured."""
        await self.homeserver()
        # No user_activity_notification_bot_user_id in module config

        _, admin_token = await self.new_user("admin", admin=True)

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


class TestUserActivityGlobalSortE2E(_RefreshOnRead, _ActivitySeeds, BaseSynapseE2ETest):
    """Sorting across the server's whole user list, before pagination.

    Its unfiltered pages assert exactly which users the server holds and in
    what order, so it needs a homeserver holding only its own users: this
    class holds this one test, and gets a server of its own.
    """

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

    async def test_user_activity_sorting_and_latest_activity_pagination(self):
        """Sort params order users globally before pagination and expose the sort key."""
        config_path = (await self.homeserver()).config_path

        admin_id, admin_token = await self.new_user("admin", admin=True)
        loginonly_id, _ = await self.new_user("loginonly")
        messageonly_id, messageonly_token = await self.new_user("messageonly")
        both_id, both_token = await self.new_user("both")
        never_id, _ = await self.new_user("never")
        tie_id, _ = await self.new_user("tie")

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


class TestUserActivityNotificationCooldownE2E(BaseSynapseE2ETest):
    """notification_cooldown_ms, with the notification bot configured."""

    # The bot's ID is in the module config, so it has a fixed localpart and is
    # registered once per homeserver: this is the server it was registered on.
    _bot_registered_on: Optional[Homeserver] = None

    async def _start_with_bot(self) -> str:
        """Start the class's homeserver; returns an access token for the bot."""
        homeserver = await self.homeserver(
            module_config={
                "user_activity_notification_bot_user_id": NOTIFICATION_BOT_USER_ID,
            }
        )
        cls = type(self)
        if cls._bot_registered_on is not homeserver:
            await self.register_user(
                config_path=homeserver.config_path,
                dir=homeserver.synapse_dir,
                user="bot",
                password="botpw",
                admin=True,
            )
            cls._bot_registered_on = homeserver
        _, bot_token = await self.login_user("bot", "botpw")
        return bot_token

    async def test_notification_cooldown_excludes_recently_notified(self):
        """Users with a recent p.room.notice in their bot DM are excluded."""
        bot_user_id = NOTIFICATION_BOT_USER_ID
        bot_token = await self._start_with_bot()

        _, admin_token = await self.new_user("admin", admin=True)
        learner_id, learner_token = await self.new_user("learner")

        # Create a DM room between bot and learner
        dm_resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json={
                "preset": "trusted_private_chat",
                "is_direct": True,
                "invite": [learner_id],
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
            f"{self.server_url}/_matrix/client/v3/user/{learner_id}/account_data/m.direct",
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
                "user_ids": learner_id,
                "notification_cooldown_ms": str(24 * 60 * 60 * 1000),  # 1 day
            },
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=30,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        returned_ids = {u["user_id"] for u in data["docs"]}
        self.assertNotIn(learner_id, returned_ids)
        self.assertEqual(data["totalDocs"], 0)

    async def test_notification_cooldown_includes_expired(self):
        """Users whose last bot notice is older than the cooldown are included."""
        bot_user_id = NOTIFICATION_BOT_USER_ID
        bot_token = await self._start_with_bot()

        _, admin_token = await self.new_user("admin", admin=True)
        learner_id, learner_token = await self.new_user("learner")

        # Create DM, learner joins
        dm_resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json={
                "preset": "trusted_private_chat",
                "is_direct": True,
                "invite": [learner_id],
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
            f"{self.server_url}/_matrix/client/v3/user/{learner_id}/account_data/m.direct",
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
                "user_ids": learner_id,
                "notification_cooldown_ms": "1",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=30,
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        returned_ids = {u["user_id"] for u in data["docs"]}
        self.assertIn(learner_id, returned_ids)
        self.assertEqual(data["totalDocs"], 1)


class TestUserActivityRefreshE2E(_ActivitySeeds, BaseSynapseE2ETest):
    """The kept activity table and its refresh (user-activity.instructions.md, "DB query strategy").

    This server's loop is off, so every refresh here is the test's own call.
    """

    def _refresh(self, token: str, *, full: bool = False) -> requests.Response:
        return requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/user_activity_refresh",
            params={"full": "true"} if full else None,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )

    def _doc(self, admin_token: str, user_id: str) -> tuple[dict, dict]:
        response = requests.get(
            f"{self.server_url}/_synapse/client/pangea/v1/user_activity",
            params={"user_ids": user_id},
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=30,
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(len(data["docs"]), 1, data)
        return data["docs"][0], data

    async def test_the_table_is_filled_by_a_refresh_not_by_the_read(self):
        """Before any refresh the read answers zeros and no refresh time; an incremental refresh folds in
        a message sent since; only an admin may refresh."""
        await self.homeserver(
            module_config={"user_activity_refresh_interval_seconds": 0}
        )
        _, admin_token = await self.new_user("admin", admin=True)
        learner_id, learner_token = await self.new_user("learner")

        self.assertEqual(self._refresh(learner_token).status_code, 403)

        # A new user has no row until a refresh folds one in, whatever earlier tests refreshed.
        doc, data = self._doc(admin_token, learner_id)
        self.assertEqual(
            (doc["last_login_ts"], doc["last_message_ts"], doc["latest_activity_ts"]),
            (0, 0, 0),
        )

        room_id = await self.create_private_room(learner_token)
        sent = requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/txn-refresh-1",
            json={"msgtype": "m.text", "body": "hello"},
            headers={"Authorization": f"Bearer {learner_token}"},
            timeout=30,
        )
        self.assertEqual(sent.status_code, 200, sent.text)

        first = self._refresh(admin_token)
        self.assertEqual(first.status_code, 200, first.text)
        out = first.json()
        self.assertFalse(out["full"])
        self.assertGreaterEqual(out["messages_upserted"], 1)
        self.assertGreaterEqual(out["event_chunks"], 1)

        doc, data = self._doc(admin_token, learner_id)
        self.assertGreater(doc["last_message_ts"], 0)
        self.assertEqual(
            doc["latest_activity_ts"], max(doc["last_login_ts"], doc["last_message_ts"])
        )
        self.assertEqual(data["activity_refreshed_at"], out["refreshed_at"])

        # Nothing new: the next incremental refresh folds nothing in and the table stands.
        again = self._refresh(admin_token).json()
        self.assertEqual((again["messages_upserted"], again["event_chunks"]), (0, 0))
        self.assertEqual(
            self._doc(admin_token, learner_id)[0]["last_message_ts"],
            doc["last_message_ts"],
        )

    async def test_a_full_refresh_rebuilds_from_the_facts_as_they_are_now(self):
        """A fact changed behind the watermarks (a seeded login, an event's time moved) is not seen by an
        incremental refresh and is by a full one."""
        config_path = (
            await self.homeserver(
                module_config={"user_activity_refresh_interval_seconds": 0}
            )
        ).config_path
        _, admin_token = await self.new_user("admin", admin=True)
        learner_id, learner_token = await self.new_user("rebuilt")
        room_id = await self.create_private_room(learner_token)
        sent = requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/txn-refresh-2",
            json={"msgtype": "m.text", "body": "before"},
            headers={"Authorization": f"Bearer {learner_token}"},
            timeout=30,
        )
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(self._refresh(admin_token).status_code, 200)
        before = self._doc(admin_token, learner_id)[0]
        self.assertGreater(before["last_message_ts"], 0)

        self._replace_last_login_ts(config_path, learner_id, 5_000)
        self._set_event_origin_server_ts(config_path, sent.json()["event_id"], 7_000)

        incremental = self._refresh(admin_token).json()
        self.assertEqual(incremental["event_chunks"], 0)
        self.assertEqual(
            self._doc(admin_token, learner_id)[0]["last_message_ts"],
            before["last_message_ts"],
        )

        full = self._refresh(admin_token, full=True).json()
        self.assertTrue(full["full"])
        doc = self._doc(admin_token, learner_id)[0]
        self.assertEqual(
            (doc["last_login_ts"], doc["last_message_ts"], doc["latest_activity_ts"]),
            (5_000, 7_000, 7_000),
        )
