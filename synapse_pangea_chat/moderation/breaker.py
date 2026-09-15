"""A circuit breaker for the choreo moderation endpoint.

Hand-rolled, and the alternative was considered rather than dismissed: no
Twisted-Deferred-native breaker library exists, and `pybreaker`'s async
support is Tornado-only with no Deferred path at all. A runtime dependency
that would have to be driven synchronously from inside a coroutine, for a
state machine this size, is not a trade worth making.

**What opens it, and what deliberately does not.**

The endpoint does not fail the way a transport breaker expects. The choreo
handler catches `Exception` and answers **HTTP 200 with `evaluated: false`**,
so a provider outage looks, to the socket, exactly like a healthy server. A
breaker that counted only transport errors would sit closed through the whole
outage. So `evaluated: false` is a failure here, and the half-open probe only
counts as recovery when it comes back `evaluated: true` - otherwise the
breaker "recovers" into an ongoing outage and starts paying for calls that
answer nothing.

A 4xx that is not 429 is the opposite case. A bad or expired service-account
token returns 401 on every request forever. Opening on that would disable
moderation permanently while the breaker's own state gauge read "open", which
looks like an upstream problem and is not one. So a config error is counted,
logged once per cooldown rather than once per request, and never opens
anything; the operator sees a steady quiet signal and the checks keep being
attempted, because the moment the token is fixed they must start working
again with no further intervention.

**The breaker fails OPEN, not closed.** When it refuses a job the message is
not checked and is not held: moderation being unavailable must never be a
reason a send is blocked or delayed. The refusal is counted, and the count is
the only thing standing between "we are shedding" and "we are fine".
"""

from typing import Any, Optional

from synapse_pangea_chat.moderation import metrics
from synapse_pangea_chat.moderation.log_safety import scrubbing_logger

logger = scrubbing_logger("synapse.modules.synapse_pangea_chat.moderation.breaker")

BREAKER_CLOSED = "closed"
BREAKER_OPEN = "open"
BREAKER_HALF_OPEN = "half_open"

# The value `check()` returns when the breaker is open, and when it is
# half-open with its one probe already outstanding. Both are drop causes in
# `metrics.DROP_CAUSES`, so a caller can pass either straight to `record_drop`.
REFUSED_OPEN = "breaker_open"
REFUSED_PROBE_BUSY = "breaker_probe_busy"


class CircuitBreaker:
    """Closed / open / half-open, driven by consecutive failures.

    Not thread-safe and not meant to be: it lives on the reactor thread with
    everything else in this module, and a lock would only hide a caller that
    had wandered off it.
    """

    def __init__(
        self,
        *,
        clock: Any,
        failure_threshold: int,
        cooldown_seconds: float,
        max_cooldown_seconds: float,
        name: str = "choreo",
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")
        if max_cooldown_seconds < cooldown_seconds:
            raise ValueError("max_cooldown_seconds must be at least cooldown_seconds")
        self._clock = clock
        self._name = name
        self._failure_threshold = failure_threshold
        self._base_cooldown = float(cooldown_seconds)
        self._max_cooldown = float(max_cooldown_seconds)

        self._state = BREAKER_CLOSED
        self._consecutive_failures = 0
        self._cooldown = self._base_cooldown
        self._open_until = 0.0
        self._probe_outstanding = False
        self._config_error_logged_at: Optional[float] = None
        metrics.set_breaker_state(self._state)

    @property
    def state(self) -> str:
        return self._state

    def check(self) -> Optional[str]:
        """``None`` when the caller may make its request, else a drop cause.

        Calling this is what moves OPEN to HALF_OPEN: there is no timer, so
        there is nothing to leak, nothing to cancel on shutdown, and no way
        for the breaker to change state while nothing is asking it to.
        """
        if self._state == BREAKER_CLOSED:
            return None

        if self._state == BREAKER_OPEN:
            if self._clock.time() < self._open_until:
                return REFUSED_OPEN
            self._set_state(BREAKER_HALF_OPEN)
            self._probe_outstanding = True
            return None

        # HALF_OPEN: exactly one request is in flight as the probe. A second
        # arrival is refused rather than becoming a second probe, or the
        # "one request" the half-open state promises becomes "as many as
        # arrive in the same tick", which against a dead provider is the
        # stampede the breaker exists to prevent.
        if self._probe_outstanding:
            return REFUSED_PROBE_BUSY
        self._probe_outstanding = True
        return None

    def record_success(self) -> None:
        """A call that produced a real verdict.

        The caller decides what "real" means - specifically, a 200 carrying
        `evaluated: false` is NOT a success, and is reported through
        `record_failure`.
        """
        self._consecutive_failures = 0
        self._probe_outstanding = False
        self._cooldown = self._base_cooldown
        if self._state != BREAKER_CLOSED:
            logger.info("moderation breaker %s closed", self._name)
        self._set_state(BREAKER_CLOSED)

    def record_failure(self) -> None:
        """A transport error, a timeout, a 5xx, a 429, or `evaluated: false`."""
        self._probe_outstanding = False
        if self._state == BREAKER_HALF_OPEN:
            # The probe failed. Reopen, and wait longer this time, so a
            # provider that is down for an hour is not probed 120 times.
            self._cooldown = min(self._cooldown * 2, self._max_cooldown)
            self._open()
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._open()

    def record_config_error(self) -> bool:
        """A 4xx that is not 429. Never opens the breaker.

        Returns whether the caller should log this one: true at most once per
        cooldown, so a wrong token produces a steady quiet signal instead of
        an ERROR per message. The failure run is left alone deliberately - a
        bad token arriving between two genuine outages must not clear the
        count and paper the outage over.
        """
        now = self._clock.time()
        last = self._config_error_logged_at
        if last is not None and now - last < self._base_cooldown:
            return False
        self._config_error_logged_at = now
        return True

    def _open(self) -> None:
        self._open_until = self._clock.time() + self._cooldown
        self._consecutive_failures = self._failure_threshold
        if self._state != BREAKER_OPEN:
            logger.warning(
                "moderation breaker %s opened for %.0fs; messages will go "
                "unchecked until it closes",
                self._name,
                self._cooldown,
            )
        self._set_state(BREAKER_OPEN)

    def _set_state(self, state: str) -> None:
        self._state = state
        metrics.set_breaker_state(state)
