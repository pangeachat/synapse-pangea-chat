import json
import os
import shutil
import tempfile
import unittest
import zipfile

import requests
from psycopg2 import connect

from .base_e2e import SERVER_NAME, BaseSynapseE2ETest
from .mock_cms_server import MockCmsServer

# Long enough that the background processor never runs while a class's tests
# do, so a schedule stays where a test put it until the test acts on it.
_PROCESSOR_IDLE_SECONDS = 3600

_DEAD_CMS_URL = "http://127.0.0.1:9"


def _class_export_dir(cls: type[unittest.TestCase]) -> str:
    """A directory for the class's homeserver to write export ZIPs into.

    Every test of the class shares it, so a test reads only the ZIP named for
    its own user. Removed after the class's server has stopped.
    """
    export_dir = tempfile.mkdtemp()
    cls.addClassCleanup(shutil.rmtree, export_dir, ignore_errors=True)
    return export_dir


class _ExportUserDataHelpers:
    _SCHEDULE_TABLE = "pangea_export_user_data_schedule"

    def _export_url(self) -> str:
        return f"{self.server_url}/_synapse/client/pangea/v1/export_user_data"

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


class TestExportUserDataE2E(_ExportUserDataHelpers, BaseSynapseE2ETest):
    """The endpoint's contract, with the processor idle and the CMS unreachable.

    The processor never runs here, so a scheduled export is consumed only by
    the test's own force or cancel.
    """

    export_dir: str

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.export_dir = _class_export_dir(cls)

    def _module_config(self) -> dict:
        return {
            "export_user_data_processor_interval_seconds": _PROCESSOR_IDLE_SECONDS,
            "export_user_data_output_dir": self.export_dir,
            "cms_base_url": _DEAD_CMS_URL,
            "cms_service_api_key": "test-key",
        }

    async def test_schedule_export_creates_schedule(self):
        await self.homeserver(module_config=self._module_config())

        user_id, access_token = await self.new_user("exporter", password="pw1")

        # Schedule export
        response = requests.post(
            self._export_url(),
            json={"action": "schedule"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["message"], "Export scheduled")
        self.assertEqual(data["action"], "schedule")
        self.assertEqual(data["user_id"], user_id)
        self.assertEqual(self._count_schedules(user_id), 1)

    async def test_schedule_then_cancel(self):
        await self.homeserver(module_config=self._module_config())

        user_id, access_token = await self.new_user("canceluser", password="pw1")

        # Schedule
        response = requests.post(
            self._export_url(),
            json={"action": "schedule"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._count_schedules(user_id), 1)

        # Cancel
        response = requests.post(
            self._export_url(),
            json={"action": "cancel"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["action"], "cancel")
        self.assertTrue(data["canceled"])
        self.assertEqual(self._count_schedules(user_id), 0)

    async def test_cancel_nonexistent_returns_false(self):
        await self.homeserver(module_config=self._module_config())

        _, access_token = await self.new_user("noexport", password="pw1")

        response = requests.post(
            self._export_url(),
            json={"action": "cancel"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["canceled"])

    async def test_status_shows_schedule(self):
        await self.homeserver(module_config=self._module_config())

        user_id, access_token = await self.new_user("statususer", password="pw1")

        # No schedule yet
        response = requests.post(
            self._export_url(),
            json={"action": "status"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["scheduled"])
        self.assertIsNone(data["schedule"])

        # Create schedule
        requests.post(
            self._export_url(),
            json={"action": "schedule"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        # Check status
        response = requests.post(
            self._export_url(),
            json={"action": "status"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["scheduled"])
        self.assertIsNotNone(data["schedule"])
        self.assertEqual(data["schedule"]["user_id"], user_id)

    async def test_non_admin_cannot_export_other_user(self):
        await self.homeserver(module_config=self._module_config())

        _, regular_token = await self.new_user("regular", password="pw1")
        target_user_id, _ = await self.new_user("target", password="pw2")

        response = requests.post(
            self._export_url(),
            json={
                "action": "schedule",
                "user_id": target_user_id,
            },
            headers={"Authorization": f"Bearer {regular_token}"},
        )
        self.assertEqual(response.status_code, 403)

    async def test_admin_can_schedule_for_other_user(self):
        await self.homeserver(module_config=self._module_config())

        _, admin_token = await self.new_user("admin", admin=True, password="pw1")
        target_user_id, _ = await self.new_user("exportee", password="pw2")

        response = requests.post(
            self._export_url(),
            json={
                "action": "schedule",
                "user_id": target_user_id,
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["user_id"], target_user_id)
        self.assertEqual(self._count_schedules(target_user_id), 1)

    async def test_force_requires_admin(self):
        await self.homeserver(module_config=self._module_config())

        _, access_token = await self.new_user("forceuser", password="pw1")

        # Schedule first
        requests.post(
            self._export_url(),
            json={"action": "schedule"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        # Try force — should be rejected
        response = requests.post(
            self._export_url(),
            json={"action": "force"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 403)

    async def test_unauthenticated_request_rejected(self):
        await self.homeserver(module_config=self._module_config())

        response = requests.post(
            self._export_url(),
            json={"action": "schedule"},
        )
        self.assertEqual(response.status_code, 403)

    async def test_invalid_action_rejected(self):
        await self.homeserver(module_config=self._module_config())

        _, access_token = await self.new_user("badaction", password="pw1")

        response = requests.post(
            self._export_url(),
            json={"action": "nonexistent"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 400)

    async def test_default_action_is_schedule(self):
        """POST with empty body should default to action=schedule."""
        await self.homeserver(module_config=self._module_config())

        _, access_token = await self.new_user("defaultuser", password="pw1")

        # POST with no body
        response = requests.post(
            self._export_url(),
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["action"], "schedule")

    async def test_user_schedules_then_admin_forces_export_writes_valid_zip(self):
        await self.homeserver(module_config=self._module_config())

        _, admin_token = await self.new_user("adminforce", admin=True, password="pw1")
        user_id, user_token = await self.new_user("student1", password="pw2")

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
            self._export_url(),
            json={"action": "schedule"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        self.assertEqual(schedule_response.status_code, 200)
        self.assertEqual(self._count_schedules(user_id), 1)

        force_response = requests.post(
            self._export_url(),
            json={"action": "force", "user_id": user_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(force_response.status_code, 200)
        self.assertEqual(force_response.json().get("action"), "force")
        self.assertEqual(self._count_schedules(user_id), 0)

        zip_path = self._zip_path_for_user(self.export_dir, user_id)
        user_data = self._read_export_json(zip_path)
        self._assert_export_contains_message(
            user_data=user_data,
            room_id=room_id,
            expected_body="ideal admin force export",
        )

    async def test_user_schedule_then_cancel_before_run_creates_no_file(self):
        import asyncio

        await self.homeserver(module_config=self._module_config())

        user_id, user_token = await self.new_user("cancelbefore", password="pw1")

        schedule_response = requests.post(
            self._export_url(),
            json={"action": "schedule"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        self.assertEqual(schedule_response.status_code, 200)
        self.assertEqual(self._count_schedules(user_id), 1)

        cancel_response = requests.post(
            self._export_url(),
            json={"action": "cancel"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        self.assertEqual(cancel_response.status_code, 200)
        self.assertTrue(cancel_response.json().get("canceled"))
        self.assertEqual(self._count_schedules(user_id), 0)

        await asyncio.sleep(2)
        zip_path = self._zip_path_for_user(self.export_dir, user_id)
        self.assertFalse(
            os.path.exists(zip_path),
            f"Did not expect export ZIP at {zip_path} after cancel-before-run",
        )

    async def test_repeated_forced_export_overwrites_zip_with_latest_content(self):
        await self.homeserver(module_config=self._module_config())

        _, admin_token = await self.new_user("admin2", admin=True, password="pw1")
        user_id, user_token = await self.new_user("repeatuser", password="pw2")

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
            self._export_url(),
            json={"action": "schedule"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        first_force = requests.post(
            self._export_url(),
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
            self._export_url(),
            json={"action": "schedule"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        second_force = requests.post(
            self._export_url(),
            json={"action": "force", "user_id": user_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(second_force.status_code, 200)

        zip_path = self._zip_path_for_user(self.export_dir, user_id)
        user_data = self._read_export_json(zip_path)
        self._assert_export_contains_message(
            user_data=user_data,
            room_id=room_id,
            expected_body="latest export content",
        )

    async def test_disk_export_succeeds_even_if_cms_upload_fails(self):
        await self.homeserver(module_config=self._module_config())

        _, admin_token = await self.new_user("admin3", admin=True, password="pw1")
        user_id, user_token = await self.new_user("cmsdown", password="pw2")

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
            self._export_url(),
            json={"action": "schedule"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        force_response = requests.post(
            self._export_url(),
            json={"action": "force", "user_id": user_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(force_response.status_code, 200)
        self.assertEqual(self._count_schedules(user_id), 0)

        zip_path = self._zip_path_for_user(self.export_dir, user_id)
        user_data = self._read_export_json(zip_path)
        self._assert_export_contains_message(
            user_data=user_data,
            room_id=room_id,
            expected_body="disk should still work",
        )

    async def test_export_succeeds_when_cms_unavailable(self):
        """Dead CMS URL → ZIP still valid."""
        await self.homeserver(module_config=self._module_config())

        _, admin_token = await self.new_user("admin", admin=True, password="pw1")
        user_id, _ = await self.new_user("deadcms", password="pw2")

        requests.post(
            self._export_url(),
            json={"action": "schedule", "user_id": user_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        force = requests.post(
            self._export_url(),
            json={"action": "force", "user_id": user_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(force.status_code, 200)

        zip_path = self._zip_path_for_user(self.export_dir, user_id)
        user_data = self._read_export_json(zip_path)
        self.assertIsInstance(user_data, dict)


class TestExportUserDataMediaRepoDisabled(_ExportUserDataHelpers, BaseSynapseE2ETest):
    async def test_schedule_export_with_media_repo_disabled_still_starts(self):
        await self.homeserver(synapse_config_overrides={"enable_media_repo": False})

        user_id, access_token = await self.new_user("workerboot", password="pw1")

        response = requests.post(
            self._export_url(),
            json={"action": "schedule"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["message"], "Export scheduled")
        self.assertEqual(data["action"], "schedule")
        self.assertEqual(data["user_id"], user_id)
        self.assertEqual(self._count_schedules(user_id), 1)


class TestExportUserDataProcessor(_ExportUserDataHelpers, BaseSynapseE2ETest):
    """The background processor running every second.

    It exports every due schedule on the server, so this class holds only the
    test that waits for it.
    """

    export_dir: str

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.export_dir = _class_export_dir(cls)

    def _module_config(self) -> dict:
        return {
            "export_user_data_processor_interval_seconds": 1,
            "export_user_data_output_dir": self.export_dir,
        }

    async def test_scheduled_export_produces_valid_zip_with_user_data(self):
        """Schedule an export, wait for background processor, verify ZIP on disk."""
        import asyncio

        await self.homeserver(module_config=self._module_config())

        # Register user and send a message so there's data to export
        user_id, access_token = await self.new_user("exporter", password="pw1")

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
            self._export_url(),
            json={"action": "schedule"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._count_schedules(user_id), 1)

        # Wait for background processor to pick up and complete the export
        await asyncio.sleep(8)

        # Schedule should be consumed
        self.assertEqual(self._count_schedules(user_id), 0)

        # Verify ZIP file was written to disk
        expected_filename = f"export_{self.localpart('exporter')}_{SERVER_NAME}.zip"
        zip_path = os.path.join(self.export_dir, expected_filename)
        self.assertTrue(
            os.path.exists(zip_path),
            f"Expected export ZIP at {zip_path}, "
            f"files in dir: {os.listdir(self.export_dir)}",
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


class TestExportUserDataCmsRecord(_ExportUserDataHelpers, BaseSynapseE2ETest):
    """Exports uploaded to a mock CMS that lives as long as the class's server."""

    export_dir: str
    mock_cms: MockCmsServer
    cms_url: str

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.export_dir = _class_export_dir(cls)
        cls.mock_cms = MockCmsServer()
        cls.cms_url = cls.mock_cms.start()
        cls.addClassCleanup(cls.mock_cms.stop)

    def _module_config(self) -> dict:
        return {
            "export_user_data_processor_interval_seconds": _PROCESSOR_IDLE_SECONDS,
            "export_user_data_output_dir": self.export_dir,
            "cms_base_url": self.cms_url,
            "cms_service_api_key": "test-key",
        }

    async def test_force_export_creates_complete_cms_record_for_related_matrix_user(
        self,
    ):
        """Regression: force export must create a complete CMS user-exports row.

        This covers the staging bug where the export ZIP was produced locally,
        but the CMS record was never created because the module used the wrong
        auth collection and sent the Matrix ID instead of the related
        matrix-users UUID.
        """
        await self.homeserver(module_config=self._module_config())

        _, admin_token = await self.new_user("admin", admin=True, password="pw1")
        user_id, _ = await self.new_user("cmsrecord", password="pw2")
        cms_matrix_user = self.mock_cms.seed_matrix_user(user_id)

        requests.post(
            self._export_url(),
            json={"action": "schedule", "user_id": user_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        force = requests.post(
            self._export_url(),
            json={"action": "force", "user_id": user_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(force.status_code, 200)
        self.assertEqual(self._count_schedules(user_id), 0)

        zip_path = self._zip_path_for_user(self.export_dir, user_id)
        self._read_export_json(zip_path)

        exports = self.mock_cms.get_exports_for_matrix_user_id(cms_matrix_user["id"])
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0].get("user"), cms_matrix_user["id"])
        self.assertEqual(exports[0].get("status"), "complete")
        self.assertTrue(exports[0].get("filename", "").endswith(".zip"))
        self.assertEqual(exports[0].get("mimeType"), "application/zip")
