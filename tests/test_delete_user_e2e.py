import asyncio

import requests
from psycopg2 import connect

from .base_e2e import SERVER_NAME, BaseSynapseE2ETest


class _DeleteUserHelpers:
    _SCHEDULE_TABLE = "pangea_delete_user_schedule"

    def _delete_user_url(self) -> str:
        return f"{self.server_url}/_synapse/client/pangea/v1/delete_user"

    def _login_url(self) -> str:
        return f"{self.server_url}/_matrix/client/v3/login"

    def _insert_threepid(self, user_id: str, address: str) -> None:
        conn = connect(self.database_url)
        try:
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_threepids (medium, address, user_id, validated_at, added_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                ("email", address, user_id, 1, 1),
            )
            cursor.close()
        finally:
            conn.close()

    def _count_threepids(self, user_id: str) -> int:
        conn = connect(self.database_url)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM user_threepids WHERE user_id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
            cursor.close()
            return row[0] if row else 0
        finally:
            conn.close()

    def _insert_external_id(
        self,
        user_id: str,
        auth_provider: str,
        external_id: str,
    ) -> None:
        conn = connect(self.database_url)
        try:
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO user_external_ids (auth_provider, external_id, user_id)
                VALUES (%s, %s, %s)
                """,
                (auth_provider, external_id, user_id),
            )
            cursor.close()
        finally:
            conn.close()

    def _count_external_ids(self, user_id: str) -> int:
        conn = connect(self.database_url)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM user_external_ids WHERE user_id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
            cursor.close()
            return row[0] if row else 0
        finally:
            conn.close()

    def _insert_schedule(self, user_id: str, execute_at_ms: int) -> None:
        conn = connect(self.database_url)
        try:
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute(
                f"""
                INSERT INTO {self._SCHEDULE_TABLE}
                    (user_id, execute_at_ms, requested_at_ms, requested_by, requested_by_admin)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, execute_at_ms, 1, user_id, False),
            )
            cursor.close()
        finally:
            conn.close()

    def _schedule_table_exists(self) -> bool:
        conn = connect(self.database_url)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
                (self._SCHEDULE_TABLE,),
            )
            row = cursor.fetchone()
            cursor.close()
            return row is not None
        finally:
            conn.close()

    def _count_schedules(self, user_id: str) -> int:
        conn = connect(self.database_url)
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT COUNT(*) FROM {self._SCHEDULE_TABLE} WHERE user_id = %s",
                (user_id,),
            )
            row = cursor.fetchone()
            cursor.close()
            return row[0] if row else 0
        finally:
            conn.close()


class TestDeleteUserE2E(_DeleteUserHelpers, BaseSynapseE2ETest):
    """The endpoint's contract, with the default 7-day delay and a dead CMS.

    No schedule made here comes due while the class runs, so the background
    processor never touches one; a test's schedule moves only when it acts.
    """

    _MODULE_CONFIG = {
        "cms_base_url": "http://127.0.0.1:9",
        "cms_service_api_key": "test-key",
    }

    async def test_delete_user_self_schedule_then_force(self):
        await self.homeserver(module_config=self._MODULE_CONFIG)

        user_id, access_token = await self.new_user("selfdelete", password="pw1")

        self._insert_threepid(user_id, f"selfdelete_{self.tag}@example.com")
        self._insert_external_id(
            user_id,
            "oidc",
            f"selfdelete-external-id-{self.tag}",
        )
        self.assertEqual(self._count_threepids(user_id), 1)
        self.assertEqual(self._count_external_ids(user_id), 1)

        response = requests.post(
            self._delete_user_url(),
            headers={"Authorization": f"Bearer {access_token}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Delete scheduled")
        self.assertEqual(response.json()["action"], "schedule")
        self.assertEqual(response.json()["user_id"], user_id)
        self.assertEqual(self._count_schedules(user_id), 1)

        login_response = requests.post(
            self._login_url(),
            json={
                "type": "m.login.password",
                "user": self.localpart("selfdelete"),
                "password": "pw1",
            },
        )
        self.assertEqual(login_response.status_code, 200)

        force_response = requests.post(
            self._delete_user_url(),
            json={"action": "force"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(force_response.status_code, 200)
        self.assertEqual(force_response.json()["message"], "Deleted")
        self.assertEqual(force_response.json()["action"], "force")
        self.assertEqual(force_response.json()["deleted_threepids"], 1)
        self.assertEqual(force_response.json()["deleted_external_ids"], 1)
        self.assertEqual(self._count_threepids(user_id), 0)
        self.assertEqual(self._count_external_ids(user_id), 0)
        self.assertEqual(self._count_schedules(user_id), 0)

        login_response = requests.post(
            self._login_url(),
            json={
                "type": "m.login.password",
                "user": self.localpart("selfdelete"),
                "password": "pw1",
            },
        )
        self.assertNotEqual(login_response.status_code, 200)

    async def test_delete_user_admin_can_cancel_and_force_target_schedule(self):
        await self.homeserver(module_config=self._MODULE_CONFIG)

        _, admin_token = await self.new_user("admin", admin=True, password="pw1")
        target_user_id, _ = await self.new_user("target", password="pw2")
        self._insert_threepid(target_user_id, f"target_{self.tag}@example.com")
        self._insert_external_id(
            target_user_id,
            "oidc",
            f"target-external-id-{self.tag}",
        )
        self.assertEqual(self._count_threepids(target_user_id), 1)
        self.assertEqual(self._count_external_ids(target_user_id), 1)

        response = requests.post(
            self._delete_user_url(),
            json={"user_id": target_user_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Delete scheduled")
        self.assertEqual(response.json()["action"], "schedule")
        self.assertEqual(response.json()["user_id"], target_user_id)
        self.assertEqual(self._count_schedules(target_user_id), 1)

        cancel_response = requests.post(
            self._delete_user_url(),
            json={"action": "cancel", "user_id": target_user_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(cancel_response.status_code, 200)
        self.assertEqual(cancel_response.json()["action"], "cancel")
        self.assertTrue(cancel_response.json()["canceled"])
        self.assertEqual(self._count_schedules(target_user_id), 0)

        force_without_schedule_response = requests.post(
            self._delete_user_url(),
            json={"action": "force", "user_id": target_user_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(force_without_schedule_response.status_code, 400)

        reschedule_response = requests.post(
            self._delete_user_url(),
            json={"user_id": target_user_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(reschedule_response.status_code, 200)
        self.assertEqual(self._count_schedules(target_user_id), 1)

        force_response = requests.post(
            self._delete_user_url(),
            json={"action": "force", "user_id": target_user_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(force_response.status_code, 200)
        self.assertEqual(force_response.json()["action"], "force")
        self.assertEqual(force_response.json()["deleted_threepids"], 1)
        self.assertEqual(force_response.json()["deleted_external_ids"], 1)
        self.assertEqual(self._count_threepids(target_user_id), 0)
        self.assertEqual(self._count_external_ids(target_user_id), 0)
        self.assertEqual(self._count_schedules(target_user_id), 0)

        login_response = requests.post(
            self._login_url(),
            json={
                "type": "m.login.password",
                "user": self.localpart("target"),
                "password": "pw2",
            },
        )
        self.assertNotEqual(login_response.status_code, 200)

    async def test_delete_user_rejects_non_local_target(self):
        await self.homeserver(module_config=self._MODULE_CONFIG)

        _, admin_token = await self.new_user("admin2", admin=True, password="pw1")

        response = requests.post(
            self._delete_user_url(),
            json={"user_id": "@remote:other.domain"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("local users", response.json().get("error", ""))

    async def test_delete_user_non_admin_cannot_target_another_user(self):
        await self.homeserver(module_config=self._MODULE_CONFIG)

        _, user1_token = await self.new_user("user1", password="pw1")
        user2_id, _ = await self.new_user("user2", password="pw2")

        response = requests.post(
            self._delete_user_url(),
            json={"user_id": user2_id},
            headers={"Authorization": f"Bearer {user1_token}"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("server admin required", response.json().get("error", ""))

    async def test_delete_user_succeeds_when_cms_unavailable(self):
        """Dead CMS URL → account still deactivated."""
        await self.homeserver(module_config=self._MODULE_CONFIG)

        _, access_token = await self.new_user("deadcms", password="pw1")

        requests.post(
            self._delete_user_url(),
            headers={"Authorization": f"Bearer {access_token}"},
        )
        force = requests.post(
            self._delete_user_url(),
            json={"action": "force"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(force.status_code, 200)

        login_response = requests.post(
            self._login_url(),
            json={
                "type": "m.login.password",
                "user": self.localpart("deadcms"),
                "password": "pw1",
            },
        )
        self.assertNotEqual(login_response.status_code, 200)


class TestDeleteUserProcessor(_DeleteUserHelpers, BaseSynapseE2ETest):
    """The background processor, running every second over a 3-second delay.

    It acts on every due schedule on the server, so each test asserts only on
    the schedule it made, and a schedule another test left behind changes
    nothing it checks.
    """

    _MODULE_CONFIG = {
        "delete_user_schedule_delay_seconds": 3,
        "delete_user_processor_interval_seconds": 1,
    }

    async def test_scheduled_delete_executes_with_short_config_delay(self):
        await self.homeserver(module_config=self._MODULE_CONFIG)

        user_id, access_token = await self.new_user("scheduleduser", password="pw1")

        self._insert_threepid(user_id, f"scheduled_{self.tag}@example.com")
        self._insert_external_id(
            user_id,
            "oidc",
            f"scheduled-external-id-{self.tag}",
        )
        self.assertEqual(self._count_threepids(user_id), 1)
        self.assertEqual(self._count_external_ids(user_id), 1)

        schedule_response = requests.post(
            self._delete_user_url(),
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(schedule_response.status_code, 200)
        self.assertEqual(schedule_response.json()["action"], "schedule")
        self.assertEqual(self._count_schedules(user_id), 1)

        await asyncio.sleep(6)

        self.assertEqual(self._count_threepids(user_id), 0)
        self.assertEqual(self._count_external_ids(user_id), 0)
        self.assertEqual(self._count_schedules(user_id), 0)

        login_response = requests.post(
            self._login_url(),
            json={
                "type": "m.login.password",
                "user": self.localpart("scheduleduser"),
                "password": "pw1",
            },
        )
        self.assertNotEqual(login_response.status_code, 200)

    async def test_scheduled_delete_for_missing_user_stops_retrying(self):
        """Schedule for an already-deleted user must fail terminally, not retry forever.

        Regression test for the staging incident where a schedule for a user
        whose row was already gone retried every processor interval for days
        with "404: No row found (users)".
        """
        await self.homeserver(module_config=self._MODULE_CONFIG)

        # Wait for the background processor loop to create the schedule
        # table, then plant a due schedule for a user that does not exist.
        for _ in range(30):
            if self._schedule_table_exists():
                break
            await asyncio.sleep(1)
        else:
            self.fail("Schedule table was never created")

        ghost_user_id = f"@{self.localpart('ghostuser')}:{SERVER_NAME}"
        self._insert_schedule(ghost_user_id, execute_at_ms=1)
        self.assertEqual(self._count_schedules(ghost_user_id), 1)

        # The schedule must be dropped as terminally failed. Without
        # bounded retries the row is re-inserted every cycle forever.
        for _ in range(30):
            if self._count_schedules(ghost_user_id) == 0:
                break
            await asyncio.sleep(1)
        else:
            self.fail("Schedule for missing user was still being retried")

        # It must stay gone across further processor cycles (i.e. it was
        # not re-inserted by another retry).
        await asyncio.sleep(3)
        self.assertEqual(self._count_schedules(ghost_user_id), 0)
