from __future__ import annotations

import logging
from typing import Any, Protocol

import synapse
from synapse.push import httppusher
from synapse.push.httppusher import HttpPusher
from twisted.internet.error import AlreadyCalled, AlreadyCancelled

logger = logging.getLogger(__name__)


_ORIGINAL_UNSAFE_PROCESS_ATTR = "_pangea_delayed_push_original_unsafe_process"
_ORIGINAL_START_PROCESSING_ATTR = "_pangea_delayed_push_original_start_processing"
_PATCHED_ATTR = "_pangea_delayed_push_patched"
_CONFIG_ATTR = "_pangea_delayed_push_config"


class DelayedPushConfigProtocol(Protocol):
    @property
    def delayed_push_enabled(self) -> bool:
        ...

    @property
    def delayed_push_delay_ms(self) -> int:
        ...

    @property
    def delayed_push_max_delay_ms(self) -> int:
        ...

    @property
    def delayed_push_require_synapse_version(self) -> str:
        ...


def configure_delayed_push(config: DelayedPushConfigProtocol) -> None:
    """Install the delayed HTTP push monkey patch when enabled."""
    if not config.delayed_push_enabled:
        return

    _require_audited_synapse_version(config.delayed_push_require_synapse_version)
    _install_delayed_push_patch(config)


def reset_delayed_push_patch_for_tests() -> None:
    """Restore HttpPusher methods patched by configure_delayed_push.

    This is intentionally only for isolated unit tests; production code should never
    unpatch while Synapse is running.
    """
    if not getattr(HttpPusher, _PATCHED_ATTR, False):
        return

    original_unsafe_process = getattr(HttpPusher, _ORIGINAL_UNSAFE_PROCESS_ATTR)
    original_start_processing = getattr(HttpPusher, _ORIGINAL_START_PROCESSING_ATTR)
    HttpPusher._unsafe_process = original_unsafe_process  # type: ignore[method-assign]
    HttpPusher._start_processing = original_start_processing  # type: ignore[method-assign]

    for attr_name in (
        _ORIGINAL_UNSAFE_PROCESS_ATTR,
        _ORIGINAL_START_PROCESSING_ATTR,
        _PATCHED_ATTR,
        _CONFIG_ATTR,
    ):
        if hasattr(HttpPusher, attr_name):
            delattr(HttpPusher, attr_name)


def _require_audited_synapse_version(required_version: str) -> None:
    actual_version = getattr(synapse, "__version__", "")
    if actual_version != required_version:
        raise ValueError(
            "delayed_push is enabled but this synapse-pangea-chat commit was "
            f"audited for Synapse {required_version}; running Synapse "
            f"{actual_version or 'unknown'}"
        )


def _install_delayed_push_patch(config: DelayedPushConfigProtocol) -> None:
    if not getattr(HttpPusher, _PATCHED_ATTR, False):
        setattr(HttpPusher, _ORIGINAL_UNSAFE_PROCESS_ATTR, HttpPusher._unsafe_process)
        setattr(
            HttpPusher,
            _ORIGINAL_START_PROCESSING_ATTR,
            HttpPusher._start_processing,
        )
        HttpPusher._unsafe_process = _pangea_delayed_push_unsafe_process  # type: ignore[method-assign]
        HttpPusher._start_processing = _pangea_delayed_push_start_processing  # type: ignore[method-assign]
        setattr(HttpPusher, _PATCHED_ATTR, True)

    setattr(HttpPusher, _CONFIG_ATTR, config)
    logger.info(
        "Pangea delayed HTTP push enabled: delay_ms=%s max_delay_ms=%s "
        "require_synapse_version=%s",
        config.delayed_push_delay_ms,
        config.delayed_push_max_delay_ms,
        config.delayed_push_require_synapse_version,
    )


def _get_delayed_push_config(self: Any) -> DelayedPushConfigProtocol | None:
    return getattr(self, _CONFIG_ATTR, None) or getattr(type(self), _CONFIG_ATTR, None)


def _delayed_push_pending(self: Any, config: DelayedPushConfigProtocol | None) -> bool:
    if config is None or not config.delayed_push_enabled:
        return False

    delayed_until_ms = getattr(self, "_pangea_delayed_push_until_ms", None)
    if delayed_until_ms is None:
        return False

    return delayed_until_ms > self.clock.time_msec()


def _pangea_delayed_push_start_processing(self: Any) -> None:
    config = _get_delayed_push_config(self)
    if _delayed_push_pending(self, config):
        logger.debug(
            "Skipping early HTTP pusher wake while delayed push is pending for %s "
            "until %s",
            getattr(self, "name", "<unknown pusher>"),
            getattr(self, "_pangea_delayed_push_until_ms", None),
        )
        return

    original_start_processing = getattr(type(self), _ORIGINAL_START_PROCESSING_ATTR)
    original_start_processing(self)


async def _pangea_delayed_push_unsafe_process(self: Any) -> None:
    """HttpPusher._unsafe_process with Pangea active-user deferral.

    This is a private Synapse API monkey patch. It intentionally mirrors Synapse
    v1.124.0's HttpPusher._unsafe_process, adding one pre-_process_one decision
    point that may reschedule the pusher without advancing last_stream_ordering.
    """
    config = _get_delayed_push_config(self)
    if _delayed_push_pending(self, config):
        return

    unprocessed = await self.store.get_unread_push_actions_for_user_in_range_for_http(
        self.user_id, self.last_stream_ordering, self.max_stream_ordering
    )
    _log_deferred_event_if_no_longer_unread(self, unprocessed)

    logger.info(
        "Processing %i unprocessed push actions for %s starting at "
        "stream_ordering %s",
        len(unprocessed),
        self.name,
        self.last_stream_ordering,
    )

    for push_action in unprocessed:
        with httppusher.opentracing.start_active_span(
            "http-push",
            tags={
                "authenticated_entity": self.user_id,
                "event_id": push_action.event_id,
                "app_id": self.app_id,
                "app_display_name": self.app_display_name,
            },
        ):
            should_defer = False
            try:
                should_defer = await _should_defer_push_action(self, push_action)
            except Exception:
                logger.exception(
                    "Pangea delayed push decision failed for user %s event %s; "
                    "sending normally",
                    self.user_id,
                    push_action.event_id,
                )
                _clear_delayed_push_state(self)

            if should_defer:
                _schedule_delayed_push(self, push_action, config)
                return

            processed = await self._process_one(push_action)
            if processed:
                httppusher.http_push_processed_counter.inc()
                self.backoff_delay = HttpPusher.INITIAL_BACKOFF_SEC
                self.last_stream_ordering = push_action.stream_ordering
                pusher_still_exists = (
                    await self.store.update_pusher_last_stream_ordering_and_success(
                        self.app_id,
                        self.pushkey,
                        self.user_id,
                        self.last_stream_ordering,
                        self.clock.time_msec(),
                    )
                )
                if not pusher_still_exists:
                    # The pusher has been deleted while we were processing, so
                    # lets just stop and return.
                    self.on_stop()
                    return

                if self.failing_since:
                    self.failing_since = None
                    await self.store.update_pusher_failing_since(
                        self.app_id, self.pushkey, self.user_id, self.failing_since
                    )
            else:
                httppusher.http_push_failed_counter.inc()
                if not self.failing_since:
                    self.failing_since = self.clock.time_msec()
                    await self.store.update_pusher_failing_since(
                        self.app_id, self.pushkey, self.user_id, self.failing_since
                    )

                if (
                    self.failing_since
                    and self.failing_since
                    < self.clock.time_msec() - HttpPusher.GIVE_UP_AFTER_MS
                ):
                    # we really only give up so that if the URL gets
                    # fixed, we don't suddenly deliver a load
                    # of old notifications.
                    logger.warning(
                        "Giving up on a notification to user %s, pushkey %s",
                        self.user_id,
                        self.pushkey,
                    )
                    self.backoff_delay = HttpPusher.INITIAL_BACKOFF_SEC
                    self.last_stream_ordering = push_action.stream_ordering
                    await self.store.update_pusher_last_stream_ordering(
                        self.app_id,
                        self.pushkey,
                        self.user_id,
                        self.last_stream_ordering,
                    )
                    self.failing_since = None
                    await self.store.update_pusher_failing_since(
                        self.app_id, self.pushkey, self.user_id, self.failing_since
                    )
                else:
                    logger.info("Push failed: delaying for %ds", self.backoff_delay)
                    self.timed_call = self.hs.get_reactor().callLater(
                        self.backoff_delay, self.on_timer
                    )
                    self.backoff_delay = min(
                        self.backoff_delay * 2, self.MAX_BACKOFF_SEC
                    )
                    break


async def _should_defer_push_action(self: Any, push_action: Any) -> bool:
    config = _get_delayed_push_config(self)
    if config is None or not config.delayed_push_enabled:
        return False

    if "notify" not in push_action.actions:
        return False

    event = await self.store.get_event(push_action.event_id, allow_none=True)
    if event is None:
        return False

    event_age_ms = self.clock.time_msec() - event.origin_server_ts
    if event_age_ms >= config.delayed_push_max_delay_ms:
        logger.info(
            "Pangea delayed push sending event %s for user %s because age_ms=%s "
            "reached max_delay_ms=%s",
            push_action.event_id,
            self.user_id,
            event_age_ms,
            config.delayed_push_max_delay_ms,
        )
        _clear_delayed_push_state(self)
        return False

    if not await _user_is_currently_active(self):
        logger.info(
            "Pangea delayed push sending event %s for user %s because user is not "
            "currently active",
            push_action.event_id,
            self.user_id,
        )
        _clear_delayed_push_state(self)
        return False

    logger.info(
        "Pangea delayed push deferring event %s for active user %s: age_ms=%s "
        "delay_ms=%s max_delay_ms=%s",
        push_action.event_id,
        self.user_id,
        event_age_ms,
        config.delayed_push_delay_ms,
        config.delayed_push_max_delay_ms,
    )
    return True


async def _user_is_currently_active(self: Any) -> bool:
    server_config = getattr(getattr(self.hs, "config", None), "server", None)
    if getattr(server_config, "presence_enabled", True) is False:
        return False
    if getattr(server_config, "track_presence", True) is False:
        return False

    presence_handler = self.hs.get_presence_handler()
    state = await presence_handler.current_state_for_user(self.user_id)
    return bool(getattr(state, "currently_active", False))


def _schedule_delayed_push(
    self: Any,
    push_action: Any,
    config: DelayedPushConfigProtocol | None,
) -> None:
    if config is None:
        return

    _cancel_existing_timed_call(self)
    delay_seconds = config.delayed_push_delay_ms / 1000
    self._pangea_delayed_push_event_id = push_action.event_id
    self._pangea_delayed_push_stream_ordering = push_action.stream_ordering
    self._pangea_delayed_push_until_ms = (
        self.clock.time_msec() + config.delayed_push_delay_ms
    )
    self.timed_call = self.hs.get_reactor().callLater(delay_seconds, self.on_timer)


def _cancel_existing_timed_call(self: Any) -> None:
    timed_call = getattr(self, "timed_call", None)
    if timed_call is None:
        return

    try:
        is_active = timed_call.active()
    except AttributeError:
        is_active = False

    if not is_active:
        return

    try:
        timed_call.cancel()
    except (AlreadyCalled, AlreadyCancelled):
        pass


def _clear_delayed_push_state(self: Any) -> None:
    for attr_name in (
        "_pangea_delayed_push_event_id",
        "_pangea_delayed_push_stream_ordering",
        "_pangea_delayed_push_until_ms",
    ):
        if hasattr(self, attr_name):
            delattr(self, attr_name)


def _log_deferred_event_if_no_longer_unread(self: Any, unprocessed: list[Any]) -> None:
    pending_event_id = getattr(self, "_pangea_delayed_push_event_id", None)
    if pending_event_id is None:
        return

    if pending_event_id in {push_action.event_id for push_action in unprocessed}:
        return

    logger.info(
        "Pangea delayed push suppressing previously deferred event %s for user %s "
        "because Synapse no longer returns it as unread",
        pending_event_id,
        self.user_id,
    )
    _clear_delayed_push_state(self)
