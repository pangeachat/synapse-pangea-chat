import requests
import yaml
from psycopg2 import connect

from .base_e2e import BaseSynapseE2ETest


class TestDeleteUserE2E(BaseSynapseE2ETest):
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

    async def test_delete_user_self(self):
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
            self.assertEqual(response.json()["message"], "Deleted")
            self.assertEqual(response.json()["user_id"], user_id)
            self.assertEqual(response.json()["deleted_threepids"], 1)
            self.assertEqual(response.json()["deleted_external_ids"], 1)
            self.assertEqual(self._count_threepids(config_path, user_id), 0)
            self.assertEqual(self._count_external_ids(config_path, user_id), 0)

            login_url = "http://localhost:8008/_matrix/client/v3/login"
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

    async def test_delete_user_admin_can_target_another_user(self):
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
            self.assertEqual(response.json()["user_id"], target_user_id)
            self.assertEqual(response.json()["deleted_threepids"], 1)
            self.assertEqual(response.json()["deleted_external_ids"], 1)
            self.assertEqual(self._count_threepids(config_path, target_user_id), 0)
            self.assertEqual(self._count_external_ids(config_path, target_user_id), 0)

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
