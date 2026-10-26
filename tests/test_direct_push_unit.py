from __future__ import annotations

import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from twisted.internet import defer

from synapse_pangea_chat import PangeaChat
from synapse_pangea_chat.direct_push.direct_push import DirectPush

DEFAULT_SYGNAL_URL = "https://sygnal.default.test/_matrix/push/v1/notify"
OTHER_SYGNAL_URL = "https://sygnal.other.test/_matrix/push/v1/notify"


def _make_handler(
    send_push_sygnal_url: str | None = "https://sygnal.example.test",
    send_push_allowed_sygnal_urls: list[str] | None = None,
) -> DirectPush:
    api = MagicMock()
    api._hs.get_auth.return_value = MagicMock()
    api._hs.get_datastores.return_value = MagicMock()
    config = MagicMock()
    config.send_push_sygnal_url = send_push_sygnal_url
    config.send_push_allowed_sygnal_urls = send_push_allowed_sygnal_urls or []
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
        self.assertEqual(config.send_push_allowed_sygnal_urls, [])

    def test_parse_config_reads_allowed_sygnal_urls(self):
        config = PangeaChat.parse_config(
            {
                "cms_base_url": "http://cms.example.test",
                "cms_service_api_key": "test-api-key",
                "send_push_allowed_sygnal_urls": [OTHER_SYGNAL_URL],
            }
        )

        self.assertEqual(config.send_push_allowed_sygnal_urls, [OTHER_SYGNAL_URL])

    def test_parse_config_rejects_invalid_allowed_sygnal_urls(self):
        for value in (OTHER_SYGNAL_URL, [""], [OTHER_SYGNAL_URL, 7]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError, 'Config "send_push_allowed_sygnal_urls"'
                ):
                    PangeaChat.parse_config(
                        {
                            "cms_base_url": "http://cms.example.test",
                            "cms_service_api_key": "test-api-key",
                            "send_push_allowed_sygnal_urls": value,
                        }
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
                "sender": "@bot:example.test",
                "body": "hello",
                "title": "A new activity",
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
        self.assertEqual(payload["notification"]["sender"], "@bot:example.test")
        self.assertEqual(
            payload["notification"]["sender_display_name"], "A new activity"
        )
        self.assertEqual(payload["notification"]["room_id"], "!room:test")
        self.assertEqual(payload["notification"]["content"]["body"], "hello")
        self.assertEqual(
            payload["notification"]["content"]["format"],
            "org.matrix.custom.html",
        )
        self.assertEqual(payload["notification"]["devices"][0]["pushkey"], "push-a")

    async def test_post_stamps_the_caller_as_the_push_sender(self):
        # The client follows a notice's app path only when the push's sender is
        # its environment's bot, so the sender must be the account that asked,
        # not a constant and not whatever the body claims.
        handler = _make_handler()
        handler._config.send_push_requests_per_burst = 100
        handler._config.send_push_burst_duration_seconds = 60
        handler._auth.get_user_by_req = AsyncMock(
            return_value=SimpleNamespace(
                user=SimpleNamespace(to_string=lambda: "@bot:example.test")
            )
        )
        handler._api.is_user_admin = AsyncMock(return_value=True)
        handler._send_push = AsyncMock(return_value={"sent": 1})
        request = SimpleNamespace(
            content=BytesIO(
                b'{"user_id": "@alice:example.test", "body": "hi", "sender": "@mallory:example.test"}'
            )
        )
        with patch(
            "synapse_pangea_chat.direct_push.direct_push.respond_with_json"
        ) as respond:
            await handler._async_render_POST(request)

        respond.assert_called_once()
        self.assertEqual(respond.call_args.args[1], 200)
        sent_body = handler._send_push.await_args.args[2]
        self.assertEqual(sent_body["sender"], "@bot:example.test")

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

    async def test_post_to_sygnal_posts_to_the_given_url(self):
        handler = _make_handler()
        fake_response = SimpleNamespace(code=200)
        fake_agent = MagicMock()
        # Twisted's Agent returns Deferreds, not coroutines; the handler relies
        # on that to hand them to `make_deferred_yieldable`.
        fake_agent.request = MagicMock(return_value=defer.succeed(fake_response))

        with (
            unittest.mock.patch(
                "synapse_pangea_chat.direct_push.direct_push.Agent",
                return_value=fake_agent,
            ),
            unittest.mock.patch(
                "synapse_pangea_chat.direct_push.direct_push.readBody",
                new=MagicMock(return_value=defer.succeed(b"{}")),
            ),
        ):
            result = await handler._post_to_sygnal(
                "https://sygnal.custom.test/_matrix/push/v1/notify",
                {"notification": {}},
            )

        self.assertTrue(result)
        fake_agent.request.assert_called_once()
        self.assertEqual(
            fake_agent.request.call_args.args[1],
            b"https://sygnal.custom.test/_matrix/push/v1/notify",
        )

    async def test_post_to_sygnal_without_a_url_is_a_failed_push(self):
        handler = _make_handler(send_push_sygnal_url=None)

        self.assertFalse(await handler._post_to_sygnal(None, {"notification": {}}))


class TestSygnalChoice(unittest.IsolatedAsyncioTestCase):
    """notice-delivery.instructions.md, "Which Sygnal"."""

    def _pusher(self, device_id: str, data: dict | None) -> dict:
        return {
            "device_id": device_id,
            "app_id": "com.talktolearn.chat",
            "pushkey": f"key-{device_id}",
            "pushkey_ts": 1,
            "data": data,
        }

    def test_an_allowed_registered_url_is_used(self):
        handler = _make_handler(DEFAULT_SYGNAL_URL, [OTHER_SYGNAL_URL])

        self.assertEqual(
            handler._sygnal_url_for(self._pusher("a", {"url": OTHER_SYGNAL_URL})),
            OTHER_SYGNAL_URL,
        )

    def test_any_other_pusher_gets_the_default(self):
        handler = _make_handler(DEFAULT_SYGNAL_URL, [OTHER_SYGNAL_URL])

        for data in (
            {"url": "http://169.254.169.254/_matrix/push/v1/notify"},
            {"url": DEFAULT_SYGNAL_URL},
            {},
            None,
        ):
            with self.subTest(data=data):
                self.assertEqual(
                    handler._sygnal_url_for(self._pusher("a", data)),
                    DEFAULT_SYGNAL_URL,
                )

    async def test_send_push_posts_each_device_to_its_own_sygnal(self):
        handler = _make_handler(DEFAULT_SYGNAL_URL, [OTHER_SYGNAL_URL])
        handler._get_pushers = AsyncMock(
            return_value=[
                self._pusher("switched", {"url": OTHER_SYGNAL_URL}),
                self._pusher("native", {"url": DEFAULT_SYGNAL_URL}),
            ]
        )
        handler._post_to_sygnal = AsyncMock(return_value=True)

        result = await handler._send_push(
            "@alice:my.domain.name", None, {"body": "hello"}
        )

        self.assertEqual(result["sent"], 2)
        self.assertEqual(
            [call.args[0] for call in handler._post_to_sygnal.await_args_list],
            [OTHER_SYGNAL_URL, DEFAULT_SYGNAL_URL],
        )


class TestSygnalRejections(unittest.TestCase):
    def test_rejected_pushkey_is_not_sent(self):
        from synapse_pangea_chat.direct_push.direct_push import DirectPush

        payload = {"notification": {"devices": [{"pushkey": "key-1"}]}}
        self.assertFalse(
            DirectPush._sygnal_accepted(payload, b'{"rejected": ["key-1"]}')
        )
        self.assertTrue(
            DirectPush._sygnal_accepted(payload, b'{"rejected": ["other"]}')
        )
        self.assertTrue(DirectPush._sygnal_accepted(payload, b'{"rejected": []}'))
        self.assertTrue(DirectPush._sygnal_accepted(payload, b""))
        self.assertFalse(DirectPush._sygnal_accepted(payload, b"not json"))
