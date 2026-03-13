import asyncio

import requests
import yaml
from psycopg2 import connect

from .base_e2e import BaseSynapseE2ETest
from .mock_cms_server import MockCmsServer


class TestDeleteUserE2E(BaseSynapseE2ETest):
    _SCHEDULE_TABLE = "pangea_delete_user_schedule"

    def _db_args_from_config(self, config_path: str) -> dict:
        with open(config_path, "r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)
        database = config.get("database", {})
        args = database.get("args", {})
        return args

    def _insert_threepid(self, config_path: str, user_id: str, address: str) -> None:
        db_args = self._db_args_from_config(config_path)
        conn = connect(**db_args)
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

    def _count_threepids(self, config_path: str, user_id: str) -> int:
        db_args = self._db_args_from_config(config_path)
        conn = connect(**db_args)
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
        config_path: str,
        user_id: str,
        auth_provider: str,
        external_id: str,
    ) -> None:
        db_args = self._db_args_from_config(config_path)
        conn = connect(**db_args)
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

    def _count_external_ids(self, config_path: str, user_id: str) -> int:
        db_args = self._db_args_from_config(config_path)
        conn = connect(**db_args)
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

    def _count_schedules(self, config_path: str, user_id: str) -> int:
        db_args = self._db_args_from_config(config_path)
        conn = connect(**db_args)
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

    async def test_delete_user_self_schedule_then_force(self):
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
                user="selfdelete",
                password="pw1",
                admin=False,
            )
            _, access_token = await self.login_user("selfdelete", "pw1")
            user_id = "@selfdelete:my.domain.name"

            self._insert_threepid(config_path, user_id, "selfdelete@example.com")
            self._insert_external_id(
                config_path,
                user_id,
                "oidc",
                "selfdelete-external-id",
            )
            self.assertEqual(self._count_threepids(config_path, user_id), 1)
            self.assertEqual(self._count_external_ids(config_path, user_id), 1)

            delete_user_url = (
                "http://localhost:8008/_synapse/client/pangea/v1/delete_user"
            )
            response = requests.post(
                delete_user_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["message"], "Delete scheduled")
            self.assertEqual(response.json()["action"], "schedule")
            self.assertEqual(response.json()["user_id"], user_id)
            self.assertEqual(self._count_schedules(config_path, user_id), 1)

            login_url = "http://localhost:8008/_matrix/client/v3/login"
            login_response = requests.post(
                login_url,
                json={
                    "type": "m.login.password",
                    "user": "selfdelete",
                    "password": "pw1",
                },
            )
            self.assertEqual(login_response.status_code, 200)

            force_response = requests.post(
                delete_user_url,
                json={"action": "force"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(force_response.status_code, 200)
            self.assertEqual(force_response.json()["message"], "Deleted")
            self.assertEqual(force_response.json()["action"], "force")
            self.assertEqual(force_response.json()["deleted_threepids"], 1)
            self.assertEqual(force_response.json()["deleted_external_ids"], 1)
            self.assertEqual(self._count_threepids(config_path, user_id), 0)
            self.assertEqual(self._count_external_ids(config_path, user_id), 0)
            self.assertEqual(self._count_schedules(config_path, user_id), 0)

            login_response = requests.post(
                login_url,
                json={
                    "type": "m.login.password",
                    "user": "selfdelete",
                    "password": "pw1",
                },
            )
            self.assertNotEqual(login_response.status_code, 200)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_delete_user_admin_can_cancel_and_force_target_schedule(self):
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
                password="pw1",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="target",
                password="pw2",
                admin=False,
            )
            target_user_id = "@target:my.domain.name"
            self._insert_threepid(config_path, target_user_id, "target@example.com")
            self._insert_external_id(
                config_path,
                target_user_id,
                "oidc",
                "target-external-id",
            )
            self.assertEqual(self._count_threepids(config_path, target_user_id), 1)
            self.assertEqual(self._count_external_ids(config_path, target_user_id), 1)

            _, admin_token = await self.login_user("admin", "pw1")

            delete_user_url = (
                "http://localhost:8008/_synapse/client/pangea/v1/delete_user"
            )
            response = requests.post(
                delete_user_url,
                json={"user_id": "@target:my.domain.name"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["message"], "Delete scheduled")
            self.assertEqual(response.json()["action"], "schedule")
            self.assertEqual(response.json()["user_id"], target_user_id)
            self.assertEqual(self._count_schedules(config_path, target_user_id), 1)

            cancel_response = requests.post(
                delete_user_url,
                json={"action": "cancel", "user_id": target_user_id},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            self.assertEqual(cancel_response.status_code, 200)
            self.assertEqual(cancel_response.json()["action"], "cancel")
            self.assertTrue(cancel_response.json()["canceled"])
            self.assertEqual(self._count_schedules(config_path, target_user_id), 0)

            force_without_schedule_response = requests.post(
                delete_user_url,
                json={"action": "force", "user_id": target_user_id},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            self.assertEqual(force_without_schedule_response.status_code, 400)

            reschedule_response = requests.post(
                delete_user_url,
                json={"user_id": target_user_id},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            self.assertEqual(reschedule_response.status_code, 200)
            self.assertEqual(self._count_schedules(config_path, target_user_id), 1)

            force_response = requests.post(
                delete_user_url,
                json={"action": "force", "user_id": target_user_id},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            self.assertEqual(force_response.status_code, 200)
            self.assertEqual(force_response.json()["action"], "force")
            self.assertEqual(force_response.json()["deleted_threepids"], 1)
            self.assertEqual(force_response.json()["deleted_external_ids"], 1)
            self.assertEqual(self._count_threepids(config_path, target_user_id), 0)
            self.assertEqual(self._count_external_ids(config_path, target_user_id), 0)
            self.assertEqual(self._count_schedules(config_path, target_user_id), 0)

            login_url = "http://localhost:8008/_matrix/client/v3/login"
            login_response = requests.post(
                login_url,
                json={
                    "type": "m.login.password",
                    "user": "target",
                    "password": "pw2",
                },
            )
            self.assertNotEqual(login_response.status_code, 200)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_delete_user_rejects_non_local_target(self):
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
                user="admin2",
                password="pw1",
                admin=True,
            )

            _, admin_token = await self.login_user("admin2", "pw1")

            delete_user_url = (
                "http://localhost:8008/_synapse/client/pangea/v1/delete_user"
            )
            response = requests.post(
                delete_user_url,
                json={"user_id": "@remote:other.domain"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("local users", response.json().get("error", ""))
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_delete_user_non_admin_cannot_target_another_user(self):
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

            _, user1_token = await self.login_user("user1", "pw1")

            delete_user_url = (
                "http://localhost:8008/_synapse/client/pangea/v1/delete_user"
            )
            response = requests.post(
                delete_user_url,
                json={"user_id": "@user2:my.domain.name"},
                headers={"Authorization": f"Bearer {user1_token}"},
            )

            self.assertEqual(response.status_code, 403)
            self.assertIn("server admin required", response.json().get("error", ""))
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_scheduled_delete_executes_with_short_config_delay(self):
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
                module_config={
                    "delete_user_schedule_delay_seconds": 3,
                    "delete_user_processor_interval_seconds": 1,
                }
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="scheduleduser",
                password="pw1",
                admin=False,
            )
            user_id = "@scheduleduser:my.domain.name"
            _, access_token = await self.login_user("scheduleduser", "pw1")

            self._insert_threepid(config_path, user_id, "scheduled@example.com")
            self._insert_external_id(
                config_path,
                user_id,
                "oidc",
                "scheduled-external-id",
            )
            self.assertEqual(self._count_threepids(config_path, user_id), 1)
            self.assertEqual(self._count_external_ids(config_path, user_id), 1)

            delete_user_url = (
                "http://localhost:8008/_synapse/client/pangea/v1/delete_user"
            )
            schedule_response = requests.post(
                delete_user_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(schedule_response.status_code, 200)
            self.assertEqual(schedule_response.json()["action"], "schedule")
            self.assertEqual(self._count_schedules(config_path, user_id), 1)

            await asyncio.sleep(6)

            self.assertEqual(self._count_threepids(config_path, user_id), 0)
            self.assertEqual(self._count_external_ids(config_path, user_id), 0)
            self.assertEqual(self._count_schedules(config_path, user_id), 0)

            login_url = "http://localhost:8008/_matrix/client/v3/login"
            login_response = requests.post(
                login_url,
                json={
                    "type": "m.login.password",
                    "user": "scheduleduser",
                    "password": "pw1",
                },
            )
            self.assertNotEqual(login_response.status_code, 200)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    # ---- CMS feedback-log delete tests ----

    async def test_delete_user_removes_cms_feedback_logs(self):
        """Seed 2 logs → force delete → response reports 2 deleted, mock confirms."""
        mock_cms = MockCmsServer()
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            cms_url = mock_cms.start()
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config={
                    "cms_base_url": cms_url,
                    "cms_service_api_key": "test-key",
                }
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="delcms",
                password="pw1",
                admin=False,
            )
            _, access_token = await self.login_user("delcms", "pw1")
            user_id = "@delcms:my.domain.name"

            mock_cms.seed_feedback_logs(
                user_id,
                [
                    {"id": "fb1", "req": {"user_id": user_id}},
                    {"id": "fb2", "req": {"user_id": user_id}},
                ],
            )

            delete_url = "http://localhost:8008/_synapse/client/pangea/v1/delete_user"
            requests.post(
                delete_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            force = requests.post(
                delete_url,
                json={"action": "force"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(force.status_code, 200)
            self.assertEqual(force.json()["deleted_cms_feedback_logs"], 2)
            self.assertEqual(mock_cms.get_remaining_logs(user_id), [])
        finally:
            mock_cms.stop()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_delete_user_reports_zero_when_no_feedback_logs(self):
        """No seeded data → deleted_cms_feedback_logs: 0."""
        mock_cms = MockCmsServer()
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            cms_url = mock_cms.start()
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config={
                    "cms_base_url": cms_url,
                    "cms_service_api_key": "test-key",
                }
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="nocms",
                password="pw1",
                admin=False,
            )
            _, access_token = await self.login_user("nocms", "pw1")

            delete_url = "http://localhost:8008/_synapse/client/pangea/v1/delete_user"
            requests.post(
                delete_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            force = requests.post(
                delete_url,
                json={"action": "force"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(force.status_code, 200)
            self.assertEqual(force.json()["deleted_cms_feedback_logs"], 0)
        finally:
            mock_cms.stop()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_delete_user_succeeds_when_cms_unavailable(self):
        """Dead CMS URL → account still deactivated, feedback logs = 0."""
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
                module_config={
                    "cms_base_url": "http://127.0.0.1:9",
                    "cms_service_api_key": "test-key",
                }
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="deadcms",
                password="pw1",
                admin=False,
            )
            _, access_token = await self.login_user("deadcms", "pw1")

            delete_url = "http://localhost:8008/_synapse/client/pangea/v1/delete_user"
            requests.post(
                delete_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            force = requests.post(
                delete_url,
                json={"action": "force"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(force.status_code, 200)
            self.assertEqual(force.json()["deleted_cms_feedback_logs"], 0)

            login_url = "http://localhost:8008/_matrix/client/v3/login"
            login_response = requests.post(
                login_url,
                json={
                    "type": "m.login.password",
                    "user": "deadcms",
                    "password": "pw1",
                },
            )
            self.assertNotEqual(login_response.status_code, 200)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_scheduled_delete_removes_cms_feedback_logs(self):
        """Seed data + short delay + fast processor → logs gone & account deactivated."""
        mock_cms = MockCmsServer()
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            cms_url = mock_cms.start()
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config={
                    "delete_user_schedule_delay_seconds": 3,
                    "delete_user_processor_interval_seconds": 1,
                    "cms_base_url": cms_url,
                    "cms_service_api_key": "test-key",
                }
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="schedcms",
                password="pw1",
                admin=False,
            )
            _, access_token = await self.login_user("schedcms", "pw1")
            user_id = "@schedcms:my.domain.name"

            mock_cms.seed_feedback_logs(
                user_id,
                [{"id": "sc1", "req": {"user_id": user_id}}],
            )

            delete_url = "http://localhost:8008/_synapse/client/pangea/v1/delete_user"
            schedule_resp = requests.post(
                delete_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(schedule_resp.status_code, 200)

            await asyncio.sleep(6)

            self.assertEqual(mock_cms.get_remaining_logs(user_id), [])
            self.assertEqual(self._count_schedules(config_path, user_id), 0)

            login_url = "http://localhost:8008/_matrix/client/v3/login"
            login_response = requests.post(
                login_url,
                json={
                    "type": "m.login.password",
                    "user": "schedcms",
                    "password": "pw1",
                },
            )
            self.assertNotEqual(login_response.status_code, 200)
        finally:
            mock_cms.stop()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_admin_force_delete_removes_other_users_feedback_logs(self):
        """Admin deletes target → target's feedback logs removed."""
        mock_cms = MockCmsServer()
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            cms_url = mock_cms.start()
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config={
                    "cms_base_url": cms_url,
                    "cms_service_api_key": "test-key",
                }
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admindel",
                password="pw1",
                admin=True,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="victimcms",
                password="pw2",
                admin=False,
            )
            _, admin_token = await self.login_user("admindel", "pw1")
            target_user_id = "@victimcms:my.domain.name"

            mock_cms.seed_feedback_logs(
                target_user_id,
                [
                    {"id": "v1", "req": {"user_id": target_user_id}},
                    {"id": "v2", "req": {"user_id": target_user_id}},
                    {"id": "v3", "req": {"user_id": target_user_id}},
                ],
            )

            delete_url = "http://localhost:8008/_synapse/client/pangea/v1/delete_user"
            requests.post(
                delete_url,
                json={"user_id": target_user_id},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            force = requests.post(
                delete_url,
                json={"action": "force", "user_id": target_user_id},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            self.assertEqual(force.status_code, 200)
            self.assertEqual(force.json()["deleted_cms_feedback_logs"], 3)
            self.assertEqual(mock_cms.get_remaining_logs(target_user_id), [])

            login_url = "http://localhost:8008/_matrix/client/v3/login"
            login_response = requests.post(
                login_url,
                json={
                    "type": "m.login.password",
                    "user": "victimcms",
                    "password": "pw2",
                },
            )
            self.assertNotEqual(login_response.status_code, 200)
        finally:
            mock_cms.stop()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
