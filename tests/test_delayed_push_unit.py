from __future__ import annotations

import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat import PangeaChat
from synapse_pangea_chat.delayed_push.delayed_push import (
    _pangea_delayed_push_start_processing,
    _pangea_delayed_push_unsafe_process,
    configure_delayed_push,
    reset_delayed_push_patch_for_tests,
)


def _base_config(**delayed_push):
    config = {
        "cms_base_url": "http://cms.example.test",
        "cms_service_api_key": "test-api-key",
    }
    if delayed_push:
        config["delayed_push"] = delayed_push
    return config


class FakeClock:
    def __init__(self, now_ms: int = 1_000_000):
        self.now_ms = now_ms

    def time_msec(self) -> int:
        return self.now_ms


class FakeDelayedCall:
    def __init__(self, delay_seconds, callback):
        self.delay_seconds = delay_seconds
        self.callback = callback
        self.cancelled = False

    def active(self):
        return not self.cancelled

    def cancel(self):
        self.cancelled = True


class FakeReactor:
    def __init__(self):
        self.calls = []

    def callLater(self, delay_seconds, callback):
        delayed_call = FakeDelayedCall(delay_seconds, callback)
        self.calls.append(delayed_call)
        return delayed_call


class FakePresenceHandler:
    def __init__(self, active: bool = True, error: Exception | None = None):
        if error is not None:
            self.current_state_for_user = AsyncMock(side_effect=error)
        else:
            self.current_state_for_user = AsyncMock(
                return_value=SimpleNamespace(currently_active=active)
            )


class FakeHomeServer:
    def __init__(
        self,
        *,
        active: bool = True,
        presence_enabled: bool = True,
        track_presence: bool = True,
        presence_error: Exception | None = None,
    ):
        self.config = SimpleNamespace(
            server=SimpleNamespace(
                presence_enabled=presence_enabled,
                track_presence=track_presence,
            )
        )
        self.reactor = FakeReactor()
        self.presence_handler = FakePresenceHandler(active, presence_error)

    def get_reactor(self):
        return self.reactor

    def get_presence_handler(self):
        return self.presence_handler


class FakePusher:
    def __init__(self, *, active: bool = True, event_age_ms: int = 1_000):
        self.user_id = "@alice:example.test"
        self.app_id = "app"
        self.app_display_name = "App"
        self.pushkey = "pushkey"
        self.name = "@alice:example.test/app/pushkey"
        self.last_stream_ordering = 1
        self.max_stream_ordering = 10
        self.backoff_delay = 1
        self.failing_since = None
        self.timed_call = None
        self.clock = FakeClock()
        self.hs = FakeHomeServer(active=active)
        self._pusherpool = MagicMock()
        self.on_timer = MagicMock()
        self.on_stop = MagicMock()
        self._process_one = AsyncMock(return_value=True)
        self.store = SimpleNamespace(
            get_unread_push_actions_for_user_in_range_for_http=AsyncMock(),
            get_event=AsyncMock(),
            update_pusher_last_stream_ordering_and_success=AsyncMock(return_value=True),
            update_pusher_failing_since=AsyncMock(),
            update_pusher_last_stream_ordering=AsyncMock(),
        )
        self.push_action = SimpleNamespace(
            event_id="$event",
            stream_ordering=5,
            actions=["notify"],
        )
        self.event = SimpleNamespace(
            event_id="$event",
            room_id="!room:example.test",
            origin_server_ts=self.clock.time_msec() - event_age_ms,
        )
        self.store.get_unread_push_actions_for_user_in_range_for_http.return_value = [
            self.push_action
        ]
        self.store.get_event.return_value = self.event
        self._pangea_delayed_push_config = PangeaChat.parse_config(
            _base_config(enabled=True, delay_ms=60_000, max_delay_ms=600_000)
        )


class TestDelayedPushConfig(unittest.TestCase):
    def test_parse_config_includes_delayed_push_defaults(self):
        config = PangeaChat.parse_config(_base_config())

        self.assertFalse(config.delayed_push_enabled)
        self.assertEqual(config.delayed_push_delay_ms, 60_000)
        self.assertEqual(config.delayed_push_max_delay_ms, 600_000)
        self.assertEqual(config.delayed_push_require_synapse_version, "1.124.0")

    def test_parse_config_includes_delayed_push_overrides(self):
        config = PangeaChat.parse_config(
            _base_config(
                enabled=True,
                delay_ms=30_000,
                max_delay_ms=300_000,
                require_synapse_version="1.124.0",
            )
        )

        self.assertTrue(config.delayed_push_enabled)
        self.assertEqual(config.delayed_push_delay_ms, 30_000)
        self.assertEqual(config.delayed_push_max_delay_ms, 300_000)
        self.assertEqual(config.delayed_push_require_synapse_version, "1.124.0")

    def test_parse_config_rejects_invalid_delayed_push_values(self):
        invalid_cases = [
            ({"delayed_push": []}, 'Config "delayed_push"'),
            ({"delayed_push": {"enabled": "yes"}}, "delayed_push.enabled"),
            ({"delayed_push": {"delay_ms": 0}}, "delayed_push.delay_ms"),
            ({"delayed_push": {"max_delay_ms": 0}}, "delayed_push.max_delay_ms"),
            (
                {"delayed_push": {"delay_ms": 60_000, "max_delay_ms": 1_000}},
                "delayed_push.max_delay_ms",
            ),
            (
                {"delayed_push": {"require_synapse_version": ""}},
                "delayed_push.require_synapse_version",
            ),
        ]
        for overrides, message in invalid_cases:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, message):
                    PangeaChat.parse_config(
                        {
                            "cms_base_url": "http://cms.example.test",
                            "cms_service_api_key": "test-api-key",
                            **overrides,
                        }
                    )


class TestDelayedPushPatch(unittest.TestCase):
    def tearDown(self):
        reset_delayed_push_patch_for_tests()

    def test_configure_delayed_push_requires_audited_synapse_version(self):
        config = PangeaChat.parse_config(_base_config(enabled=True))

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.synapse.__version__",
            "1.125.0",
        ):
            with self.assertRaisesRegex(ValueError, "audited for Synapse 1.124.0"):
                configure_delayed_push(config)

    def test_configure_delayed_push_patches_when_version_matches(self):
        config = PangeaChat.parse_config(_base_config(enabled=True))

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.synapse.__version__",
            "1.124.0",
        ):
            configure_delayed_push(config)

        from synapse.push.httppusher import HttpPusher

        self.assertTrue(HttpPusher._pangea_delayed_push_patched)
        self.assertIs(HttpPusher._pangea_delayed_push_config, config)


class TestDelayedPushHelpers(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        reset_delayed_push_patch_for_tests()

    async def test_unsafe_process_defers_active_user_without_advancing_cursor(self):
        pusher = FakePusher(active=True, event_age_ms=1_000)

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.httppusher.opentracing.start_active_span",
            return_value=nullcontext(),
        ):
            await _pangea_delayed_push_unsafe_process(pusher)

        self.assertEqual(pusher.last_stream_ordering, 1)
        pusher._process_one.assert_not_awaited()
        pusher.store.update_pusher_last_stream_ordering_and_success.assert_not_awaited()
        self.assertEqual(len(pusher.hs.reactor.calls), 1)
        self.assertEqual(pusher.hs.reactor.calls[0].delay_seconds, 60)
        self.assertEqual(pusher._pangea_delayed_push_event_id, "$event")
        self.assertEqual(
            pusher._pangea_delayed_push_until_ms,
            pusher.clock.time_msec() + 60_000,
        )

    async def test_unsafe_process_sends_when_user_is_inactive(self):
        pusher = FakePusher(active=False, event_age_ms=1_000)

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.httppusher.opentracing.start_active_span",
            return_value=nullcontext(),
        ):
            await _pangea_delayed_push_unsafe_process(pusher)

        pusher._process_one.assert_awaited_once_with(pusher.push_action)
        self.assertEqual(pusher.last_stream_ordering, 5)
        pusher.store.update_pusher_last_stream_ordering_and_success.assert_awaited_once()
        self.assertEqual(pusher.hs.reactor.calls, [])

    async def test_unsafe_process_sends_when_max_delay_reached(self):
        pusher = FakePusher(active=True, event_age_ms=600_000)

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.httppusher.opentracing.start_active_span",
            return_value=nullcontext(),
        ):
            await _pangea_delayed_push_unsafe_process(pusher)

        pusher._process_one.assert_awaited_once_with(pusher.push_action)
        self.assertEqual(pusher.last_stream_ordering, 5)
        self.assertEqual(pusher.hs.reactor.calls, [])

    async def test_unsafe_process_fails_open_when_presence_lookup_errors(self):
        pusher = FakePusher(active=True, event_age_ms=1_000)
        pusher.hs = FakeHomeServer(
            active=True,
            presence_error=RuntimeError("presence unavailable"),
        )

        with (
            patch(
                "synapse_pangea_chat.delayed_push.delayed_push.httppusher.opentracing.start_active_span",
                return_value=nullcontext(),
            ),
            patch(
                "synapse_pangea_chat.delayed_push.delayed_push.logger.exception"
            ) as log_exception,
        ):
            await _pangea_delayed_push_unsafe_process(pusher)

        log_exception.assert_called_once()
        pusher._process_one.assert_awaited_once_with(pusher.push_action)
        self.assertEqual(pusher.last_stream_ordering, 5)

    async def test_unsafe_process_sends_when_presence_is_disabled(self):
        pusher = FakePusher(active=True, event_age_ms=1_000)
        pusher.hs = FakeHomeServer(active=True, presence_enabled=False)

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.httppusher.opentracing.start_active_span",
            return_value=nullcontext(),
        ):
            await _pangea_delayed_push_unsafe_process(pusher)

        pusher._process_one.assert_awaited_once_with(pusher.push_action)
        self.assertEqual(pusher.last_stream_ordering, 5)

    async def test_unsafe_process_does_not_manually_advance_when_read_disappears(self):
        pusher = FakePusher(active=True, event_age_ms=1_000)
        pusher._pangea_delayed_push_event_id = "$event"
        pusher.store.get_unread_push_actions_for_user_in_range_for_http.return_value = (
            []
        )

        await _pangea_delayed_push_unsafe_process(pusher)

        self.assertEqual(pusher.last_stream_ordering, 1)
        pusher.store.update_pusher_last_stream_ordering.assert_not_awaited()
        pusher.store.update_pusher_last_stream_ordering_and_success.assert_not_awaited()
        self.assertFalse(hasattr(pusher, "_pangea_delayed_push_event_id"))


class TestDelayedPushStartProcessing(unittest.TestCase):
    def test_start_processing_ignores_early_wake_until_delay_expires(self):
        class FakeStartPusher:
            _pangea_delayed_push_config = PangeaChat.parse_config(
                _base_config(enabled=True)
            )

            def __init__(self):
                self.clock = FakeClock(now_ms=1_000)
                self.name = "pusher"
                self.started = False
                self._pangea_delayed_push_until_ms = 2_000

            def original_start(self):
                self.started = True

        FakeStartPusher._pangea_delayed_push_original_start_processing = (
            FakeStartPusher.original_start
        )
        pusher = FakeStartPusher()

        _pangea_delayed_push_start_processing(pusher)
        self.assertFalse(pusher.started)

        pusher.clock.now_ms = 2_000
        _pangea_delayed_push_start_processing(pusher)
        self.assertTrue(pusher.started)
