"""Forwards the dashboard's Matrix inputs to admin-dash-api, losslessly.

Design: ``.github/instructions/analytics-forward.instructions.md``.

Constructed only when the ``analytics_forward`` block (ingest URL and shared
secret) is configured; construction is what registers the callbacks and
endpoints, so a deployment without the block has none.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Tuple

from synapse_pangea_chat.analytics_forward import metrics
from synapse_pangea_chat.analytics_forward.config import (
    CONFIG_KEY,
    AnalyticsForwardConfig,
    parse_analytics_forward_config,
)
from synapse_pangea_chat.analytics_forward.consent import ConsentAcceptor
from synapse_pangea_chat.analytics_forward.forwarder import (
    Forwarder,
    make_http_poster,
)
from synapse_pangea_chat.analytics_forward.resources import (
    HEAD_PATH,
    STATE_PATH,
    AnalyticsState,
    ForwardHead,
)
from synapse_pangea_chat.analytics_forward.store import ForwardStore
from synapse_pangea_chat.analytics_forward.wire import FORWARDED_TYPES

__all__ = [
    "AnalyticsForward",
    "AnalyticsForwardConfig",
    "CONFIG_KEY",
    "parse_analytics_forward_config",
]

logger = logging.getLogger("synapse.modules.synapse_pangea_chat.analytics_forward")

#: How often the pending-invite scan runs after the one at startup.
PENDING_INVITE_SCAN_SECONDS = 24 * 60 * 60


class AnalyticsForward:
    def __init__(self, api: Any, config: AnalyticsForwardConfig) -> None:
        self._api = api
        homeserver = api._hs
        clock = homeserver.get_clock()
        self._store = ForwardStore(homeserver)

        def clock_ms() -> int:
            return int(clock.time_msec())

        # Endpoints and the profile callback on every instance: a profile
        # change is handled wherever it lands, and any instance may serve the
        # endpoints.
        api.register_web_resource(
            path=HEAD_PATH, resource=ForwardHead(config.secret, self._store, clock_ms)
        )
        api.register_web_resource(
            path=STATE_PATH,
            resource=AnalyticsState(
                config.secret, self._store, homeserver.get_storage_controllers().state
            ),
        )
        self._clock_ms = clock_ms
        api.register_third_party_rules_callbacks(
            on_profile_update=self.on_profile_update
        )

        self._forwarder: Optional[Forwarder] = None
        # The loop on the one instance that runs background tasks, as Tier-2
        # moderation does: on_new_event fires on every worker subscribed to
        # the events stream, and N loops would send everything N times.
        if not api.should_run_background_tasks():
            metrics.ACTIVE.set(0)
            return
        self._forwarder = Forwarder(
            store=self._store,
            post=make_http_poster(api.http_client, config.url, config.secret),
            consent=ConsentAcceptor(api),
            clock_ms=clock_ms,
            sleep=api.sleep,
            run_in_background=lambda f: api.run_as_background_process(
                "pangea_analytics_forward", f
            ),
            batch_max=config.batch_max,
            poll_seconds=config.poll_seconds,
            start_position=config.start_position,
        )
        api.register_third_party_rules_callbacks(on_new_event=self.on_new_event)
        api.delayed_background_call(
            0, self._start, desc="pangea_analytics_forward_start"
        )

    async def _start(self) -> None:
        assert self._forwarder is not None
        delay = 1.0
        while True:
            try:
                await self._forwarder.start()
                break
            except Exception:  # silent-ok: logged and retried until it starts
                logger.exception("analytics_forward: start failed; retrying")
                await self._api.sleep(delay)
                delay = min(delay * 2, 60.0)
        # The heartbeat and the missed-wake-up safety net in one timer.
        self._api.looping_background_call(
            self._poll,
            self._forwarder.poll_ms,
            desc="pangea_analytics_forward_poll",
        )
        self._api.looping_background_call(
            self._forwarder.scan_pending_invites,
            PENDING_INVITE_SCAN_SECONDS * 1000,
            desc="pangea_analytics_forward_pending_invites",
        )

    async def _poll(self) -> None:
        # Async because the timer awaits what it calls.
        if self._forwarder is not None:
            self._forwarder.wake()

    async def on_new_event(
        self, event: Any, _state: Mapping[Tuple[str, str], Any]
    ) -> None:
        """A wake-up only: the loop reads the event from the stream itself."""
        if self._forwarder is not None and event.type in FORWARDED_TYPES:
            self._forwarder.wake()

    async def on_profile_update(
        self, user_id: str, new_profile: Any, by_admin: bool, deactivation: bool
    ) -> None:
        if not self._api.is_mine(user_id):
            return
        try:
            await self._store.mark_profile_dirty(user_id, self._clock_ms())
        except Exception:  # silent-ok: logged; the nightly snapshot repairs it
            # Synapse calls this after the profile write, in its own
            # transaction, so a failure here cannot undo the change; the
            # snapshot is the bound (the plan's accepted limitation).
            logger.exception("analytics_forward: profile outbox write failed")
