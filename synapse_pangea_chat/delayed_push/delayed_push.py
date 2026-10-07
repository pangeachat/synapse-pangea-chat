from __future__ import annotations

import logging
from typing import Any, Protocol

import synapse
from synapse.push import httppusher
from synapse.push.httppusher import HttpPusher
from synapse.storage.database import LoggingTransaction
from twisted.internet.error import AlreadyCalled, AlreadyCancelled

logger = logging.getLogger(__name__)


AUDITED_SYNAPSE_VERSION = "1.159.0"
"""The exact Synapse version whose private HttpPusher internals the patched
body below mirrors. A property of this commit's code, not of configuration:
the config's require_synapse_version can only confirm this value, never select
a different one, so a stale inventory claim cannot boot the patch against
internals it was not audited for."""


CALL_RING_EVENT_TYPE = "org.matrix.msc4075.rtc.notification"
"""The call ring (MSC4075), the client's PangeaEventTypes.callNotification."""


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
    if required_version != AUDITED_SYNAPSE_VERSION:
        raise ValueError(
            "delayed_push is enabled with "
            f"require_synapse_version={required_version!r}, but this "
            "synapse-pangea-chat commit's patch body was audited for Synapse "
            f"{AUDITED_SYNAPSE_VERSION}. Update the config to confirm the "
            "audited version, or disable delayed_push"
        )
    # synapse.__version__ can carry a git suffix ("1.159.0 (b=main,abc1234)")
    # when the install sits inside a git checkout; compare the base version.
    actual_version = getattr(synapse, "__version__", "") or ""
    if actual_version.split(" ")[0] != AUDITED_SYNAPSE_VERSION:
        raise ValueError(
            "delayed_push is enabled but this synapse-pangea-chat commit was "
            f"audited for Synapse {AUDITED_SYNAPSE_VERSION}; running Synapse "
            f"{actual_version or 'unknown'}"
        )


def _install_delayed_push_patch(config: DelayedPushConfigProtocol) -> None:
    if not getattr(HttpPusher, _PATCHED_ATTR, False):
        setattr(HttpPusher, _ORIGINAL_UNSAFE_PROCESS_ATTR, HttpPusher._unsafe_process)
        setattr(
            HttpPusher, _ORIGINAL_START_PROCESSING_ATTR, HttpPusher._start_processing
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
    """HttpPusher._start_processing that still wakes a held pusher after a
    failed push.

    Upstream drops a wake while a failed push's retry timer is active, and a
    hold keeps its timer in that same ``timed_call`` slot, so upstream would
    take the hold for a retry and drop the wake a ring arrives on. A wake
    during a hold sends nothing unless it finds a ring, so it cannot hammer a
    failing gateway.
    """
    if not self._is_processing and _delayed_push_pending(
        self, _get_delayed_push_config(self)
    ):
        self.hs.run_as_background_process("httppush.process", self._process)
        return

    original_start_processing = getattr(type(self), _ORIGINAL_START_PROCESSING_ATTR)
    original_start_processing(self)


async def _pangea_delayed_push_unsafe_process(self: Any) -> None:
    """HttpPusher._unsafe_process with Pangea active-user deferral.

    This is a private Synapse API monkey patch. It intentionally mirrors Synapse
    v1.159.0's HttpPusher._unsafe_process, adding one pre-_process_one decision
    point that may reschedule the pusher without advancing last_stream_ordering.
    It also fetches again while a queued ring is still ahead of the cursor:
    Synapse fetches 20 push actions at a time, and a hold can queue more than
    that in front of a ring. Upstream's fetch ran once, so its ``break`` on a
    failed push is a ``return`` here.
    """
    # Not importable on every audited Synapse version; only reachable once the
    # exact-version guard has passed.
    from synapse.metrics import SERVER_NAME_LABEL
    from synapse.util.duration import Duration

    config = _get_delayed_push_config(self)
    # Every new notification wakes the pusher, held or not; a held pusher
    # resumes early only for a ring.
    if _delayed_push_pending(self, config):
        released = await _release_hold_for_queued_ring(self)
        # A hold timer that fired during the lookup found this pusher busy and
        # was dropped, so an expired hold is processed now or never.
        if not released and _delayed_push_pending(self, config):
            return

    while True:
        unprocessed = (
            await self.store.get_unread_push_actions_for_user_in_range_for_http(
                self.user_id, self.last_stream_ordering, self.max_stream_ordering
            )
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
                httppusher.http_push_processed_counter.labels(
                    **{SERVER_NAME_LABEL: self.server_name}
                ).inc()
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
                httppusher.http_push_failed_counter.labels(
                    **{SERVER_NAME_LABEL: self.server_name}
                ).inc()
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
                    self.timed_call = self.hs.get_clock().call_later(
                        Duration(seconds=self.backoff_delay),
                        self.on_timer,
                    )
                    self.backoff_delay = min(
                        self.backoff_delay * 2, self.MAX_BACKOFF_SEC
                    )
                    return

        queued_ring_stream_ordering = _queued_ring_stream_ordering(self)
        if (
            not unprocessed
            or queued_ring_stream_ordering is None
            or queued_ring_stream_ordering <= self.last_stream_ordering
        ):
            return


async def _should_defer_push_action(self: Any, push_action: Any) -> bool:
    config = _get_delayed_push_config(self)
    if config is None or not config.delayed_push_enabled:
        return False

    if "notify" not in push_action.actions:
        return False

    queued_ring_stream_ordering = _queued_ring_stream_ordering(self)
    if (
        queued_ring_stream_ordering is not None
        and push_action.stream_ordering < queued_ring_stream_ordering
    ):
        _log_sent_ahead_of_ring(self, push_action, queued_ring_stream_ordering)
        _clear_delayed_push_state(self)
        return False

    event = await self.store.get_event(push_action.event_id, allow_none=True)
    if event is None:
        return False

    if event.type == CALL_RING_EVENT_TYPE:
        logger.info(
            "Pangea delayed push sending ring %s for user %s immediately",
            push_action.event_id,
            self.user_id,
        )
        _clear_delayed_push_state(self)
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

    if not await _user_is_online(self):
        logger.info(
            "Pangea delayed push sending event %s for user %s because user is not "
            "online",
            push_action.event_id,
            self.user_id,
        )
        _clear_delayed_push_state(self)
        return False

    queued_ring_stream_ordering = await _find_queued_ring(
        self, after_stream_ordering=push_action.stream_ordering
    )
    if queued_ring_stream_ordering is not None:
        _log_sent_ahead_of_ring(self, push_action, queued_ring_stream_ordering)
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


async def _user_is_online(self: Any) -> bool:
    server_config = getattr(getattr(self.hs, "config", None), "server", None)
    if getattr(server_config, "presence_enabled", True) is False:
        return False
    if getattr(server_config, "track_presence", True) is False:
        return False

    presence_handler = self.hs.get_presence_handler()
    state = await presence_handler.current_state_for_user(self.user_id)
    return getattr(state, "state", None) == "online"


def _queued_ring_stream_ordering(self: Any) -> int | None:
    return getattr(self, "_pangea_delayed_push_queued_ring_stream_ordering", None)


async def _find_queued_ring(self: Any, after_stream_ordering: int) -> int | None:
    """The stream ordering of the newest ring queued for this pusher after
    ``after_stream_ordering``, if any, remembered until the cursor passes it.

    Bounded by the event being decided rather than the cursor: Synapse returns
    no unread push action between the two, so a ring there was already read on
    another device.

    Reads Synapse's tables directly: its own push-action query carries no event
    type and returns 20 rows at a time, fewer than a hold can queue in front of
    a ring.
    """

    def newest_queued_ring_txn(txn: LoggingTransaction) -> int | None:
        txn.execute(
            """
            SELECT MAX(ep.stream_ordering)
            FROM event_push_actions AS ep
            JOIN events AS e ON e.event_id = ep.event_id
            WHERE ep.user_id = ?
                AND ep.stream_ordering > ?
                AND ep.stream_ordering <= ?
                AND ep.notif = 1
                AND e.type = ?
            """,
            (
                self.user_id,
                after_stream_ordering,
                self.max_stream_ordering,
                CALL_RING_EVENT_TYPE,
            ),
        )
        row = txn.fetchone()
        return row[0] if row else None

    queued_ring_stream_ordering = await self.store.db_pool.runInteraction(
        "pangea_delayed_push_newest_queued_ring", newest_queued_ring_txn
    )
    if queued_ring_stream_ordering is not None:
        self._pangea_delayed_push_queued_ring_stream_ordering = (
            queued_ring_stream_ordering
        )
    return queued_ring_stream_ordering


async def _release_hold_for_queued_ring(self: Any) -> bool:
    """Ends the pending hold when a ring is queued behind the held event.

    A failed lookup also ends it, so the held event is decided again and sends
    normally rather than staying held without the ring check.
    """
    try:
        queued_ring_stream_ordering = await _find_queued_ring(
            self, after_stream_ordering=self._pangea_delayed_push_stream_ordering
        )
    except Exception:
        logger.exception(
            "Pangea delayed push could not look for a queued ring for user %s; "
            "releasing the hold on event %s",
            self.user_id,
            getattr(self, "_pangea_delayed_push_event_id", None),
        )
    else:
        if queued_ring_stream_ordering is None:
            return False
        logger.info(
            "Pangea delayed push releasing the hold on event %s for user %s: "
            "ring at stream_ordering %s is queued behind it",
            getattr(self, "_pangea_delayed_push_event_id", None),
            self.user_id,
            queued_ring_stream_ordering,
        )

    _cancel_existing_timed_call(self)
    _clear_delayed_push_state(self)
    return True


def _log_sent_ahead_of_ring(
    self: Any, push_action: Any, queued_ring_stream_ordering: int
) -> None:
    logger.info(
        "Pangea delayed push sending event %s for user %s because a ring at "
        "stream_ordering %s is queued behind it",
        push_action.event_id,
        self.user_id,
        queued_ring_stream_ordering,
    )


def _schedule_delayed_push(
    self: Any,
    push_action: Any,
    config: DelayedPushConfigProtocol | None,
) -> None:
    if config is None:
        return

    from synapse.util.duration import Duration

    _cancel_existing_timed_call(self)
    self._pangea_delayed_push_event_id = push_action.event_id
    self._pangea_delayed_push_stream_ordering = push_action.stream_ordering
    self._pangea_delayed_push_until_ms = (
        self.clock.time_msec() + config.delayed_push_delay_ms
    )
    self.timed_call = self.hs.get_clock().call_later(
        Duration(milliseconds=config.delayed_push_delay_ms),
        self.on_timer,
    )


def _cancel_existing_timed_call(self: Any) -> None:
    timed_call = getattr(self, "timed_call", None)
    if timed_call is None:
        return

    try:
        timed_call.cancel()
    # Synapse's own HttpPusher.on_stop absorbs the same race the same way.
    # silent-ok: the timer fired or was cancelled already
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
