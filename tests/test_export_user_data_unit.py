import unittest
from unittest.mock import MagicMock

import synapse_pangea_chat.export_user_data.export_user_data as export_module
from synapse_pangea_chat.export_user_data.export_user_data import (
    ExportUserData,
    JsonExfiltrationWriter,
    _background_process_args,
    _looping_call_interval_seconds,
    _media_type_to_ext,
)
from tests.mock_cms_server import _parse_multipart_export_body


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
