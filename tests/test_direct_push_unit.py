from __future__ import annotations

import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from synapse_pangea_chat import PangeaChat
from synapse_pangea_chat.direct_push.direct_push import DirectPush


def _make_handler(
    send_push_sygnal_url: str | None = "https://sygnal.example.test",
) -> DirectPush:
    api = MagicMock()
    api._hs.get_auth.return_value = MagicMock()
    api._hs.get_datastores.return_value = MagicMock()
    config = MagicMock()
    config.send_push_sygnal_url = send_push_sygnal_url
    return DirectPush(api, config)


def _iter(items):
    return iter(items)


class TestDirectPushConfig(unittest.TestCase):
    def test_parse_config_includes_send_push_overrides(self):
        config = PangeaChat.parse_config(
            {
                "cms_base_url": "http://cms.example.test",
                "cms_service_api_key": "test-api-key",
                "send_push_requests_per_burst": 25,
                "send_push_burst_duration_seconds": 7,
                "send_push_sygnal_url": "https://sygnal.example.test/_matrix/push/v1/notify",
            }
        )

        self.assertEqual(config.send_push_requests_per_burst, 25)
        self.assertEqual(config.send_push_burst_duration_seconds, 7)
        self.assertEqual(
            config.send_push_sygnal_url,
            "https://sygnal.example.test/_matrix/push/v1/notify",
        )

    def test_parse_config_rejects_invalid_send_push_values(self):
        with self.assertRaisesRegex(ValueError, "send_push_requests_per_burst"):
            PangeaChat.parse_config(
                {
                    "cms_base_url": "http://cms.example.test",
                    "cms_service_api_key": "test-api-key",
                    "send_push_requests_per_burst": 0,
                }
            )

        with self.assertRaisesRegex(ValueError, "send_push_burst_duration_seconds"):
            PangeaChat.parse_config(
                {
                    "cms_base_url": "http://cms.example.test",
                    "cms_service_api_key": "test-api-key",
                    "send_push_burst_duration_seconds": 0,
                }
            )

        with self.assertRaisesRegex(ValueError, 'Config "send_push_sygnal_url"'):
            PangeaChat.parse_config(
                {
                    "cms_base_url": "http://cms.example.test",
                    "cms_service_api_key": "test-api-key",
                    "send_push_sygnal_url": "",
                }
            )


class TestDirectPushHelpers(unittest.IsolatedAsyncioTestCase):
    async def test_extract_body_json_rejects_non_object_json(self):
        handler = _make_handler()
        request = SimpleNamespace(content=BytesIO(b"[]"))

        self.assertIsNone(await handler._extract_body_json(request))

    async def test_get_pushers_filters_disabled_and_device_id(self):
        handler = _make_handler()
        pushers = [
            SimpleNamespace(
                enabled=True,
                device_id="device-a",
                app_id="app",
                pushkey="push-a",
                pushkey_ts=1,
                data={"brand": "ios"},
            ),
            SimpleNamespace(
                enabled=False,
                device_id="device-b",
                app_id="app",
                pushkey="push-b",
                pushkey_ts=2,
                data={"brand": "android"},
            ),
        ]
        handler._datastores.main.get_pushers_by_user_id = AsyncMock(
            return_value=_iter(pushers)
        )

        result = await handler._get_pushers("@alice:my.domain.name", "device-a")

        self.assertEqual(
            result,
            [
                {
                    "device_id": "device-a",
                    "app_id": "app",
                    "pushkey": "push-a",
                    "pushkey_ts": 1,
                    "data": {"brand": "ios"},
                }
            ],
        )

    async def test_get_pushers_falls_back_to_ts_when_pushkey_ts_missing(self):
        handler = _make_handler()
        pushers = [
            SimpleNamespace(
                enabled=True,
                device_id="device-a",
                app_id="app",
                pushkey="push-a",
                ts=99,
                data={"brand": "ios"},
            )
        ]
        handler._datastores.main.get_pushers_by_user_id = AsyncMock(
            return_value=_iter(pushers)
        )

        result = await handler._get_pushers("@alice:my.domain.name", None)

        self.assertEqual(
            result,
            [
                {
                    "device_id": "device-a",
                    "app_id": "app",
                    "pushkey": "push-a",
                    "pushkey_ts": 99,
                    "data": {"brand": "ios"},
                }
            ],
        )

    async def test_send_push_tracks_success_and_failures_per_device(self):
        handler = _make_handler()
        handler._get_pushers = AsyncMock(
            return_value=[
                {
                    "device_id": "device-a",
                    "app_id": "app-a",
                    "pushkey": "push-a",
                    "pushkey_ts": 1,
                    "data": {},
                },
                {
                    "device_id": "device-b",
                    "app_id": "app-b",
                    "pushkey": "push-b",
                    "pushkey_ts": 2,
                    "data": {},
                },
            ]
        )
        handler._post_to_sygnal = AsyncMock(side_effect=[True, False])

        result = await handler._send_push(
            "@alice:my.domain.name",
            None,
            {"room_id": "!room:test", "body": "hello"},
        )

        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(result["devices"]["device-a"]["sent"])
        self.assertEqual(result["devices"]["device-a"]["pushkey"], "push-a")
        self.assertEqual(
            result["devices"]["device-b"]["error"], "Sygnal returned error"
        )
        self.assertEqual(result["errors"], ["device-b: Sygnal returned error"])

    def test_build_payload_includes_message_and_device_metadata(self):
        handler = _make_handler()

        payload = handler._build_payload(
            "event-1",
            {
                "room_id": "!room:test",
                "body": "hello",
                "content": {"format": "org.matrix.custom.html"},
                "type": "m.room.message",
                "prio": "high",
            },
            {
                "app_id": "app-a",
                "pushkey": "push-a",
                "pushkey_ts": 123,
                "data": {"default_payload": {"aps": {}}},
            },
        )

        self.assertEqual(payload["notification"]["event_id"], "event-1")
        self.assertEqual(payload["notification"]["room_id"], "!room:test")
        self.assertEqual(payload["notification"]["content"]["body"], "hello")
        self.assertEqual(
            payload["notification"]["content"]["format"],
            "org.matrix.custom.html",
        )
        self.assertEqual(payload["notification"]["devices"][0]["pushkey"], "push-a")

    def test_build_payload_sets_room_id_null_when_omitted(self):
        handler = _make_handler()

        payload = handler._build_payload(
            "event-1",
            {
                "body": "hello",
            },
            {
                "app_id": "app-a",
                "pushkey": "push-a",
                "pushkey_ts": 123,
                "data": {},
            },
        )

        self.assertIn("room_id", payload["notification"])
        self.assertIsNone(payload["notification"]["room_id"])
        self.assertEqual(payload["notification"]["content"]["body"], "hello")

    def test_build_payload_allows_missing_pushkey_ts(self):
        handler = _make_handler()

        payload = handler._build_payload(
            "event-1",
            {
                "room_id": "!room:test",
                "body": "hello",
            },
            {
                "app_id": "app-a",
                "pushkey": "push-a",
                "data": {},
            },
        )

        self.assertIsNone(payload["notification"]["devices"][0]["pushkey_ts"])

    async def test_post_to_sygnal_uses_configured_url(self):
        handler = _make_handler("https://sygnal.custom.test/_matrix/push/v1/notify")
        fake_response = SimpleNamespace(code=200)
        fake_agent = MagicMock()
        fake_agent.request = AsyncMock(return_value=fake_response)

        with (
            unittest.mock.patch(
                "synapse_pangea_chat.direct_push.direct_push.Agent",
                return_value=fake_agent,
            ),
            unittest.mock.patch(
                "synapse_pangea_chat.direct_push.direct_push.readBody",
                new=AsyncMock(return_value=b"{}"),
            ),
        ):
            result = await handler._post_to_sygnal({"notification": {}})

        self.assertTrue(result)
        fake_agent.request.assert_awaited_once()
        self.assertEqual(
            fake_agent.request.await_args.args[1],
            b"https://sygnal.custom.test/_matrix/push/v1/notify",
        )
