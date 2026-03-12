import unittest
from unittest.mock import MagicMock

from synapse_pangea_chat.export_user_data.export_user_data import (
    JsonExfiltrationWriter,
    _media_type_to_ext,
)


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
