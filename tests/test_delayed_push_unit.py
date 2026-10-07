from __future__ import annotations

import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import synapse
from synapse.push.httppusher import HttpPusher

from synapse_pangea_chat import PangeaChat
from synapse_pangea_chat.delayed_push.delayed_push import (
    AUDITED_SYNAPSE_VERSION,
    CALL_RING_EVENT_TYPE,
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


class FakeSynapseClock:
    """Mirrors synapse.util.Clock.call_later on Synapse >=1.159, which takes a
    Duration and returns a wrapper with active()/cancel()."""

    def __init__(self):
        self.calls = []

    def call_later(self, duration, callback):
        delayed_call = FakeDelayedCall(duration.as_secs(), callback)
        self.calls.append(delayed_call)
        return delayed_call


class FakePresenceHandler:
    def __init__(
        self,
        active: bool = True,
        state: str = "online",
        error: Exception | None = None,
    ):
        if error is not None:
            self.current_state_for_user = AsyncMock(side_effect=error)
        else:
            self.current_state_for_user = AsyncMock(
                return_value=SimpleNamespace(state=state, currently_active=active)
            )


class FakeHomeServer:
    def __init__(
        self,
        *,
        active: bool = True,
        presence_state: str | None = None,
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
        self.clock = FakeSynapseClock()
        if presence_state is None:
            presence_state = "online" if active else "offline"
        self.presence_handler = FakePresenceHandler(
            active,
            presence_state,
            presence_error,
        )

    def get_clock(self):
        return self.clock

    def get_presence_handler(self):
        return self.presence_handler


class FakePusher:
    MAX_BACKOFF_SEC = HttpPusher.MAX_BACKOFF_SEC

    def __init__(self, *, active: bool = True, event_age_ms: int = 1_000):
        self.user_id = "@alice:example.test"
        self.app_id = "app"
        self.app_display_name = "App"
        self.pushkey = "pushkey"
        self.name = "@alice:example.test/app/pushkey"
        self.server_name = "example.test"
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
            # The queued-ring lookup: the newest ring's stream ordering, or None.
            db_pool=SimpleNamespace(runInteraction=AsyncMock(return_value=None)),
        )
        self.events: dict[str, SimpleNamespace] = {}
        self.push_action = self.add_action(
            "$event", stream_ordering=5, event_age_ms=event_age_ms
        )
        self.event = self.events["$event"]
        self.store.get_unread_push_actions_for_user_in_range_for_http.return_value = [
            self.push_action
        ]
        self.store.get_event.side_effect = lambda event_id, allow_none: self.events.get(
            event_id
        )
        self._pangea_delayed_push_config = PangeaChat.parse_config(
            _base_config(enabled=True, delay_ms=60_000, max_delay_ms=600_000)
        )

    def add_action(
        self,
        event_id: str,
        *,
        stream_ordering: int,
        event_type: str = "m.room.message",
        event_age_ms: int = 1_000,
    ) -> SimpleNamespace:
        self.events[event_id] = SimpleNamespace(
            event_id=event_id,
            type=event_type,
            room_id="!room:example.test",
            origin_server_ts=self.clock.time_msec() - event_age_ms,
        )
        return SimpleNamespace(
            event_id=event_id,
            stream_ordering=stream_ordering,
            actions=["notify"],
        )

    def hold(self, push_action: SimpleNamespace) -> FakeDelayedCall:
        """Puts the pusher in the state a deferral of push_action leaves it in."""
        hold_timer = FakeDelayedCall(60, self.on_timer)
        self.timed_call = hold_timer
        self._pangea_delayed_push_event_id = push_action.event_id
        self._pangea_delayed_push_stream_ordering = push_action.stream_ordering
        self._pangea_delayed_push_until_ms = self.clock.time_msec() + 60_000
        return hold_timer


class TestDelayedPushConfig(unittest.TestCase):
    def test_parse_config_includes_delayed_push_defaults(self):
        config = PangeaChat.parse_config(_base_config())

        self.assertFalse(config.delayed_push_enabled)
        self.assertEqual(config.delayed_push_delay_ms, 60_000)
        self.assertEqual(config.delayed_push_max_delay_ms, 600_000)
        self.assertEqual(config.delayed_push_require_synapse_version, "1.159.0")

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
            "1.124.0",
        ):
            with self.assertRaisesRegex(ValueError, "audited for Synapse 1.159.0"):
                configure_delayed_push(config)

    def test_configure_delayed_push_rejects_config_contradicting_code_audit(self):
        # The misdeploy scenario: a stale inventory claims an audit of an older
        # Synapse, and the running server matches that claim — but this
        # commit's patch body doesn't. Must fail at boot, not at first push.
        config = PangeaChat.parse_config(
            _base_config(enabled=True, require_synapse_version="1.124.0")
        )

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.synapse.__version__",
            "1.124.0",
        ):
            with self.assertRaisesRegex(
                ValueError,
                "audited for Synapse " f"{AUDITED_SYNAPSE_VERSION}".replace(".", r"\."),
            ):
                configure_delayed_push(config)

    def test_configure_delayed_push_patches_when_version_matches(self):
        config = PangeaChat.parse_config(_base_config(enabled=True))

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.synapse.__version__",
            "1.159.0",
        ):
            configure_delayed_push(config)

        self.assertTrue(HttpPusher._pangea_delayed_push_patched)
        self.assertIs(HttpPusher._pangea_delayed_push_config, config)


@unittest.skipUnless(
    # base version: __version__ carries a git suffix inside a git checkout
    synapse.__version__.split(" ")[0] == AUDITED_SYNAPSE_VERSION,
    "the patched HttpPusher body mirrors Synapse "
    f"{AUDITED_SYNAPSE_VERSION} internals; on any other version the module "
    "refuses to enable delayed_push (exact-version guard), so the body is "
    "only testable on the audited version",
)
class TestDelayedPushHelpers(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        reset_delayed_push_patch_for_tests()

    async def test_unsafe_process_defers_online_user_without_advancing_cursor(self):
        pusher = FakePusher(active=True, event_age_ms=1_000)

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.httppusher.opentracing.start_active_span",
            return_value=nullcontext(),
        ):
            await _pangea_delayed_push_unsafe_process(pusher)

        self.assertEqual(pusher.last_stream_ordering, 1)
        pusher._process_one.assert_not_awaited()
        pusher.store.update_pusher_last_stream_ordering_and_success.assert_not_awaited()
        self.assertEqual(len(pusher.hs.clock.calls), 1)
        self.assertEqual(pusher.hs.clock.calls[0].delay_seconds, 60)
        self.assertEqual(pusher._pangea_delayed_push_event_id, "$event")
        self.assertEqual(
            pusher._pangea_delayed_push_until_ms,
            pusher.clock.time_msec() + 60_000,
        )

    async def test_unsafe_process_defers_user_who_is_online_but_not_currently_active(
        self,
    ):
        pusher = FakePusher(active=False, event_age_ms=1_000)
        pusher.hs = FakeHomeServer(active=False, presence_state="online")

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.httppusher.opentracing.start_active_span",
            return_value=nullcontext(),
        ):
            await _pangea_delayed_push_unsafe_process(pusher)

        self.assertEqual(pusher.last_stream_ordering, 1)
        pusher._process_one.assert_not_awaited()
        pusher.store.update_pusher_last_stream_ordering_and_success.assert_not_awaited()
        self.assertEqual(len(pusher.hs.clock.calls), 1)

    async def test_unsafe_process_sends_when_user_is_offline_but_currently_active(
        self,
    ):
        pusher = FakePusher(active=True, event_age_ms=1_000)
        pusher.hs = FakeHomeServer(active=True, presence_state="offline")

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.httppusher.opentracing.start_active_span",
            return_value=nullcontext(),
        ):
            await _pangea_delayed_push_unsafe_process(pusher)

        pusher._process_one.assert_awaited_once_with(pusher.push_action)
        self.assertEqual(pusher.last_stream_ordering, 5)
        pusher.store.update_pusher_last_stream_ordering_and_success.assert_awaited_once()
        self.assertEqual(pusher.hs.clock.calls, [])

    async def test_unsafe_process_sends_when_user_is_offline_and_not_currently_active(
        self,
    ):
        pusher = FakePusher(active=False, event_age_ms=1_000)

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.httppusher.opentracing.start_active_span",
            return_value=nullcontext(),
        ):
            await _pangea_delayed_push_unsafe_process(pusher)

        pusher._process_one.assert_awaited_once_with(pusher.push_action)
        self.assertEqual(pusher.last_stream_ordering, 5)
        pusher.store.update_pusher_last_stream_ordering_and_success.assert_awaited_once()
        self.assertEqual(pusher.hs.clock.calls, [])

    async def test_unsafe_process_sends_when_max_delay_reached(self):
        pusher = FakePusher(active=True, event_age_ms=600_000)

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.httppusher.opentracing.start_active_span",
            return_value=nullcontext(),
        ):
            await _pangea_delayed_push_unsafe_process(pusher)

        pusher._process_one.assert_awaited_once_with(pusher.push_action)
        self.assertEqual(pusher.last_stream_ordering, 5)
        self.assertEqual(pusher.hs.clock.calls, [])

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

    async def _process(self, pusher: FakePusher) -> None:
        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.httppusher.opentracing.start_active_span",
            return_value=nullcontext(),
        ):
            await _pangea_delayed_push_unsafe_process(pusher)

    def _sent_event_ids(self, pusher: FakePusher) -> list[str]:
        return [call.args[0].event_id for call in pusher._process_one.await_args_list]

    async def test_ring_sends_immediately_for_online_user(self):
        pusher = FakePusher(active=True)
        ring = pusher.add_action(
            "$ring", stream_ordering=5, event_type=CALL_RING_EVENT_TYPE
        )
        pusher.store.get_unread_push_actions_for_user_in_range_for_http.return_value = [
            ring
        ]

        await self._process(pusher)

        self.assertEqual(self._sent_event_ids(pusher), ["$ring"])
        self.assertEqual(pusher.last_stream_ordering, 5)
        self.assertEqual(pusher.hs.clock.calls, [])

    async def test_ring_queued_behind_a_message_sends_both_in_order(self):
        pusher = FakePusher(active=True)
        ring = pusher.add_action(
            "$ring", stream_ordering=7, event_type=CALL_RING_EVENT_TYPE
        )
        pusher.store.get_unread_push_actions_for_user_in_range_for_http.return_value = [
            pusher.push_action,
            ring,
        ]
        pusher.store.db_pool.runInteraction.return_value = 7

        await self._process(pusher)

        self.assertEqual(self._sent_event_ids(pusher), ["$event", "$ring"])
        self.assertEqual(pusher.last_stream_ordering, 7)
        self.assertEqual(pusher.hs.clock.calls, [])

    async def test_message_after_the_ring_is_still_held(self):
        pusher = FakePusher(active=True)
        ring = pusher.add_action(
            "$ring", stream_ordering=7, event_type=CALL_RING_EVENT_TYPE
        )
        later = pusher.add_action("$later", stream_ordering=9)
        pusher.store.get_unread_push_actions_for_user_in_range_for_http.return_value = [
            pusher.push_action,
            ring,
            later,
        ]
        # The lookup that finds the ring, then the one past it that finds none.
        pusher.store.db_pool.runInteraction.side_effect = [7, None]

        await self._process(pusher)

        self.assertEqual(self._sent_event_ids(pusher), ["$event", "$ring"])
        self.assertEqual(pusher.last_stream_ordering, 7)
        self.assertEqual(pusher._pangea_delayed_push_event_id, "$later")
        self.assertEqual(len(pusher.hs.clock.calls), 1)

    async def test_wake_during_a_hold_without_a_ring_keeps_the_hold(self):
        pusher = FakePusher(active=True)
        hold_timer = pusher.hold(pusher.push_action)

        await self._process(pusher)

        pusher.store.get_unread_push_actions_for_user_in_range_for_http.assert_not_awaited()
        pusher._process_one.assert_not_awaited()
        self.assertFalse(hold_timer.cancelled)
        self.assertEqual(pusher._pangea_delayed_push_event_id, "$event")

    async def test_ring_arriving_during_a_hold_releases_it(self):
        pusher = FakePusher(active=True)
        hold_timer = pusher.hold(pusher.push_action)
        ring = pusher.add_action(
            "$ring", stream_ordering=7, event_type=CALL_RING_EVENT_TYPE
        )
        pusher.store.get_unread_push_actions_for_user_in_range_for_http.return_value = [
            pusher.push_action,
            ring,
        ]
        pusher.store.db_pool.runInteraction.return_value = 7

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.logger.info"
        ) as log_info:
            await self._process(pusher)

        self.assertTrue(hold_timer.cancelled)
        self.assertEqual(self._sent_event_ids(pusher), ["$event", "$ring"])
        self.assertEqual(pusher.last_stream_ordering, 7)
        self.assertFalse(hasattr(pusher, "_pangea_delayed_push_event_id"))
        self.assertEqual(pusher.hs.clock.calls, [])
        # Releasing the hold is not reading the held event.
        logged = [call.args[0] for call in log_info.call_args_list]
        self.assertFalse(any("suppressing" in message for message in logged))

    async def test_ring_beyond_one_fetch_is_still_reached(self):
        # Synapse returns 20 push actions per fetch; a hold can queue more.
        pusher = FakePusher(active=True)
        ring = pusher.add_action(
            "$ring", stream_ordering=9, event_type=CALL_RING_EVENT_TYPE
        )
        pusher.store.get_unread_push_actions_for_user_in_range_for_http.side_effect = [
            [pusher.push_action],
            [ring],
        ]
        pusher.store.db_pool.runInteraction.return_value = 9

        await self._process(pusher)

        self.assertEqual(self._sent_event_ids(pusher), ["$event", "$ring"])
        self.assertEqual(pusher.last_stream_ordering, 9)
        self.assertEqual(
            pusher.store.get_unread_push_actions_for_user_in_range_for_http.await_count,
            2,
        )

    async def test_failed_push_ahead_of_a_ring_backs_off_instead_of_refetching(self):
        pusher = FakePusher(active=True)
        pusher.store.db_pool.runInteraction.return_value = 9
        pusher._process_one.return_value = False

        await self._process(pusher)

        self.assertEqual(
            pusher.store.get_unread_push_actions_for_user_in_range_for_http.await_count,
            1,
        )
        self.assertEqual(pusher.last_stream_ordering, 1)
        self.assertEqual(len(pusher.hs.clock.calls), 1)
        self.assertEqual(pusher.hs.clock.calls[0].delay_seconds, 1)

    async def test_ring_lookup_failure_during_a_hold_sends_normally(self):
        pusher = FakePusher(active=True)
        hold_timer = pusher.hold(pusher.push_action)
        pusher.store.db_pool.runInteraction.side_effect = RuntimeError("db down")

        with patch(
            "synapse_pangea_chat.delayed_push.delayed_push.logger.exception"
        ) as log_exception:
            await self._process(pusher)

        self.assertTrue(hold_timer.cancelled)
        self.assertEqual(self._sent_event_ids(pusher), ["$event"])
        self.assertEqual(pusher.last_stream_ordering, 5)
        # Once for the hold, once for the held event's own decision.
        self.assertEqual(log_exception.call_count, 2)
