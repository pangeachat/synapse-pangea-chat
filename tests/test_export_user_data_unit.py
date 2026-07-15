import unittest
from unittest.mock import AsyncMock, MagicMock

from synapse.api.errors import StoreError

import synapse_pangea_chat.export_user_data.export_user_data as export_module
from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.export_user_data.export_user_data import (
    MAX_SCHEDULE_ATTEMPTS,
    ExportUserData,
    JsonExfiltrationWriter,
    _background_process_args,
    _looping_call_interval_seconds,
    _media_type_to_ext,
)
from tests.mock_cms_server import _parse_multipart_export_body

LOGGER_NAME = "synapse.module.synapse_pangea_chat.export_user_data"


def _make_handler() -> ExportUserData:
    api = MagicMock()
    api._hs.hostname = "my.domain.name"
    clock = MagicMock()
    clock.time_msec.return_value = 1_000_000
    api._hs.get_clock.return_value = clock
    handler = ExportUserData(api, PangeaChatConfig())
    handler._ensure_schedule_table = AsyncMock()  # type: ignore[method-assign]
    handler._upsert_schedule = AsyncMock()  # type: ignore[method-assign]
    handler._export_user_now = AsyncMock()  # type: ignore[method-assign]
    return handler


def _schedule(
    user_id: str = "@ghost:my.domain.name",
    requested_by: str = "@requester:my.domain.name",
    attempts: int = 0,
) -> dict:
    return {
        "user_id": user_id,
        "requested_by": requested_by,
        "requested_by_admin": False,
        "attempts": attempts,
    }


class TestJsonExfiltrationWriter(unittest.TestCase):
    def setUp(self):
        self.writer = JsonExfiltrationWriter()

    def test_write_events(self):
        event = MagicMock()
        event.get_pdu_json.return_value = {"type": "m.room.message", "content": {}}
        self.writer.write_events("!room:example.com", [event])
        result = self.writer.finished()
        self.assertEqual(len(result["rooms"]["!room:example.com"]["events"]), 1)
        self.assertEqual(
            result["rooms"]["!room:example.com"]["events"][0]["type"],
            "m.room.message",
        )

    def test_write_events_multiple_rooms(self):
        event1 = MagicMock()
        event1.get_pdu_json.return_value = {"room": "1"}
        event2 = MagicMock()
        event2.get_pdu_json.return_value = {"room": "2"}
        self.writer.write_events("!a:x", [event1])
        self.writer.write_events("!b:x", [event2])
        result = self.writer.finished()
        self.assertIn("!a:x", result["rooms"])
        self.assertIn("!b:x", result["rooms"])

    def test_write_state(self):
        state_event = MagicMock()
        state_event.get_pdu_json.return_value = {"type": "m.room.name"}
        self.writer.write_state("!r:x", "$ev1", {("m.room.name", ""): state_event})
        result = self.writer.finished()
        self.assertIn("$ev1", result["rooms"]["!r:x"]["state"])

    def test_write_invite(self):
        event = MagicMock()
        event.get_pdu_json.return_value = {"type": "m.room.member"}
        self.writer.write_invite("!r:x", event, {})
        result = self.writer.finished()
        self.assertEqual(len(result["rooms"]["!r:x"]["events"]), 1)
        self.assertIn("invite_state", result["rooms"]["!r:x"])

    def test_write_knock(self):
        event = MagicMock()
        event.get_pdu_json.return_value = {"type": "m.room.member"}
        self.writer.write_knock("!r:x", event, {})
        result = self.writer.finished()
        self.assertEqual(len(result["rooms"]["!r:x"]["events"]), 1)
        self.assertIn("knock_state", result["rooms"]["!r:x"])

    def test_write_profile(self):
        self.writer.write_profile({"displayname": "Alice"})
        result = self.writer.finished()
        self.assertEqual(result["user_data"]["profile"]["displayname"], "Alice")

    def test_write_devices(self):
        self.writer.write_devices([{"device_id": "ABC"}])
        result = self.writer.finished()
        self.assertEqual(len(result["user_data"]["devices"]), 1)

    def test_write_connections(self):
        self.writer.write_connections([{"ip": "1.2.3.4"}])
        result = self.writer.finished()
        self.assertEqual(len(result["user_data"]["connections"]), 1)

    def test_write_account_data(self):
        self.writer.write_account_data("global", {"theme": {"content": "dark"}})
        result = self.writer.finished()
        self.assertIn("global", result["user_data"]["account_data"])

    def test_write_media_id(self):
        self.writer.write_media_id("abc123", {"media_type": "image/png"})
        result = self.writer.finished()
        self.assertEqual(len(result["media_ids"]), 1)
        self.assertEqual(result["media_ids"][0]["media_id"], "abc123")

    def test_finished_returns_complete_structure(self):
        result = self.writer.finished()
        self.assertIn("rooms", result)
        self.assertIn("user_data", result)
        self.assertIn("media_ids", result)
        self.assertIn("profile", result["user_data"])
        self.assertIn("devices", result["user_data"])
        self.assertIn("connections", result["user_data"])
        self.assertIn("account_data", result["user_data"])


class TestMediaTypeToExt(unittest.TestCase):
    def test_known_types(self):
        self.assertEqual(_media_type_to_ext("image/jpeg"), ".jpg")
        self.assertEqual(_media_type_to_ext("image/png"), ".png")
        self.assertEqual(_media_type_to_ext("video/mp4"), ".mp4")
        self.assertEqual(_media_type_to_ext("application/pdf"), ".pdf")

    def test_unknown_type(self):
        self.assertEqual(_media_type_to_ext("application/x-custom"), "")


class TestMultipartCmsUploadContract(unittest.TestCase):
    def test_looping_call_interval_behaves_like_ms_int_and_duration(self):
        interval = _looping_call_interval_seconds(60)

        self.assertIsInstance(interval, int)
        self.assertEqual(interval, 60000)
        self.assertEqual(interval.as_millis(), 60000)
        self.assertEqual(interval.as_secs(), 60)

    def test_background_process_args_include_server_name_when_supported(self):
        homeserver = MagicMock()
        homeserver.hostname = "my.domain.name"

        original = export_module._RUN_AS_BG_SUPPORTS_SERVER_NAME
        export_module._RUN_AS_BG_SUPPORTS_SERVER_NAME = True
        try:
            self.assertEqual(
                _background_process_args(homeserver, "desc", MagicMock()),
                ("desc", "my.domain.name", unittest.mock.ANY),
            )
        finally:
            export_module._RUN_AS_BG_SUPPORTS_SERVER_NAME = original

    def test_background_process_args_omit_server_name_when_not_supported(self):
        homeserver = MagicMock()
        homeserver.hostname = "my.domain.name"

        original = export_module._RUN_AS_BG_SUPPORTS_SERVER_NAME
        export_module._RUN_AS_BG_SUPPORTS_SERVER_NAME = False
        try:
            self.assertEqual(
                _background_process_args(homeserver, "desc", MagicMock()),
                ("desc", unittest.mock.ANY),
            )
        finally:
            export_module._RUN_AS_BG_SUPPORTS_SERVER_NAME = original

    def test_build_multipart_form_body_matches_mock_cms_parser(self):
        resource = ExportUserData.__new__(ExportUserData)

        multipart_body = resource._build_multipart_form_body(
            boundary=b"----PangeaExportBoundary",
            fields=[
                (
                    b"_payload",
                    b'{"user":"matrix-user-1","status":"complete","requestedAt":"2026-03-20T00:00:00Z"}',
                )
            ],
            files=[
                {
                    "field_name": b"file",
                    "filename": b"export_wilsonle_staging.pangea.chat.zip",
                    "content_type": b"application/zip",
                    "body": b"zip-bytes",
                }
            ],
        )

        parsed = _parse_multipart_export_body(multipart_body)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["payload"]["user"], "matrix-user-1")
        self.assertEqual(parsed["payload"]["status"], "complete")
        self.assertEqual(
            parsed["filename"],
            "export_wilsonle_staging.pangea.chat.zip",
        )

    def test_mock_cms_parser_rejects_metadata_only_create(self):
        parsed = _parse_multipart_export_body(
            b"------PangeaExportBoundary\r\n"
            b'Content-Disposition: form-data; name="_payload"\r\n\r\n'
            b'{"user":"matrix-user-1"}\r\n'
            b"------PangeaExportBoundary--\r\n"
        )

        self.assertIsNone(parsed)


class TestProcessScheduledExportsRetries(unittest.IsolatedAsyncioTestCase):
    async def test_success_does_not_reschedule(self):
        handler = _make_handler()
        handler._claim_due_schedules = AsyncMock(return_value=[_schedule()])

        await handler._process_scheduled_exports()

        handler._export_user_now.assert_awaited_once()
        handler._upsert_schedule.assert_not_awaited()

    async def test_transient_failure_reschedules_with_incremented_attempts(self):
        handler = _make_handler()
        handler._claim_due_schedules = AsyncMock(return_value=[_schedule(attempts=1)])
        handler._export_user_now = AsyncMock(side_effect=RuntimeError("boom"))

        await handler._process_scheduled_exports()

        handler._upsert_schedule.assert_awaited_once()
        kwargs = handler._upsert_schedule.await_args.kwargs
        self.assertEqual(kwargs["user_id"], "@ghost:my.domain.name")
        self.assertEqual(kwargs["attempts"], 2)

    async def test_transient_failure_preserves_original_requester(self):
        handler = _make_handler()
        handler._claim_due_schedules = AsyncMock(return_value=[_schedule()])
        handler._export_user_now = AsyncMock(side_effect=RuntimeError("boom"))

        await handler._process_scheduled_exports()

        kwargs = handler._upsert_schedule.await_args.kwargs
        self.assertEqual(kwargs["requested_by"], "@requester:my.domain.name")

    async def test_transient_failure_stops_after_max_attempts(self):
        handler = _make_handler()
        handler._claim_due_schedules = AsyncMock(
            return_value=[_schedule(attempts=MAX_SCHEDULE_ATTEMPTS - 1)]
        )
        handler._export_user_now = AsyncMock(side_effect=RuntimeError("boom"))

        with self.assertLogs(LOGGER_NAME, level="ERROR") as logs:
            await handler._process_scheduled_exports()

        handler._upsert_schedule.assert_not_awaited()
        self.assertTrue(any("giving up" in line for line in logs.output))
        self.assertTrue(any("@ghost:my.domain.name" in line for line in logs.output))

    async def test_user_not_found_fails_terminally_on_first_attempt(self):
        handler = _make_handler()
        handler._claim_due_schedules = AsyncMock(return_value=[_schedule(attempts=0)])
        handler._export_user_now = AsyncMock(
            side_effect=StoreError(404, "No row found (users)")
        )

        with self.assertLogs(LOGGER_NAME, level="ERROR") as logs:
            await handler._process_scheduled_exports()

        handler._upsert_schedule.assert_not_awaited()
        self.assertTrue(any("giving up" in line for line in logs.output))

    async def test_missing_attempts_defaults_to_zero(self):
        handler = _make_handler()
        schedule = _schedule()
        del schedule["attempts"]
        handler._claim_due_schedules = AsyncMock(return_value=[schedule])
        handler._export_user_now = AsyncMock(side_effect=RuntimeError("boom"))

        await handler._process_scheduled_exports()

        kwargs = handler._upsert_schedule.await_args.kwargs
        self.assertEqual(kwargs["attempts"], 1)

    async def test_failure_on_one_schedule_does_not_block_others(self):
        handler = _make_handler()
        failing = _schedule(user_id="@ghost:my.domain.name")
        succeeding = _schedule(user_id="@alive:my.domain.name")
        handler._claim_due_schedules = AsyncMock(return_value=[failing, succeeding])
        handler._export_user_now = AsyncMock(side_effect=[RuntimeError("boom"), None])

        await handler._process_scheduled_exports()

        self.assertEqual(handler._export_user_now.await_count, 2)
