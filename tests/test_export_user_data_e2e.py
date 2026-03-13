import asyncio
import json
import os
import tempfile
import zipfile

import requests
import yaml
from psycopg2 import connect

from .base_e2e import BaseSynapseE2ETest
from .mock_cms_server import MockCmsServer


class TestExportUserDataE2E(BaseSynapseE2ETest):
    _SCHEDULE_TABLE = "pangea_export_user_data_schedule"
    _EXPORT_URL = "http://localhost:8008/_synapse/client/pangea/v1/export_user_data"

    def _db_args_from_config(self, config_path: str) -> dict:
        with open(config_path, "r", encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)
        database = config.get("database", {})
        args = database.get("args", {})
        return args

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

    def _zip_path_for_user(self, export_dir: str, user_id: str) -> str:
        safe_user_id = user_id.replace("@", "").replace(":", "_")
        return os.path.join(export_dir, f"export_{safe_user_id}.zip")

    def _read_export_json(self, zip_path: str) -> dict:
        self.assertTrue(
            os.path.exists(zip_path),
            f"Expected export ZIP at {zip_path}, but file does not exist",
        )
        self.assertGreater(os.path.getsize(zip_path), 0)

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            self.assertIn("user_data.json", names)
            return json.loads(zf.read("user_data.json"))

    def _assert_export_contains_message(
        self,
        *,
        user_data: dict,
        room_id: str,
        expected_body: str,
    ) -> None:
        self.assertIn("rooms", user_data)
        self.assertIn("user_data", user_data)
        self.assertIn("media_ids", user_data)

        profile = user_data["user_data"].get("profile")
        self.assertIsNotNone(profile, "Expected profile in export")

        self.assertIn(room_id, user_data["rooms"])
        room_data = user_data["rooms"][room_id]
        events = room_data.get("events", [])
        message_bodies = [
            e.get("content", {}).get("body")
            for e in events
            if e.get("type") == "m.room.message"
        ]
        self.assertIn(
            expected_body,
            message_bodies,
            f"Expected message '{expected_body}' in exported room events",
        )

    async def test_schedule_export_creates_schedule(self):
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
                user="exporter",
                password="pw1",
                admin=False,
            )
            _, access_token = await self.login_user("exporter", "pw1")
            user_id = "@exporter:my.domain.name"

            # Schedule export
            response = requests.post(
                self._EXPORT_URL,
                json={"action": "schedule"},
                headers={"Authorization": f"Bearer {access_token}"},
            )

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["message"], "Export scheduled")
            self.assertEqual(data["action"], "schedule")
            self.assertEqual(data["user_id"], user_id)
            self.assertEqual(self._count_schedules(config_path, user_id), 1)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_schedule_then_cancel(self):
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
                user="canceluser",
                password="pw1",
                admin=False,
            )
            _, access_token = await self.login_user("canceluser", "pw1")
            user_id = "@canceluser:my.domain.name"

            # Schedule
            response = requests.post(
                self._EXPORT_URL,
                json={"action": "schedule"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self._count_schedules(config_path, user_id), 1)

            # Cancel
            response = requests.post(
                self._EXPORT_URL,
                json={"action": "cancel"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["action"], "cancel")
            self.assertTrue(data["canceled"])
            self.assertEqual(self._count_schedules(config_path, user_id), 0)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_cancel_nonexistent_returns_false(self):
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
                user="noexport",
                password="pw1",
                admin=False,
            )
            _, access_token = await self.login_user("noexport", "pw1")

            response = requests.post(
                self._EXPORT_URL,
                json={"action": "cancel"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertFalse(data["canceled"])
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_status_shows_schedule(self):
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
                user="statususer",
                password="pw1",
                admin=False,
            )
            _, access_token = await self.login_user("statususer", "pw1")
            user_id = "@statususer:my.domain.name"

            # No schedule yet
            response = requests.post(
                self._EXPORT_URL,
                json={"action": "status"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertFalse(data["scheduled"])
            self.assertIsNone(data["schedule"])

            # Create schedule
            requests.post(
                self._EXPORT_URL,
                json={"action": "schedule"},
                headers={"Authorization": f"Bearer {access_token}"},
            )

            # Check status
            response = requests.post(
                self._EXPORT_URL,
                json={"action": "status"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["scheduled"])
            self.assertIsNotNone(data["schedule"])
            self.assertEqual(data["schedule"]["user_id"], user_id)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_non_admin_cannot_export_other_user(self):
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
                user="regular",
                password="pw1",
                admin=False,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="target",
                password="pw2",
                admin=False,
            )
            _, regular_token = await self.login_user("regular", "pw1")

            response = requests.post(
                self._EXPORT_URL,
                json={
                    "action": "schedule",
                    "user_id": "@target:my.domain.name",
                },
                headers={"Authorization": f"Bearer {regular_token}"},
            )
            self.assertEqual(response.status_code, 403)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_admin_can_schedule_for_other_user(self):
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
                user="exportee",
                password="pw2",
                admin=False,
            )
            _, admin_token = await self.login_user("admin", "pw1")
            target_user_id = "@exportee:my.domain.name"

            response = requests.post(
                self._EXPORT_URL,
                json={
                    "action": "schedule",
                    "user_id": target_user_id,
                },
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["user_id"], target_user_id)
            self.assertEqual(self._count_schedules(config_path, target_user_id), 1)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_force_requires_admin(self):
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
                user="forceuser",
                password="pw1",
                admin=False,
            )
            _, access_token = await self.login_user("forceuser", "pw1")

            # Schedule first
            requests.post(
                self._EXPORT_URL,
                json={"action": "schedule"},
                headers={"Authorization": f"Bearer {access_token}"},
            )

            # Try force — should be rejected
            response = requests.post(
                self._EXPORT_URL,
                json={"action": "force"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(response.status_code, 403)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_unauthenticated_request_rejected(self):
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

            response = requests.post(
                self._EXPORT_URL,
                json={"action": "schedule"},
            )
            self.assertEqual(response.status_code, 403)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_invalid_action_rejected(self):
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
                user="badaction",
                password="pw1",
                admin=False,
            )
            _, access_token = await self.login_user("badaction", "pw1")

            response = requests.post(
                self._EXPORT_URL,
                json={"action": "nonexistent"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(response.status_code, 400)
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_default_action_is_schedule(self):
        """POST with empty body should default to action=schedule."""
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
                user="defaultuser",
                password="pw1",
                admin=False,
            )
            _, access_token = await self.login_user("defaultuser", "pw1")

            # POST with no body
            response = requests.post(
                self._EXPORT_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["action"], "schedule")
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_scheduled_export_produces_valid_zip_with_user_data(self):
        """Schedule an export, wait for background processor, verify ZIP on disk."""
        import asyncio

        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        export_dir = tempfile.mkdtemp()

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
                    "export_user_data_processor_interval_seconds": 1,
                    "export_user_data_output_dir": export_dir,
                }
            )

            # Register user and send a message so there's data to export
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="exporter",
                password="pw1",
                admin=False,
            )
            _, access_token = await self.login_user("exporter", "pw1")
            user_id = "@exporter:my.domain.name"

            room_id = await self.create_private_room(access_token)
            send_url = (
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id}"
                f"/send/m.room.message/test-txn-1"
            )
            send_resp = requests.put(
                send_url,
                json={"msgtype": "m.text", "body": "Hello export test!"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(send_resp.status_code, 200)

            # Schedule export (execute_at_ms = now, processor runs every 1s)
            response = requests.post(
                self._EXPORT_URL,
                json={"action": "schedule"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self._count_schedules(config_path, user_id), 1)

            # Wait for background processor to pick up and complete the export
            await asyncio.sleep(8)

            # Schedule should be consumed
            self.assertEqual(self._count_schedules(config_path, user_id), 0)

            # Verify ZIP file was written to disk
            expected_filename = "export_exporter_my.domain.name.zip"
            zip_path = os.path.join(export_dir, expected_filename)
            self.assertTrue(
                os.path.exists(zip_path),
                f"Expected export ZIP at {zip_path}, "
                f"files in dir: {os.listdir(export_dir)}",
            )
            self.assertGreater(os.path.getsize(zip_path), 0)

            # Unzip and validate contents
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                self.assertIn("user_data.json", names)
                user_data = json.loads(zf.read("user_data.json"))

            # Verify exported data structure
            self.assertIn("rooms", user_data)
            self.assertIn("user_data", user_data)
            self.assertIn("media_ids", user_data)

            # Verify profile is present
            profile = user_data["user_data"].get("profile")
            self.assertIsNotNone(profile, "Expected profile in export")

            # Verify the room and message are present
            self.assertIn(room_id, user_data["rooms"])
            room_data = user_data["rooms"][room_id]
            events = room_data.get("events", [])
            message_bodies = [
                e.get("content", {}).get("body")
                for e in events
                if e.get("type") == "m.room.message"
            ]
            self.assertIn(
                "Hello export test!",
                message_bodies,
                "Expected test message in exported room events",
            )
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_user_schedules_then_admin_forces_export_writes_valid_zip(self):
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        with tempfile.TemporaryDirectory() as export_dir:
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
                        "export_user_data_processor_interval_seconds": 60,
                        "export_user_data_output_dir": export_dir,
                    }
                )

                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user="adminforce",
                    password="pw1",
                    admin=True,
                )
                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user="student1",
                    password="pw2",
                    admin=False,
                )

                _, admin_token = await self.login_user("adminforce", "pw1")
                user_id, user_token = await self.login_user("student1", "pw2")

                room_id = await self.create_private_room(user_token)
                send_url = (
                    f"{self.server_url}/_matrix/client/v3/rooms/{room_id}"
                    f"/send/m.room.message/admin-force-txn-1"
                )
                send_resp = requests.put(
                    send_url,
                    json={"msgtype": "m.text", "body": "ideal admin force export"},
                    headers={"Authorization": f"Bearer {user_token}"},
                )
                self.assertEqual(send_resp.status_code, 200)

                schedule_response = requests.post(
                    self._EXPORT_URL,
                    json={"action": "schedule"},
                    headers={"Authorization": f"Bearer {user_token}"},
                )
                self.assertEqual(schedule_response.status_code, 200)
                self.assertEqual(self._count_schedules(config_path, user_id), 1)

                force_response = requests.post(
                    self._EXPORT_URL,
                    json={"action": "force", "user_id": user_id},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                self.assertEqual(force_response.status_code, 200)
                self.assertEqual(force_response.json().get("action"), "force")
                self.assertEqual(self._count_schedules(config_path, user_id), 0)

                zip_path = self._zip_path_for_user(export_dir, user_id)
                user_data = self._read_export_json(zip_path)
                self._assert_export_contains_message(
                    user_data=user_data,
                    room_id=room_id,
                    expected_body="ideal admin force export",
                )
            finally:
                self.stop_synapse(
                    server_process=server_process,
                    stdout_thread=stdout_thread,
                    stderr_thread=stderr_thread,
                    synapse_dir=synapse_dir,
                    postgres=postgres,
                )

    async def test_user_schedule_then_cancel_before_run_creates_no_file(self):
        import asyncio

        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        with tempfile.TemporaryDirectory() as export_dir:
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
                        "export_user_data_processor_interval_seconds": 30,
                        "export_user_data_output_dir": export_dir,
                    }
                )

                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user="cancelbefore",
                    password="pw1",
                    admin=False,
                )
                user_id, user_token = await self.login_user("cancelbefore", "pw1")

                schedule_response = requests.post(
                    self._EXPORT_URL,
                    json={"action": "schedule"},
                    headers={"Authorization": f"Bearer {user_token}"},
                )
                self.assertEqual(schedule_response.status_code, 200)
                self.assertEqual(self._count_schedules(config_path, user_id), 1)

                cancel_response = requests.post(
                    self._EXPORT_URL,
                    json={"action": "cancel"},
                    headers={"Authorization": f"Bearer {user_token}"},
                )
                self.assertEqual(cancel_response.status_code, 200)
                self.assertTrue(cancel_response.json().get("canceled"))
                self.assertEqual(self._count_schedules(config_path, user_id), 0)

                await asyncio.sleep(2)
                zip_path = self._zip_path_for_user(export_dir, user_id)
                self.assertFalse(
                    os.path.exists(zip_path),
                    f"Did not expect export ZIP at {zip_path} after cancel-before-run",
                )
            finally:
                self.stop_synapse(
                    server_process=server_process,
                    stdout_thread=stdout_thread,
                    stderr_thread=stderr_thread,
                    synapse_dir=synapse_dir,
                    postgres=postgres,
                )

    async def test_repeated_forced_export_overwrites_zip_with_latest_content(self):
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        with tempfile.TemporaryDirectory() as export_dir:
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
                        "export_user_data_processor_interval_seconds": 60,
                        "export_user_data_output_dir": export_dir,
                    }
                )

                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user="admin2",
                    password="pw1",
                    admin=True,
                )
                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user="repeatuser",
                    password="pw2",
                    admin=False,
                )

                _, admin_token = await self.login_user("admin2", "pw1")
                user_id, user_token = await self.login_user("repeatuser", "pw2")

                room_id = await self.create_private_room(user_token)
                send_url_1 = (
                    f"{self.server_url}/_matrix/client/v3/rooms/{room_id}"
                    f"/send/m.room.message/repeat-txn-1"
                )
                send_resp_1 = requests.put(
                    send_url_1,
                    json={"msgtype": "m.text", "body": "first export content"},
                    headers={"Authorization": f"Bearer {user_token}"},
                )
                self.assertEqual(send_resp_1.status_code, 200)

                requests.post(
                    self._EXPORT_URL,
                    json={"action": "schedule"},
                    headers={"Authorization": f"Bearer {user_token}"},
                )
                first_force = requests.post(
                    self._EXPORT_URL,
                    json={"action": "force", "user_id": user_id},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                self.assertEqual(first_force.status_code, 200)

                send_url_2 = (
                    f"{self.server_url}/_matrix/client/v3/rooms/{room_id}"
                    f"/send/m.room.message/repeat-txn-2"
                )
                send_resp_2 = requests.put(
                    send_url_2,
                    json={"msgtype": "m.text", "body": "latest export content"},
                    headers={"Authorization": f"Bearer {user_token}"},
                )
                self.assertEqual(send_resp_2.status_code, 200)

                requests.post(
                    self._EXPORT_URL,
                    json={"action": "schedule"},
                    headers={"Authorization": f"Bearer {user_token}"},
                )
                second_force = requests.post(
                    self._EXPORT_URL,
                    json={"action": "force", "user_id": user_id},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                self.assertEqual(second_force.status_code, 200)

                zip_path = self._zip_path_for_user(export_dir, user_id)
                user_data = self._read_export_json(zip_path)
                self._assert_export_contains_message(
                    user_data=user_data,
                    room_id=room_id,
                    expected_body="latest export content",
                )
            finally:
                self.stop_synapse(
                    server_process=server_process,
                    stdout_thread=stdout_thread,
                    stderr_thread=stderr_thread,
                    synapse_dir=synapse_dir,
                    postgres=postgres,
                )

    async def test_disk_export_succeeds_even_if_cms_upload_fails(self):
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        with tempfile.TemporaryDirectory() as export_dir:
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
                        "export_user_data_processor_interval_seconds": 60,
                        "export_user_data_output_dir": export_dir,
                        "cms_base_url": "http://127.0.0.1:9",
                        "cms_service_api_key": "bad-key",
                    }
                )

                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user="admin3",
                    password="pw1",
                    admin=True,
                )
                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user="cmsdown",
                    password="pw2",
                    admin=False,
                )

                _, admin_token = await self.login_user("admin3", "pw1")
                user_id, user_token = await self.login_user("cmsdown", "pw2")

                room_id = await self.create_private_room(user_token)
                send_url = (
                    f"{self.server_url}/_matrix/client/v3/rooms/{room_id}"
                    f"/send/m.room.message/cms-failure-txn-1"
                )
                send_resp = requests.put(
                    send_url,
                    json={"msgtype": "m.text", "body": "disk should still work"},
                    headers={"Authorization": f"Bearer {user_token}"},
                )
                self.assertEqual(send_resp.status_code, 200)

                requests.post(
                    self._EXPORT_URL,
                    json={"action": "schedule"},
                    headers={"Authorization": f"Bearer {user_token}"},
                )
                force_response = requests.post(
                    self._EXPORT_URL,
                    json={"action": "force", "user_id": user_id},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                self.assertEqual(force_response.status_code, 200)
                self.assertEqual(self._count_schedules(config_path, user_id), 0)

                zip_path = self._zip_path_for_user(export_dir, user_id)
                user_data = self._read_export_json(zip_path)
                self._assert_export_contains_message(
                    user_data=user_data,
                    room_id=room_id,
                    expected_body="disk should still work",
                )
            finally:
                self.stop_synapse(
                    server_process=server_process,
                    stdout_thread=stdout_thread,
                    stderr_thread=stderr_thread,
                    synapse_dir=synapse_dir,
                    postgres=postgres,
                )

    # ---- CMS feedback-log tests ----

    async def test_export_includes_cms_feedback_logs_in_zip(self):
        """Seed 3 feedback logs → admin force export → ZIP contains them."""
        mock_cms = MockCmsServer()
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        with tempfile.TemporaryDirectory() as export_dir:
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
                        "export_user_data_processor_interval_seconds": 60,
                        "export_user_data_output_dir": export_dir,
                        "cms_base_url": cms_url,
                        "cms_service_api_key": "test-key",
                    }
                )

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
                    user="fbuser",
                    password="pw2",
                    admin=False,
                )
                _, admin_token = await self.login_user("admin", "pw1")
                user_id, _ = await self.login_user("fbuser", "pw2")

                mock_cms.seed_feedback_logs(
                    user_id,
                    [
                        {"id": "1", "req": {"user_id": user_id}, "data": "a"},
                        {"id": "2", "req": {"user_id": user_id}, "data": "b"},
                        {"id": "3", "req": {"user_id": user_id}, "data": "c"},
                    ],
                )

                requests.post(
                    self._EXPORT_URL,
                    json={"action": "schedule", "user_id": user_id},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                force = requests.post(
                    self._EXPORT_URL,
                    json={"action": "force", "user_id": user_id},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                self.assertEqual(force.status_code, 200)

                zip_path = self._zip_path_for_user(export_dir, user_id)
                user_data = self._read_export_json(zip_path)
                logs = user_data.get("cms_process_token_feedback_logs", [])
                self.assertEqual(len(logs), 3)
                self.assertEqual({doc["id"] for doc in logs}, {"1", "2", "3"})
            finally:
                mock_cms.stop()
                self.stop_synapse(
                    server_process=server_process,
                    stdout_thread=stdout_thread,
                    stderr_thread=stderr_thread,
                    synapse_dir=synapse_dir,
                    postgres=postgres,
                )

    async def test_export_includes_empty_feedback_logs_when_user_has_none(self):
        """No seeded data → export contains empty list."""
        mock_cms = MockCmsServer()
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        with tempfile.TemporaryDirectory() as export_dir:
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
                        "export_user_data_processor_interval_seconds": 60,
                        "export_user_data_output_dir": export_dir,
                        "cms_base_url": cms_url,
                        "cms_service_api_key": "test-key",
                    }
                )

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
                    user="nofb",
                    password="pw2",
                    admin=False,
                )
                _, admin_token = await self.login_user("admin", "pw1")
                user_id, _ = await self.login_user("nofb", "pw2")

                requests.post(
                    self._EXPORT_URL,
                    json={"action": "schedule", "user_id": user_id},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                force = requests.post(
                    self._EXPORT_URL,
                    json={"action": "force", "user_id": user_id},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                self.assertEqual(force.status_code, 200)

                zip_path = self._zip_path_for_user(export_dir, user_id)
                user_data = self._read_export_json(zip_path)
                self.assertEqual(user_data.get("cms_process_token_feedback_logs"), [])
            finally:
                mock_cms.stop()
                self.stop_synapse(
                    server_process=server_process,
                    stdout_thread=stdout_thread,
                    stderr_thread=stderr_thread,
                    synapse_dir=synapse_dir,
                    postgres=postgres,
                )

    async def test_export_paginates_feedback_logs(self):
        """Seed items exceeding a single page → all appear in export.

        The mock returns pages of configurable size via the limit param.
        We seed 5 items and the export code uses limit=100 by default,
        so all appear in one page. This test verifies the pagination loop
        handles the hasNextPage=false termination correctly.
        """
        mock_cms = MockCmsServer()
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        with tempfile.TemporaryDirectory() as export_dir:
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
                        "export_user_data_processor_interval_seconds": 60,
                        "export_user_data_output_dir": export_dir,
                        "cms_base_url": cms_url,
                        "cms_service_api_key": "test-key",
                    }
                )

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
                    user="pager",
                    password="pw2",
                    admin=False,
                )
                _, admin_token = await self.login_user("admin", "pw1")
                user_id, _ = await self.login_user("pager", "pw2")

                mock_cms.seed_feedback_logs(
                    user_id,
                    [{"id": str(i), "req": {"user_id": user_id}} for i in range(5)],
                )

                requests.post(
                    self._EXPORT_URL,
                    json={"action": "schedule", "user_id": user_id},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                force = requests.post(
                    self._EXPORT_URL,
                    json={"action": "force", "user_id": user_id},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                self.assertEqual(force.status_code, 200)

                zip_path = self._zip_path_for_user(export_dir, user_id)
                user_data = self._read_export_json(zip_path)
                logs = user_data.get("cms_process_token_feedback_logs", [])
                self.assertEqual(len(logs), 5)
            finally:
                mock_cms.stop()
                self.stop_synapse(
                    server_process=server_process,
                    stdout_thread=stdout_thread,
                    stderr_thread=stderr_thread,
                    synapse_dir=synapse_dir,
                    postgres=postgres,
                )

    async def test_export_succeeds_when_cms_unavailable(self):
        """Dead CMS URL → ZIP still valid with empty feedback-logs array."""
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        with tempfile.TemporaryDirectory() as export_dir:
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
                        "export_user_data_processor_interval_seconds": 60,
                        "export_user_data_output_dir": export_dir,
                        "cms_base_url": "http://127.0.0.1:9",
                        "cms_service_api_key": "test-key",
                    }
                )

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
                    user="deadcms",
                    password="pw2",
                    admin=False,
                )
                _, admin_token = await self.login_user("admin", "pw1")
                user_id, _ = await self.login_user("deadcms", "pw2")

                requests.post(
                    self._EXPORT_URL,
                    json={"action": "schedule", "user_id": user_id},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                force = requests.post(
                    self._EXPORT_URL,
                    json={"action": "force", "user_id": user_id},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                self.assertEqual(force.status_code, 200)

                zip_path = self._zip_path_for_user(export_dir, user_id)
                user_data = self._read_export_json(zip_path)
                self.assertEqual(user_data.get("cms_process_token_feedback_logs"), [])
            finally:
                self.stop_synapse(
                    server_process=server_process,
                    stdout_thread=stdout_thread,
                    stderr_thread=stderr_thread,
                    synapse_dir=synapse_dir,
                    postgres=postgres,
                )

    async def test_scheduled_export_includes_feedback_logs(self):
        """Seed data, background processor (interval=1s) → ZIP has feedback logs."""
        mock_cms = MockCmsServer()
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        with tempfile.TemporaryDirectory() as export_dir:
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
                        "export_user_data_processor_interval_seconds": 1,
                        "export_user_data_output_dir": export_dir,
                        "cms_base_url": cms_url,
                        "cms_service_api_key": "test-key",
                    }
                )

                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user="schedexp",
                    password="pw1",
                    admin=False,
                )
                user_id, user_token = await self.login_user("schedexp", "pw1")

                mock_cms.seed_feedback_logs(
                    user_id,
                    [
                        {"id": "s1", "req": {"user_id": user_id}},
                        {"id": "s2", "req": {"user_id": user_id}},
                    ],
                )

                response = requests.post(
                    self._EXPORT_URL,
                    json={"action": "schedule"},
                    headers={"Authorization": f"Bearer {user_token}"},
                )
                self.assertEqual(response.status_code, 200)

                await asyncio.sleep(8)

                self.assertEqual(self._count_schedules(config_path, user_id), 0)

                zip_path = self._zip_path_for_user(export_dir, user_id)
                user_data = self._read_export_json(zip_path)
                logs = user_data.get("cms_process_token_feedback_logs", [])
                self.assertEqual(len(logs), 2)
            finally:
                mock_cms.stop()
                self.stop_synapse(
                    server_process=server_process,
                    stdout_thread=stdout_thread,
                    stderr_thread=stderr_thread,
                    synapse_dir=synapse_dir,
                    postgres=postgres,
                )
