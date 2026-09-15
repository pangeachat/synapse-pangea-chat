"""Tier-2 transport, queue, worker pool and circuit breaker.

The tests here drive the real machinery rather than a mock of it. Two choices
carry that:

- **The worker loops are started through the real
  `run_as_background_process`**, not `asyncio`. A worker parks on a Deferred
  that the reactor fires later, and a Deferred pending across an `await`
  cannot be driven by an asyncio task runner at all - so a test that used
  `IsolatedAsyncioTestCase` could only ever exercise code paths that never
  park, which is every path this chunk exists to get right. Driving through
  Synapse's own runner also means Synapse's own logcontext assertions run in
  these tests, and `_assert_no_logcontext_errors` reads them.
- **The clock is a fake with Synapse's `Clock` surface**, so a scheduled
  wakeup is a value the test steps rather than a race it waits on. Nothing
  here sleeps.
"""

import logging
import unittest
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Tuple
from unittest.mock import patch

from synapse.logging.context import (
    SENTINEL_CONTEXT,
    LoggingContext,
    current_context,
    make_deferred_yieldable,
)
from twisted.internet import defer
from twisted.python.failure import Failure

from synapse_pangea_chat.moderation import metrics as mod_metrics
from synapse_pangea_chat.moderation.breaker import (
    BREAKER_CLOSED,
    BREAKER_HALF_OPEN,
    BREAKER_OPEN,
    CircuitBreaker,
)
from synapse_pangea_chat.moderation.choreo_client import (
    KIND_CONFIG_ERROR,
    KIND_DECODE,
    KIND_RATE_LIMITED,
    KIND_SERVER_ERROR,
    KIND_SHAPE,
    KIND_TIMEOUT,
    KIND_TRANSPORT,
    ModerationCheckError,
)
from synapse_pangea_chat.moderation.compat import (
    _SecondsInterval,
    background_process_args,
)

from .moderation_doubles import MetricReader

# The markers Synapse emits when a logcontext is mishandled. `logcontext_error`
# routes all of them through `synapse.logging.context` at WARNING, and
# `background_process_metrics` adds the fourth. A test that asserts only on
# behaviour would pass while leaking, which is the failure mode commit 33f7ead
# was written about.
# `Re-starting finished log context` is absent for the reason measured in
# tests/test_logcontext_e2e.py: Synapse 1.159 emits it from its own event-fetch
# and send paths under whatever context is current, moderation disabled or not.
# The three below are zero on a stock homeserver under the same traffic.
LEAK_MARKERS = (
    "Expected logging context",
    "Background process re-entered without a proc",
    "Looping call died",
)


def _fire_as_synapse_would(callback: Any, args: Any, kwargs: Any) -> None:
    """Run a delayed call the way `synapse.util.Clock.call_later` runs it.

    Not a detail. Synapse does NOT call the callback under the sentinel
    context - it wraps it in `PreserveLoggingContext(LoggingContext("call_later"))`,
    so a callback that hands work to somebody else does so with a live
    context current. A fake that fired bare, under the sentinel, hid a real
    logcontext defect in the worker wakeup that the end-to-end test then
    found against a real homeserver. The fake now does what the real one does.
    """
    from synapse.logging.context import LoggingContext, PreserveLoggingContext

    with PreserveLoggingContext(
        LoggingContext(name="call_later", server_name="example.org")
    ):
        callback(*args, **kwargs)


class _FakeDelayedCall:
    def __init__(self, clock: "FakeClock", when: float, seq: int) -> None:
        self._clock = clock
        self.when = when
        self.seq = seq
        self.cancelled = False

    def active(self) -> bool:
        return not self.cancelled and self.seq in self._clock.pending_ids

    def cancel(self) -> None:
        self.cancelled = True
        self._clock.cancel(self.seq)


class FakeClock:
    """Synapse `Clock`, reduced to the surface the dispatcher and client use.

    `call_later` does NOT run its callback inline - that is the whole point of
    ADR-5a's reactor boundary, and a fake that ran it inline would make the
    producer/consumer handoff untestable by hiding the very thing under test.
    """

    def __init__(self) -> None:
        self._now = 1000.0
        self._pending: List[
            Tuple[float, int, Callable[..., Any], tuple, dict, bool]
        ] = []
        self._seq = 0
        self.shutdown = False
        self.looping: List[Tuple[Callable[..., Any], float, tuple]] = []

    @property
    def pending_ids(self) -> set:
        return {entry[1] for entry in self._pending}

    def time(self) -> float:
        return self._now

    def call_later(
        self, delay: Any, callback: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> _FakeDelayedCall:
        if self.shutdown:
            raise Exception("Cannot start delayed call. Clock has been shutdown")
        return self._schedule(delay, callback, args, kwargs, wrapped=True)

    def reactor_call_later(
        self, delay: float, callback: Callable[..., Any], *args: Any
    ) -> "_FakeDelayedCall":
        """`reactor.callLater`, which does NOT wrap the callback in a
        logcontext the way `synapse.util.Clock.call_later` does."""
        return self._schedule(delay, callback, args, {}, wrapped=False)

    def _schedule(
        self, delay: Any, callback: Any, args: Any, kwargs: Any, *, wrapped: bool
    ) -> "_FakeDelayedCall":
        self._seq += 1
        when = self._now + float(delay)
        self._pending.append((when, self._seq, callback, args, kwargs, wrapped))
        return _FakeDelayedCall(self, when, self._seq)

    def looping_call(
        self, f: Callable[..., Any], interval: Any, *args: Any, **kwargs: Any
    ) -> Any:
        self.looping.append((f, float(interval), args))
        return object()

    def cancel(self, seq: int) -> None:
        self._pending = [entry for entry in self._pending if entry[1] != seq]

    def run_pending(self, limit: int = 200) -> int:
        """Fire every call whose deadline has passed, oldest first."""
        fired = 0
        for _ in range(limit):
            due = [entry for entry in self._pending if entry[0] <= self._now]
            if not due:
                break
            due.sort(key=lambda entry: (entry[0], entry[1]))
            entry = due[0]
            self._pending.remove(entry)
            _when, _seq, callback, args, kwargs, wrapped = entry
            if wrapped:
                _fire_as_synapse_would(callback, args, kwargs)
            else:
                callback(*args, **kwargs)
            fired += 1
        return fired

    def advance(self, seconds: float) -> None:
        self._now += seconds
        self.run_pending()

    def shut_down(self) -> None:
        """What `synapse.util.Clock.shutdown()` does, which is not just
        refusing new calls: it CANCELS every delayed call it is tracking."""
        self.shutdown = True
        self._pending = []

    def fire_looping(self) -> None:
        for f, _interval, args in list(self.looping):
            f(*args)


class _LogcontextWatch:
    """Collects the warnings Synapse emits when a logcontext is mishandled."""

    def __init__(self) -> None:
        self.records: List[str] = []
        self._handler = _CollectingHandler(self.records)
        self._loggers = [
            logging.getLogger("synapse.logging.context"),
            logging.getLogger("synapse.metrics.background_process_metrics"),
        ]

    def __enter__(self) -> "_LogcontextWatch":
        for logger in self._loggers:
            logger.addHandler(self._handler)
            logger.setLevel(logging.WARNING)
        return self

    def __exit__(self, *_exc: Any) -> None:
        for logger in self._loggers:
            logger.removeHandler(self._handler)

    @property
    def leaks(self) -> List[str]:
        return [
            message
            for message in self.records
            if any(marker in message for marker in LEAK_MARKERS)
        ]


class _CollectingHandler(logging.Handler):
    def __init__(self, sink: List[str]) -> None:
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self._sink.append(record.getMessage())


class BreakerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.breaker = CircuitBreaker(
            clock=self.clock,
            failure_threshold=3,
            cooldown_seconds=30.0,
            max_cooldown_seconds=120.0,
        )

    # Every report has to carry the ticket its admission handed out, so the
    # helpers below keep the last one. A test that passed the wrong ticket
    # would exercise the stale-report path by accident and prove nothing about
    # the state machine.
    def _admit(self) -> Any:
        refusal, ticket = self.breaker.check()
        self._ticket = ticket
        return refusal

    def _refusal(self) -> Any:
        refusal, _ticket = self.breaker.check()
        return refusal

    def _fail(self) -> None:
        self.breaker.record_failure(self._current_ticket())

    def _succeed(self) -> None:
        self.breaker.record_success(self._current_ticket())

    def _config_error(self) -> bool:
        return self.breaker.record_config_error(self._current_ticket())

    def _current_ticket(self) -> Any:
        # A report always follows its own admission, so the ticket a test
        # reports with is the breaker's current one unless the test has gone
        # out of its way to keep an old one.
        ticket = getattr(self, "_ticket", None)
        return ticket if ticket is not None else self.breaker._generation

    def test_closed_admits_and_stays_closed_under_the_threshold(self) -> None:
        self.assertIsNone(self._admit())
        self._fail()
        self._fail()
        self.assertEqual(self.breaker.state, BREAKER_CLOSED)
        self.assertIsNone(self._admit())

    def test_a_success_resets_the_consecutive_count(self) -> None:
        self._fail()
        self._fail()
        self._succeed()
        self._fail()
        self._fail()
        self.assertEqual(self.breaker.state, BREAKER_CLOSED)

    def test_consecutive_failures_open_it_and_it_then_refuses(self) -> None:
        for _ in range(3):
            self._fail()
        self.assertEqual(self.breaker.state, BREAKER_OPEN)
        self.assertEqual(self._refusal(), "breaker_open")

    def test_cooldown_admits_exactly_one_probe(self) -> None:
        for _ in range(3):
            self._fail()
        self.clock.advance(29.0)
        self.assertEqual(self._refusal(), "breaker_open")
        self.clock.advance(2.0)
        self.assertIsNone(self._admit())
        self.assertEqual(self.breaker.state, BREAKER_HALF_OPEN)
        # The latch: a second job arriving while the probe is outstanding is
        # refused rather than becoming a second probe.
        self.assertEqual(self._refusal(), "breaker_probe_busy")

    def test_probe_success_closes_and_resets_the_cooldown(self) -> None:
        for _ in range(3):
            self._fail()
        self.clock.advance(31.0)
        self.assertIsNone(self._admit())
        self._succeed()
        self.assertEqual(self.breaker.state, BREAKER_CLOSED)
        self.assertIsNone(self._admit())
        # Cooldown is back to the base value, not the doubled one.
        for _ in range(3):
            self._fail()
        self.clock.advance(31.0)
        self.assertIsNone(self._admit())

    def test_probe_failure_reopens_with_a_doubled_cooldown(self) -> None:
        for _ in range(3):
            self._fail()
        self.clock.advance(31.0)
        self.assertIsNone(self._admit())
        self._fail()
        self.assertEqual(self.breaker.state, BREAKER_OPEN)
        # 60s now, not 30s: at 31s past the reopen it is still refusing.
        self.clock.advance(31.0)
        self.assertEqual(self._refusal(), "breaker_open")
        self.clock.advance(30.0)
        self.assertIsNone(self._admit())

    def test_cooldown_is_capped(self) -> None:
        for _ in range(3):
            self._fail()
        for _ in range(8):
            self.clock.advance(1000.0)
            self.assertIsNone(self._admit())
            self._fail()
        self.clock.advance(121.0)
        self.assertIsNone(self._admit(), "cooldown grew past its cap")

    def test_config_error_never_opens_it(self) -> None:
        for _ in range(50):
            self._config_error()
        self.assertEqual(self.breaker.state, BREAKER_CLOSED)
        self.assertIsNone(self._admit())

    def test_config_error_logging_is_rate_limited_to_one_per_cooldown(self) -> None:
        self.assertTrue(self._config_error())
        self.assertFalse(self._config_error())
        self.clock.advance(31.0)
        self.assertTrue(self._config_error())

    def test_a_config_error_does_not_reset_a_real_failure_run(self) -> None:
        # A bad token arriving between two genuine outages must neither open
        # the breaker nor paper over the outage by clearing the count.
        self._fail()
        self._fail()
        self._config_error()
        self._fail()
        self.assertEqual(self.breaker.state, BREAKER_OPEN)

    def test_a_config_error_on_the_probe_does_not_wedge_half_open(self) -> None:
        # The probe latch is released by whichever outcome the caller reports.
        # A config error is a third outcome - it says nothing about the
        # provider, so it must not count as a probe result - and a version
        # that simply did not touch the latch left the breaker in HALF_OPEN
        # refusing every later check as `breaker_probe_busy` FOREVER, with
        # fixing the token no help at all.
        for _ in range(3):
            self._fail()
        self.clock.advance(31.0)
        self.assertIsNone(self._admit())
        self.assertEqual(self.breaker.state, BREAKER_HALF_OPEN)
        self._config_error()
        self.assertEqual(self.breaker.state, BREAKER_OPEN)
        # And with the SAME cooldown, not a doubled one: nothing was learned
        # about the provider, so there is nothing to back off from.
        self.clock.advance(31.0)
        self.assertIsNone(self._admit())

    def test_a_released_probe_can_be_retried(self) -> None:
        # The cancellation path: admitted as the probe, then never reports.
        for _ in range(3):
            self._fail()
        self.clock.advance(31.0)
        _refusal, ticket = self.breaker.check()
        self.assertEqual(self.breaker.state, BREAKER_HALF_OPEN)
        self.breaker.release(ticket)
        self.assertEqual(self.breaker.state, BREAKER_OPEN)
        self.clock.advance(31.0)
        self.assertIsNone(self._admit(), "the probe permit was never given back")

    def test_release_after_an_outcome_changes_nothing(self) -> None:
        for _ in range(3):
            self._fail()
        self.clock.advance(31.0)
        _refusal, ticket = self.breaker.check()
        self.breaker.record_success(ticket)
        self.assertEqual(self.breaker.state, BREAKER_CLOSED)
        self.breaker.release(ticket)
        self.assertEqual(
            self.breaker.state,
            BREAKER_CLOSED,
            "release reopened a breaker that had already recovered",
        )

    def test_a_stale_success_cannot_close_the_breaker(self) -> None:
        # Several requests are in flight at once while CLOSED. Enough of them
        # fail to open the breaker; a straggler admitted BEFORE any of that
        # then comes back successful. Letting it close the breaker skips the
        # cooldown and the probe entirely, so the breaker flaps instead of
        # shedding.
        _refusal, stale = self.breaker.check()
        for _ in range(3):
            self._fail()
        self.assertEqual(self.breaker.state, BREAKER_OPEN)
        self.breaker.record_success(stale)
        self.assertEqual(self.breaker.state, BREAKER_OPEN)
        self.assertEqual(self._refusal(), "breaker_open")

    def test_a_stale_failure_does_not_extend_the_cooldown(self) -> None:
        # The other half: the failures that opened the breaker are already
        # counted, and counting a straggler from the same outage again would
        # double the cooldown for a probe that never ran.
        _refusal, stale = self.breaker.check()
        for _ in range(3):
            self._fail()
        self.breaker.record_failure(stale)
        self.clock.advance(31.0)
        self.assertIsNone(self._admit(), "the cooldown was extended by a straggler")

    def test_state_is_published_as_a_gauge(self) -> None:
        reader = MetricReader()
        self.assertEqual(
            reader.value("pangea_moderation_tier2_breaker_state"),
            float(mod_metrics.BREAKER_STATE_VALUES[BREAKER_CLOSED]),
        )
        for _ in range(3):
            self._fail()
        self.assertEqual(
            reader.value("pangea_moderation_tier2_breaker_state"),
            float(mod_metrics.BREAKER_STATE_VALUES[BREAKER_OPEN]),
        )


class MetricsTestCase(unittest.TestCase):
    def test_drop_causes_are_a_closed_set(self) -> None:
        # Cardinality is bounded by construction, so an unknown cause is a
        # programming error rather than a new time series.
        with self.assertRaises(ValueError):
            mod_metrics.record_drop("!room:example.org")

    def test_every_declared_cause_is_accepted(self) -> None:
        reader = MetricReader()
        for cause in sorted(mod_metrics.DROP_CAUSES):
            reader.snapshot("pangea_moderation_tier2_dropped_total", cause=cause)
            mod_metrics.record_drop(cause)
            self.assertEqual(
                reader.delta("pangea_moderation_tier2_dropped_total", cause=cause),
                1.0,
            )

    def test_collectors_are_reused_rather_than_re_registered(self) -> None:
        # `importlib.reload` re-executes the registrations; a bare
        # `Counter(...)` raises `Duplicated timeseries` and takes the module
        # down with it.
        import importlib

        reloaded = importlib.reload(mod_metrics)
        self.assertIsNotNone(reloaded.TIER2_DROPPED)


def start_worker(coroutine_function: Any, *args: Any) -> "defer.Deferred":
    """Start a coroutine exactly as production starts a worker."""
    from synapse.metrics.background_process_metrics import run_as_background_process

    class _Hs:
        hostname = "example.org"

    return run_as_background_process(
        *background_process_args(_Hs(), "test_worker", coroutine_function),
        *args,
    )


class SecondsIntervalTestCase(unittest.TestCase):
    def test_satisfies_both_clock_contracts(self) -> None:
        interval = _SecondsInterval(1.5)
        self.assertEqual(interval.as_secs(), 1.5)
        self.assertEqual(interval.as_millis(), 1500)
        self.assertEqual(float(interval), 1.5)


class LogcontextHandoffTestCase(unittest.TestCase):
    """The handoff primitive itself, before anything is built on it.

    If firing a parked worker's Deferred from the reactor does not return the
    caller to the sentinel context, every test below it is measuring the wrong
    thing.
    """

    def test_a_parked_coroutine_resumes_in_its_own_context(self) -> None:
        waiter: "defer.Deferred[None]" = defer.Deferred()
        seen: List[str] = []

        async def worker() -> None:
            seen.append(str(current_context()))
            await make_deferred_yieldable(waiter)
            seen.append(str(current_context()))

        with _LogcontextWatch() as watch:
            start_worker(worker)
            self.assertEqual(len(seen), 1)
            with LoggingContext(name="producer", server_name="example.org"):
                # The producer fires nothing directly; the reactor does.
                self.assertEqual(str(current_context()), "producer")
            waiter.callback(None)
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0], seen[1])
        self.assertEqual(watch.leaks, [], "logcontext leaked across the handoff")
        self.assertIs(current_context(), SENTINEL_CONTEXT)


class _Hs:
    """The homeserver surface the dispatcher reaches through."""

    hostname = "example.org"

    def __init__(self, clock: "FakeClock") -> None:
        self.shutdown_handlers: List[Any] = []
        self.clock = clock
        # Replaced by a test that needs `callLater` to fail. An attribute
        # rather than an override of the method, so a test can swap it without
        # assigning over a method - which is both a type error and a less
        # honest double.
        self.call_later: Callable[..., Any] = clock.reactor_call_later

    def get_reactor(self) -> Any:
        return SimpleNamespace(callLater=self.call_later)

    def register_async_shutdown_handler(
        self, *, phase: str, eventType: str, shutdown_func: Any
    ) -> None:
        assert phase == "before" and eventType == "shutdown"
        self.shutdown_handlers.append(shutdown_func)


class _Handler:
    """A job handler that can be held pending, fail, or hang."""

    def __init__(self) -> None:
        self.started: List[str] = []
        self.finished: List[str] = []
        self.gates: Dict[str, "defer.Deferred[None]"] = {}
        self.hold = False
        self.swallow_cancel = False
        self.raise_for: set = set()

    async def __call__(self, job: Any) -> None:
        self.started.append(job.event_id)
        if self.hold:
            gate: "defer.Deferred[None]" = defer.Deferred()
            self.gates[job.event_id] = gate
            if self.swallow_cancel:
                # What a handler catching `Exception` does, because twisted's
                # `CancelledError` IS an `Exception`.
                try:
                    await make_deferred_yieldable(gate)
                except Exception:
                    gate2: "defer.Deferred[None]" = defer.Deferred()
                    self.gates[job.event_id] = gate2
                    await make_deferred_yieldable(gate2)
            else:
                await make_deferred_yieldable(gate)
        if job.event_id in self.raise_for:
            self.finished.append(job.event_id)
            raise RuntimeError("handler blew up")
        self.finished.append(job.event_id)

    def release(self, event_id: str) -> None:
        self.gates.pop(event_id).callback(None)

    def release_all(self) -> None:
        for event_id in list(self.gates):
            self.release(event_id)


class DispatcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        from synapse_pangea_chat.moderation.dispatch import (
            ModerationJob,
            Tier2Dispatcher,
        )

        self.ModerationJob = ModerationJob
        self.clock = FakeClock()
        self.hs = _Hs(self.clock)
        self.handler = _Handler()
        self.reader = MetricReader()
        self.watch = _LogcontextWatch()
        self.watch.__enter__()
        self.addCleanup(self.watch.__exit__, None, None, None)
        self.dispatcher = Tier2Dispatcher(
            homeserver=self.hs,
            clock=self.clock,
            handler=self.handler,
            workers=2,
            queue_size=3,
            supervisor_interval_seconds=30.0,
            drain_timeout_seconds=5.0,
        )
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        self.handler.release_all()
        self.clock.run_pending()
        self.dispatcher._stopping = True
        self.dispatcher._wake_all()
        self.clock.run_pending()
        self.assertEqual(self.watch.leaks, [], "logcontext leaked")

    def _job(self, event_id: str) -> Any:
        return self.ModerationJob(
            event_id=event_id,
            room_id="!room:example.org",
            sender="@learner:example.org",
            text="text",
            enqueued_at=self.clock.time(),
        )

    def _drain(self) -> None:
        for _ in range(20):
            if not self.clock.run_pending():
                break

    # --- admission and overflow -------------------------------------

    def test_a_full_queue_rejects_the_newest_and_counts_it(self) -> None:
        # CC-1. No workers started, so nothing can drain and the buffer is
        # genuinely full rather than briefly full.
        self.reader.snapshot(
            "pangea_moderation_tier2_dropped_total", cause="queue_full"
        )
        for index in range(3):
            self.assertTrue(self.dispatcher.enqueue(self._job(f"$e{index}")))
        self.assertFalse(self.dispatcher.enqueue(self._job("$overflow")))
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="queue_full"
            ),
            1.0,
            "a dropped message was not counted, so the drop is silent",
        )
        self.assertEqual(self.dispatcher.queue_depth, 3)

    def test_the_queue_never_grows_past_its_capacity(self) -> None:
        for index in range(500):
            self.dispatcher.enqueue(self._job(f"$e{index}"))
        self.assertEqual(self.dispatcher.queue_depth, 3)

    def test_drop_newest_keeps_the_jobs_already_accepted(self) -> None:
        # CC-2. Drop-newest, not drop-oldest: the work already admitted is the
        # work that completes.
        self.dispatcher.start()
        self.handler.hold = True
        self._drain()
        # Two workers park on $e0 and $e1; $e2 and $e3 fill two of the three
        # queue slots, and $e4 fills the last. $e5 onwards meet a full queue.
        for index in range(8):
            self.dispatcher.enqueue(self._job(f"$e{index}"))
            self._drain()
        self.assertEqual(self.dispatcher.queue_depth, 3)
        self.handler.hold = False
        self.handler.release_all()
        self._drain()
        # Set, not sequence: completion ORDER is not the property - which
        # jobs survived is. Asserting the order would fail on a legitimate
        # change to how workers are woken and say nothing about the policy.
        self.assertEqual(
            sorted(self.handler.finished),
            ["$e0", "$e1", "$e2", "$e3", "$e4"],
            "the jobs already accepted are the jobs that completed",
        )

    def test_the_same_event_is_not_queued_twice(self) -> None:
        self.reader.snapshot("pangea_moderation_tier2_dropped_total", cause="duplicate")
        self.assertTrue(self.dispatcher.enqueue(self._job("$same")))
        self.assertFalse(self.dispatcher.enqueue(self._job("$same")))
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="duplicate"
            ),
            1.0,
        )
        self.assertEqual(self.dispatcher.queue_depth, 1)

    def test_a_completed_event_can_be_queued_again(self) -> None:
        # The in-flight set is not a memory of what has been done; holding ids
        # forever would be an unbounded set, and dropping them on completion
        # is what keeps it bounded.
        self.dispatcher.start()
        self._drain()
        self.assertTrue(self.dispatcher.enqueue(self._job("$again")))
        self._drain()
        self.assertEqual(self.handler.finished, ["$again"])
        self.assertTrue(self.dispatcher.enqueue(self._job("$again")))

    def test_enqueue_returns_false_rather_than_raising_when_stopping(self) -> None:
        self.reader.snapshot("pangea_moderation_tier2_dropped_total", cause="stopping")
        self.dispatcher._stopping = True
        self.assertFalse(self.dispatcher.enqueue(self._job("$late")))
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="stopping"
            ),
            1.0,
        )

    def test_a_cancelled_wakeup_does_not_look_like_a_pending_one(self) -> None:
        # `Clock.shutdown()` cancels the delayed calls it is tracking. A
        # dispatcher that remembered "a wakeup is scheduled" as a BOOLEAN then
        # coalesced every later enqueue into a wakeup that would never fire:
        # accepted jobs, every worker parked, nothing pending, nothing
        # counted. The handle knows; a flag cannot.
        self.dispatcher.start()
        self._drain()
        self.assertTrue(self.dispatcher.enqueue(self._job("$a")))
        # $a's wakeup is scheduled but has not fired yet.
        self.clock.shut_down()
        self.reader.snapshot("pangea_moderation_tier2_dropped_total", cause="no_clock")
        self.assertFalse(
            self.dispatcher.enqueue(self._job("$b")),
            "a job was accepted with no wakeup that will ever fire",
        )
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="no_clock"
            ),
            1.0,
        )

    def test_a_job_that_fails_in_the_handler_is_counted(self) -> None:
        # A job that raises before reaching the checker never reaches the
        # checker's outcome counter either, so without this it vanishes:
        # accepted, never checked, and absent from every metric.
        self.dispatcher.start()
        self._drain()
        self.handler.raise_for.add("$boom")
        self.reader.snapshot(
            "pangea_moderation_tier2_dropped_total", cause="handler_error"
        )
        self.dispatcher.enqueue(self._job("$boom"))
        self._drain()
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="handler_error"
            ),
            1.0,
        )

    def test_a_job_cancelled_under_a_worker_is_counted(self) -> None:
        self.dispatcher.start()
        self.handler.hold = True
        self._drain()
        self.dispatcher.enqueue(self._job("$victim"))
        self._drain()
        self.reader.snapshot("pangea_moderation_tier2_dropped_total", cause="cancelled")
        self.dispatcher._kill_worker_for_test(0)
        self._drain()
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="cancelled"
            ),
            1.0,
        )

    def test_a_dead_clock_does_not_leave_a_job_stranded_on_the_queue(self) -> None:
        # `Clock.call_later` raises once the clock is shut down. A job appended
        # before that raise, and left there, is a job nothing will ever wake a
        # worker for - counted as accepted and never checked.
        self.reader.snapshot("pangea_moderation_tier2_dropped_total", cause="no_clock")
        self.dispatcher.start()
        self._drain()
        self.clock.shutdown = True
        self.assertFalse(self.dispatcher.enqueue(self._job("$orphan")))
        self.assertEqual(self.dispatcher.queue_depth, 0)
        self.assertEqual(self.dispatcher.inflight, 0)
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="no_clock"
            ),
            1.0,
        )

    # --- the producer/consumer handoff ------------------------------

    def test_enqueue_runs_no_consumer_work_and_keeps_its_logcontext(self) -> None:
        # CC-12. This is the test that fails on a `DeferredQueue`, whose
        # `put()` resumes a parked consumer INSIDE the producer's frame - so
        # the notifier would run a moderation job's first slice, and the
        # producer's logcontext would be handed to the consumer.
        self.dispatcher.start()
        self._drain()
        self.assertEqual(self.handler.started, [])
        with LoggingContext(name="notifier", server_name="example.org"):
            before = str(current_context())
            self.dispatcher.enqueue(self._job("$e0"))
            self.assertEqual(
                self.handler.started,
                [],
                "enqueue ran consumer work inside the producer's frame",
            )
            self.assertEqual(str(current_context()), before)
        self.assertIs(current_context(), SENTINEL_CONTEXT)
        self._drain()
        self.assertEqual(self.handler.started, ["$e0"])
        self.assertEqual(self.watch.leaks, [])

    def test_workers_are_woken_once_per_available_job(self) -> None:
        # Several jobs arriving before the reactor turns must not wake one
        # worker and strand the rest: the wakeup is coalesced into a single
        # scheduled call, so that call has to wake as many workers as there
        # is work for.
        self.dispatcher.start()
        self.handler.hold = True
        self._drain()
        self.dispatcher.enqueue(self._job("$a"))
        self.dispatcher.enqueue(self._job("$b"))
        self._drain()
        self.assertEqual(sorted(self.handler.started), ["$a", "$b"])

    # --- worker behaviour -------------------------------------------

    def test_a_handler_exception_does_not_kill_the_worker(self) -> None:
        self.dispatcher.start()
        self._drain()
        self.handler.raise_for.add("$boom")
        self.dispatcher.enqueue(self._job("$boom"))
        self._drain()
        self.dispatcher.enqueue(self._job("$after"))
        self._drain()
        self.assertIn("$after", self.handler.finished)
        self.assertEqual(self.dispatcher.inflight, 0)

    def test_the_inflight_set_empties_after_a_mixed_workload(self) -> None:
        # CC-14. An id left behind is not just a leak: it permanently blocks
        # that event from ever being moderated again.
        self.dispatcher.start()
        self._drain()
        self.handler.raise_for.add("$err")
        for event_id in ("$ok", "$err", "$ok2"):
            self.dispatcher.enqueue(self._job(event_id))
            self._drain()
        for index in range(10):
            self.dispatcher.enqueue(self._job(f"$flood{index}"))
        self._drain()
        self.assertEqual(self.dispatcher.inflight, 0)
        self.assertEqual(self.dispatcher.queue_depth, 0)

    def test_the_supervisor_restarts_a_worker_that_died(self) -> None:
        # CC-11. `run_as_background_process` swallows exceptions, so a dead
        # worker is otherwise completely silent.
        self.dispatcher.start()
        self._drain()
        self.assertEqual(self.dispatcher.live_workers, 2)
        self.dispatcher._kill_worker_for_test(0)
        self._drain()
        self.assertEqual(self.dispatcher.live_workers, 1)
        self.reader.snapshot("pangea_moderation_tier2_workers_restarted_total")
        self.clock.fire_looping()
        self._drain()
        self.assertEqual(self.dispatcher.live_workers, 2)
        self.assertEqual(
            self.reader.delta("pangea_moderation_tier2_workers_restarted_total"), 1.0
        )
        self.dispatcher.enqueue(self._job("$after_restart"))
        self._drain()
        self.assertIn("$after_restart", self.handler.finished)

    def test_the_supervisor_never_raises_during_shutdown(self) -> None:
        # A looping call whose function raises is logged "Looping call died"
        # and STOPS FOREVER. During shutdown the clock refuses new calls, so
        # a supervisor that scheduled work unconditionally would do exactly
        # that on the way down.
        self.dispatcher.start()
        self._drain()
        self.dispatcher._stopping = True
        self.clock.shutdown = True
        self.clock.fire_looping()

    # --- shutdown ---------------------------------------------------

    def test_shutdown_is_registered_with_the_homeserver(self) -> None:
        self.dispatcher.start()
        self.assertEqual(len(self.hs.shutdown_handlers), 1)

    def test_shutdown_counts_what_was_still_queued(self) -> None:
        # CC-10. The queue is in memory; a stop loses it. What must not
        # happen is losing it silently.
        self.dispatcher.start()
        self.handler.hold = True
        self._drain()
        # Both workers busy first, so the three that follow are unambiguously
        # queued rather than running.
        for index in range(2):
            self.dispatcher.enqueue(self._job(f"$e{index}"))
            self._drain()
        for index in range(2, 5):
            self.dispatcher.enqueue(self._job(f"$e{index}"))
        self._drain()
        self.assertEqual(self.dispatcher.queue_depth, 3)
        self.reader.snapshot("pangea_moderation_tier2_dropped_total", cause="shutdown")
        drained = start_worker(self.dispatcher.shutdown)
        self._drain()
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="shutdown"
            ),
            3.0,
            "queued work vanished without being counted",
        )
        # The two jobs a worker had already picked up are still running, so
        # the drain has not finished.
        self.assertFalse(drained.called)
        self.handler.release_all()
        self._drain()
        self.assertTrue(drained.called, "the drain never completed")
        self.assertEqual(sorted(self.handler.finished), ["$e0", "$e1"])

    def test_shutdown_does_not_hang_on_work_that_never_finishes(self) -> None:
        self.dispatcher.start()
        self.handler.hold = True
        self._drain()
        self.dispatcher.enqueue(self._job("$stuck"))
        self._drain()
        self.reader.snapshot(
            "pangea_moderation_tier2_dropped_total", cause="drain_timeout"
        )
        drained = start_worker(self.dispatcher.shutdown)
        self._drain()
        self.assertFalse(drained.called)
        self.clock.advance(6.0)
        self.assertTrue(drained.called, "shutdown hung on a job that never finished")
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="drain_timeout"
            ),
            1.0,
            "abandoned work was not counted",
        )

    def test_shutdown_survives_the_clock_being_shut_down(self) -> None:
        # `HomeServer.shutdown()` starts its async shutdown handlers WITHOUT
        # awaiting them and then calls `Clock.shutdown()`, which cancels every
        # delayed call the Clock is tracking. A drain deadline taken from the
        # Clock would be cancelled out from under the drain, leaving it parked
        # and `_running` populated for the life of the process. The deadline
        # is taken from the reactor for exactly this reason.
        self.dispatcher.start()
        self.handler.hold = True
        self._drain()
        self.dispatcher.enqueue(self._job("$stuck"))
        self._drain()
        self.clock.shutdown = True
        drained = start_worker(self.dispatcher.shutdown)
        self._drain()
        self.assertFalse(drained.called)
        self.clock.advance(6.0)
        self.assertTrue(drained.called, "the drain outlived its own deadline")

    def test_shutdown_returns_when_no_timer_can_be_armed_at_all(self) -> None:
        self.dispatcher.start()
        self.handler.hold = True
        self._drain()
        self.dispatcher.enqueue(self._job("$stuck"))
        self._drain()

        def _refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("reactor is stopping")

        self.hs.call_later = _refuse
        drained = start_worker(self.dispatcher.shutdown)
        self.assertTrue(
            drained.called, "an unbounded wait is worse than an abandoned one"
        )

    def test_an_abandoned_job_does_not_act_after_shutdown_returned(self) -> None:
        """Abandoning the ACCOUNTING is not abandoning the WORK.

        The drain wrote the job off, reported it and returned - and then the
        coroutine, which nothing had stopped, woke up and carried on: a
        database read and a redaction sent into a room, after the homeserver
        had been told moderation was done. A shutdown that has returned must
        not be able to change a room.
        """
        self.dispatcher.start()
        self.handler.hold = True
        self._drain()
        self.dispatcher.enqueue(self._job("$stuck"))
        self._drain()
        drained = start_worker(self.dispatcher.shutdown)
        self._drain()
        self.clock.advance(6.0)
        self.assertTrue(drained.called, "the drain never finished")
        self.assertNotIn("$stuck", self.handler.finished)

        # The verdict arrives after the deadline, which is the case the
        # reproduction used: hold a verdict, shut down, advance past the drain
        # deadline, then release it.
        self.handler.release_all()
        self._drain()
        self.assertNotIn(
            "$stuck",
            self.handler.finished,
            "an abandoned job carried on and acted after shutdown returned",
        )

    def test_a_handler_that_swallows_cancellation_is_still_stopped(self) -> None:
        """Cancellation is the mechanism and it is not the guarantee.

        `CancelledError` IS an `Exception`, so a handler with a broad `except`
        can absorb it and carry on - which is exactly what this module's own
        fail-open handlers are written to do. The dispatcher therefore also
        REFUSES work once the drain has ended, and that refusal is what the
        redaction path consults before it sends anything.
        """
        self.dispatcher.start()
        self.handler.hold = True
        self.handler.swallow_cancel = True
        self._drain()
        self.dispatcher.enqueue(self._job("$stubborn"))
        self._drain()
        start_worker(self.dispatcher.shutdown)
        self._drain()
        self.clock.advance(6.0)
        self.assertFalse(
            self.dispatcher.actions_permitted,
            "a drain that has ended still permitted work to act on a room",
        )

    def test_actions_are_permitted_while_a_drain_is_still_running(self) -> None:
        """The refusal is on the END of the drain, not on its start. A job
        that finishes inside the drain window must still be able to redact -
        draining means finishing the work, not abandoning it."""
        self.dispatcher.start()
        self.handler.hold = True
        self._drain()
        self.dispatcher.enqueue(self._job("$inflight"))
        self._drain()
        start_worker(self.dispatcher.shutdown)
        self._drain()
        self.assertTrue(
            self.dispatcher.actions_permitted,
            "a job still inside the drain window was refused",
        )
        self.handler.release_all()
        self._drain()
        self.assertIn("$inflight", self.handler.finished)

    def test_abandoned_work_is_counted_once_and_then_forgotten(self) -> None:
        # Abandoning has to clear the accounting as well as report it. Leaving
        # the ids in `_running` makes a second shutdown wait on jobs that have
        # already been written off, and count them a second time.
        self.dispatcher.start()
        self.handler.hold = True
        self._drain()
        self.dispatcher.enqueue(self._job("$stuck"))
        self._drain()
        self.reader.snapshot(
            "pangea_moderation_tier2_dropped_total", cause="drain_timeout"
        )
        start_worker(self.dispatcher.shutdown)
        self._drain()
        self.clock.advance(6.0)
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="drain_timeout"
            ),
            1.0,
        )
        self.assertEqual(self.dispatcher.inflight, 0)
        second = start_worker(self.dispatcher.shutdown)
        self.clock.advance(6.0)
        self.assertTrue(second.called)
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="drain_timeout"
            ),
            1.0,
            "the same abandoned job was counted twice",
        )

    def test_a_cancelled_shutdown_does_not_leave_its_waiter_behind(self) -> None:
        self.dispatcher.start()
        self.handler.hold = True
        self._drain()
        self.dispatcher.enqueue(self._job("$stuck"))
        self._drain()
        for _ in range(5):
            drained = start_worker(self.dispatcher.shutdown)
            self._drain()
            self.assertFalse(drained.called)
            drained.cancel()
        self.assertEqual(len(self.dispatcher._drain_waiters), 0)

    def test_a_handler_that_swallows_cancellation_does_not_grow_the_pool(
        self,
    ) -> None:
        # The supervisor's liveness signal has to be the worker's own flag and
        # not its Deferred. A handler that catches `Exception` catches
        # twisted's `CancelledError` too, so the coroutine can carry on and
        # park again while its Deferred is already `called` - and a supervisor
        # reading the Deferred would start a replacement every interval, for
        # ever, from a pool that is not actually short of anything.
        self.dispatcher.start()
        self.handler.hold = True
        self.handler.swallow_cancel = True
        self._drain()
        self.dispatcher.enqueue(self._job("$victim"))
        self._drain()
        self.dispatcher._kill_worker_for_test(0)
        self._drain()
        self.handler.release_all()
        self._drain()
        # Measured through the RESTART COUNTER, not through `live_workers`.
        # `live_workers` is derived from the same liveness signal the
        # supervisor uses, so asserting on it would pass whichever signal the
        # supervisor read - including the wrong one. The counter is
        # independent: it says how many replacements were actually started.
        self.reader.snapshot("pangea_moderation_tier2_workers_restarted_total")
        for _ in range(5):
            self.clock.fire_looping()
            self._drain()
        self.assertEqual(
            self.reader.delta("pangea_moderation_tier2_workers_restarted_total"),
            0.0,
            "the supervisor started replacements for a worker that was still "
            "running, so the pool grows by one every interval",
        )

    def test_a_worker_cancelled_mid_job_is_replaced_and_not_duplicated(self) -> None:
        # `_run` catches `Exception`, and twisted's `CancelledError` IS an
        # `Exception`. Swallowing it would leave the coroutine running behind
        # a Deferred that has already fired, so a supervisor reading the
        # Deferred would start a replacement and the pool would grow by one
        # every time - six live consumers from a pool of one.
        self.dispatcher.start()
        self.handler.hold = True
        self._drain()
        self.dispatcher.enqueue(self._job("$victim"))
        self._drain()
        self.assertEqual(self.handler.started, ["$victim"])
        self.assertEqual(self.dispatcher.live_workers, 2)
        self.dispatcher._kill_worker_for_test(0)
        self._drain()
        self.assertEqual(
            self.dispatcher.live_workers,
            1,
            "a cancelled worker carried on, so the supervisor cannot see it",
        )
        self.assertEqual(self.dispatcher.inflight, 0, "the cancelled job leaked")
        for _ in range(4):
            self.clock.fire_looping()
            self._drain()
        self.assertEqual(
            self.dispatcher.live_workers, 2, "the pool grew or failed to recover"
        )

    def test_shutdown_is_idempotent(self) -> None:
        self.dispatcher.start()
        self._drain()
        first = start_worker(self.dispatcher.shutdown)
        self._drain()
        second = start_worker(self.dispatcher.shutdown)
        self._drain()
        self.assertTrue(first.called)
        self.assertTrue(second.called)


class _CountingClient:
    """A `SimpleHttpClient` stand-in that counts calls and can be steered."""

    def __init__(self) -> None:
        self.calls = 0
        self.responses: List[Any] = []
        self.default: Any = {"flagged": False, "categories": [], "evaluated": True}

    def _next(self) -> Any:
        if self.responses:
            return self.responses.pop(0)
        return self.default

    async def moderate(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        outcome = self._next()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class CheckerTestCase(unittest.TestCase):
    """The breaker and the metrics around one moderation call.

    Driven through `ChoreoChecker.check` rather than the breaker alone,
    because the mapping between what the endpoint did and what the breaker is
    told is where the interesting mistakes are - a 401 that opens the breaker
    disables moderation until somebody notices, and a 200 carrying
    `evaluated: false` that does not is a provider outage the breaker sleeps
    through.
    """

    def setUp(self) -> None:
        from synapse_pangea_chat.moderation.choreo_client import ChoreoChecker

        self.clock = FakeClock()
        self.client = _CountingClient()
        self.breaker = CircuitBreaker(
            clock=self.clock,
            failure_threshold=3,
            cooldown_seconds=30.0,
            max_cooldown_seconds=120.0,
        )
        self.reader = MetricReader()
        self.checker = ChoreoChecker(
            agent=object(),
            clock=self.clock,
            base_url="http://choreo.invalid",
            access_token="syt_x",
            breaker=self.breaker,
            timeout_seconds=15.0,
        )
        self._patch = patch(
            "synapse_pangea_chat.moderation.choreo_client.moderate_text",
            self.client.moderate,
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _check(self, text: str = "hello") -> Any:
        result: List[Any] = []
        deferred = defer.ensureDeferred(self.checker.check(text))
        deferred.addBoth(result.append)
        self.assertEqual(len(result), 1, "the check did not complete")
        if isinstance(result[0], Failure):
            result[0].raiseException()
        return result[0]

    def _fail(self, error: Exception, times: int) -> None:
        self.client.responses = [error] * times

    def test_a_clean_verdict_is_returned_and_counted(self) -> None:
        self.reader.snapshot("pangea_moderation_tier2_checks_total", outcome="clean")
        self.assertEqual(self._check()["flagged"], False)
        self.assertEqual(
            self.reader.delta("pangea_moderation_tier2_checks_total", outcome="clean"),
            1.0,
        )

    def test_consecutive_failures_open_the_breaker_and_stop_the_calls(self) -> None:
        # CC-3. "Open" has to mean zero HTTP calls, not a call that is
        # discarded: the point of the breaker is not spending on a dead
        # provider.
        self._fail(ModerationCheckError("down", KIND_SERVER_ERROR), 3)
        for _ in range(3):
            self.assertIsNone(self._check())
        self.assertEqual(self.breaker.state, BREAKER_OPEN)
        calls_before = self.client.calls
        self.reader.snapshot(
            "pangea_moderation_tier2_dropped_total", cause="breaker_open"
        )
        self.assertIsNone(self._check())
        self.assertEqual(self.client.calls, calls_before, "a call was made while open")
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="breaker_open"
            ),
            1.0,
        )

    def test_a_two_hundred_with_no_evaluation_opens_the_breaker(self) -> None:
        # CC-4. The choreo handler catches `Exception` and answers HTTP 200
        # with `evaluated: false`, so a provider outage is invisible to a
        # breaker that counts only transport failures.
        self.client.default = {"flagged": False, "categories": [], "evaluated": False}
        self.reader.snapshot(
            "pangea_moderation_tier2_checks_total", outcome="unevaluated"
        )
        for _ in range(3):
            self.assertIsNone(self._check())
        self.assertEqual(self.breaker.state, BREAKER_OPEN)
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_checks_total", outcome="unevaluated"
            ),
            3.0,
        )

    def test_a_response_with_no_evaluated_key_is_not_a_failure(self) -> None:
        # An endpoint that does not send the key is an older endpoint, not a
        # failing one. Inventing failures from a missing field would open the
        # breaker against a service that was working.
        self.client.default = {"flagged": False, "categories": []}
        for _ in range(5):
            self.assertIsNotNone(self._check())
        self.assertEqual(self.breaker.state, BREAKER_CLOSED)

    def test_a_probe_that_is_not_evaluated_does_not_count_as_recovery(self) -> None:
        # CC-5. Otherwise the breaker "recovers" into an ongoing outage and
        # goes back to paying for calls that answer nothing.
        self._fail(ModerationCheckError("down", KIND_SERVER_ERROR), 3)
        for _ in range(3):
            self._check()
        self.clock.advance(31.0)
        self.client.default = {"flagged": False, "categories": [], "evaluated": False}
        self.assertIsNone(self._check())
        self.assertEqual(self.breaker.state, BREAKER_OPEN)

    def test_only_one_probe_is_admitted_during_a_cooldown(self) -> None:
        # CC-6.
        self._fail(ModerationCheckError("down", KIND_SERVER_ERROR), 3)
        for _ in range(3):
            self._check()
        self.clock.advance(31.0)
        self.client.default = {"flagged": False, "categories": [], "evaluated": True}
        calls_before = self.client.calls
        self.reader.snapshot(
            "pangea_moderation_tier2_dropped_total", cause="breaker_probe_busy"
        )
        # A real probe is in flight only while its call is outstanding, so the
        # second arrival is driven with the first held: the client below never
        # returns until the test lets it.
        held: "defer.Deferred[Any]" = defer.Deferred()

        async def holding(*_args: Any, **_kwargs: Any) -> Any:
            self.client.calls += 1
            return await make_deferred_yieldable(held)

        with patch(
            "synapse_pangea_chat.moderation.choreo_client.moderate_text", holding
        ):
            first: List[Any] = []
            defer.ensureDeferred(self.checker.check("a")).addBoth(first.append)
            self.assertEqual(first, [], "the probe should still be in flight")
            self.assertEqual(self.client.calls, calls_before + 1)
            self.assertIsNone(self._check("b"))
            self.assertEqual(
                self.client.calls,
                calls_before + 1,
                "a second call went out while the probe was outstanding",
            )
            held.callback({"flagged": False, "categories": [], "evaluated": True})
            self.assertEqual(len(first), 1)
        self.assertEqual(self.breaker.state, BREAKER_CLOSED)
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="breaker_probe_busy"
            ),
            1.0,
        )

    def test_a_bad_token_never_opens_the_breaker(self) -> None:
        # CC-7. A 401 repeats forever and is ours to fix; opening on it would
        # disable moderation indefinitely while the breaker's gauge blamed the
        # provider - and the checks must resume the instant the token is
        # fixed, with no further intervention.
        self.client.responses = [
            ModerationCheckError("401", KIND_CONFIG_ERROR) for _ in range(20)
        ]
        for _ in range(20):
            self.assertIsNone(self._check())
        self.assertEqual(self.breaker.state, BREAKER_CLOSED)
        self.assertEqual(self.client.calls, 20)

    def test_a_bad_token_is_logged_once_per_cooldown(self) -> None:
        self.client.responses = [
            ModerationCheckError("401", KIND_CONFIG_ERROR) for _ in range(10)
        ]
        with self.assertLogs(
            "synapse.modules.synapse_pangea_chat.moderation.choreo_client",
            level=logging.ERROR,
        ) as captured:
            for _ in range(5):
                self._check()
            self.clock.advance(31.0)
            self._check()
        self.assertEqual(len(captured.records), 2, captured.output)

    def test_every_failure_mode_returns_no_verdict_rather_than_raising(self) -> None:
        # Fail-open, exhaustively. Anything that escapes here reaches
        # `run_as_background_process`, which logs whatever it catches - so an
        # unmapped exception is both a missed check and a plaintext log record
        # carrying whatever the exception held.
        for error in (
            ModerationCheckError("transport", KIND_TRANSPORT),
            ModerationCheckError("timeout", KIND_TIMEOUT),
            ModerationCheckError("5xx", KIND_SERVER_ERROR),
            ModerationCheckError("429", KIND_RATE_LIMITED),
            ModerationCheckError("401", KIND_CONFIG_ERROR),
            ModerationCheckError("decode", KIND_DECODE),
            ModerationCheckError("shape", KIND_SHAPE),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
            RuntimeError("nobody predicted this"),
        ):
            with self.subTest(error=type(error).__name__):
                # Reset between subtests through the same door production
                # uses: admit, then report success on that admission.
                _refusal, ticket = self.breaker.check()
                self.breaker.record_success(ticket)
                self.client.responses = [error]
                self.assertIsNone(self._check())

    def test_a_cancelled_check_gives_the_probe_permit_back(self) -> None:
        # The half-open probe is a single permit, and the one path that does
        # not report an outcome is cancellation. Without the release the
        # breaker sits in HALF_OPEN refusing every later check as
        # `breaker_probe_busy` for the life of the process, and fixing
        # whatever broke does not recover it.
        self._fail(ModerationCheckError("down", KIND_SERVER_ERROR), 3)
        for _ in range(3):
            self._check()
        self.clock.advance(31.0)
        held: "defer.Deferred[Any]" = defer.Deferred()

        async def holding(*_args: Any, **_kwargs: Any) -> Any:
            return await make_deferred_yieldable(held)

        with patch(
            "synapse_pangea_chat.moderation.choreo_client.moderate_text", holding
        ):
            probe = defer.ensureDeferred(self.checker.check("a"))
            outcome: List[Any] = []
            probe.addBoth(outcome.append)
            self.assertEqual(self.breaker.state, BREAKER_HALF_OPEN)
            probe.cancel()
            self.assertEqual(len(outcome), 1)
            # And the cancellation is NOT absorbed: it has to reach the worker
            # that is being stopped.
            self.assertIsInstance(outcome[0], Failure)
            outcome[0].trap(defer.CancelledError)
        self.assertEqual(self.breaker.state, BREAKER_OPEN)
        self.clock.advance(31.0)
        self.assertIsNotNone(self._check(), "the probe permit was never given back")

    def test_the_latency_of_every_call_is_observed(self) -> None:
        self.reader.snapshot("pangea_moderation_tier2_latency_seconds_count")
        self._check()
        self.client.responses = [ModerationCheckError("down", KIND_SERVER_ERROR)]
        self._check()
        self.assertEqual(
            self.reader.delta("pangea_moderation_tier2_latency_seconds_count"),
            2.0,
            "a failed call has a latency too, and hiding it hides the slow path",
        )


class CancellationRuleTestCase(unittest.TestCase):
    """Every fail-open handler in the Tier-2 path must let a cancellation past.

    Two review rounds found handlers that swallowed `CancelledError` one site
    at a time - the transport, the checker, the worker, the pre-send re-read,
    the redaction send. They are all the same defect, because they are all the
    same idiom: `except Exception` is what fail-open requires, and twisted's
    `CancelledError` is an `Exception`.

    So the rule lives in `compat.reraise_if_cancelled`, and this test is what
    keeps it applied. The structural half reads the source and fails on a
    fail-open handler that does not call it; the behavioural half drives a
    cancellation through the real call path and asserts it comes out the other
    end. Neither alone is enough - the first cannot tell whether the call
    actually does anything, and the second cannot see a site nobody wrote a
    case for.
    """

    # Every module on the Tier-2 dispatch path. Tier 1's own modules are not
    # here: nothing on that path awaits, so nothing on it can be cancelled.
    GUARDED_MODULES = (
        "synapse_pangea_chat/moderation/__init__.py",
        "synapse_pangea_chat/moderation/choreo_client.py",
        "synapse_pangea_chat/moderation/dispatch.py",
    )

    def test_every_handler_around_an_await_applies_the_rule(self) -> None:
        """Read from the AST, and scoped to handlers that can actually see one.

        A cancellation is delivered at an `await`, so the rule applies to a
        broad handler whose `try` body contains one. A narrow synchronous
        guard - `Clock.call_later` refusing, `json.loads` failing, a transport
        teardown - cannot receive a cancellation at all, and demanding the
        call there would be noise that teaches people to ignore the rule.
        """
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent
        unguarded: List[str] = []
        for relative in self.GUARDED_MODULES:
            path = root / relative
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                if not _contains_await(node.body):
                    continue
                for handler in node.handlers:
                    if not _catches_broadly(handler):
                        continue
                    if not _calls_the_rule_first(handler):
                        unguarded.append(f"{relative}:{handler.lineno}")
        self.assertEqual(
            unguarded,
            [],
            "a fail-open handler around an `await` does not call "
            "`reraise_if_cancelled` as its first statement, so it absorbs the "
            "cancellation of the coroutine it is running in:\n" + "\n".join(unguarded),
        )

    def test_the_rule_lets_only_cancellation_past(self) -> None:
        from synapse_pangea_chat.moderation.compat import reraise_if_cancelled

        with self.assertRaises(defer.CancelledError):
            reraise_if_cancelled(defer.CancelledError())
        # Everything else is absorbed by the handler that called it.
        reraise_if_cancelled(RuntimeError("an ordinary failure"))
        reraise_if_cancelled(ModerationCheckError("down", KIND_SERVER_ERROR))

    def test_a_cancellation_travels_the_whole_dispatch_path(self) -> None:
        """End to end, through the real call chain rather than per-frame.

        A worker holding a job is cancelled. The cancellation has to survive
        the transport, the checker and the worker's own handler, and the
        worker has to end - which is what the supervisor then sees.
        """
        from synapse_pangea_chat.moderation.choreo_client import ChoreoChecker
        from synapse_pangea_chat.moderation.dispatch import (
            ModerationJob,
            Tier2Dispatcher,
        )

        clock = FakeClock()
        hs = _Hs(clock)
        breaker = CircuitBreaker(
            clock=clock,
            failure_threshold=3,
            cooldown_seconds=30.0,
            max_cooldown_seconds=120.0,
        )
        held: "defer.Deferred[Any]" = defer.Deferred()

        async def holding(*_args: Any, **_kwargs: Any) -> Any:
            return await make_deferred_yieldable(held)

        checker = ChoreoChecker(
            agent=object(),
            clock=clock,
            base_url="http://choreo.invalid",
            access_token="syt_x",
            breaker=breaker,
            timeout_seconds=15.0,
        )

        async def handler(job: Any) -> None:
            await checker.check(job.text)

        dispatcher = Tier2Dispatcher(
            homeserver=hs,
            clock=clock,
            handler=handler,
            workers=1,
            queue_size=4,
            supervisor_interval_seconds=30.0,
            drain_timeout_seconds=5.0,
        )
        with patch(
            "synapse_pangea_chat.moderation.choreo_client.moderate_text", holding
        ):
            dispatcher.start()
            clock.run_pending()
            dispatcher.enqueue(
                ModerationJob(
                    event_id="$victim",
                    room_id="!room:example.org",
                    sender="@learner:example.org",
                    text="x",
                    enqueued_at=clock.time(),
                )
            )
            for _ in range(10):
                if not clock.run_pending():
                    break
            self.assertEqual(dispatcher.live_workers, 1)
            dispatcher._kill_worker_for_test(0)
            for _ in range(10):
                if not clock.run_pending():
                    break
        self.assertEqual(
            dispatcher.live_workers,
            0,
            "the cancellation was absorbed somewhere on the path and the "
            "worker carried on",
        )
        self.assertEqual(dispatcher.inflight, 0, "the cancelled job leaked")


def _contains_await(body: List[Any]) -> bool:
    """Is there an `await` in this block, not counting nested functions?

    A nested `async def` has its own frame and its own handlers; an `await`
    inside one says nothing about whether the enclosing `try` can see a
    cancellation.
    """
    import ast

    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                return True
    return False


def _catches_broadly(handler: Any) -> bool:
    import ast

    if handler.type is None:
        return True
    return isinstance(handler.type, ast.Name) and handler.type.id in (
        "Exception",
        "BaseException",
    )


def _calls_the_rule_first(handler: Any) -> bool:
    import ast

    for statement in handler.body:
        # Skip a docstring, which is the only statement allowed before the
        # rule. `ast.Str` is gone in 3.12+; a docstring is a `Constant` holding
        # a string.
        if isinstance(statement, ast.Expr) and isinstance(
            statement.value, ast.Constant
        ):
            continue
        return (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == "reraise_if_cancelled"
        )
    return False


class ProxyLogGuardTestCase(unittest.TestCase):
    """The CONNECT reason phrase is a string a proxy chooses, and Synapse logs
    it verbatim at DEBUG on a logger outside this module's namespace."""

    def setUp(self) -> None:
        from synapse_pangea_chat.moderation.choreo_client import (
            _PROXY_LOGGER_NAME,
            install_proxy_log_guard,
        )

        install_proxy_log_guard()
        self.logger = logging.getLogger(_PROXY_LOGGER_NAME)
        self.records: List[str] = []
        self.handler = _CollectingHandler(self.records)
        self.handler.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        self._previous = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.addCleanup(self.logger.setLevel, self._previous)
        self.addCleanup(self.logger.removeHandler, self.handler)

    def test_a_hostile_reason_phrase_does_not_reach_a_handler(self) -> None:
        self.logger.debug(
            "Got Status: %s %s %s", b"200", b"@alice:example.org", b"HTTP/1.1"
        )
        self.assertTrue(self.records, "the record never reached the handler")
        joined = "\n".join(self.records)
        self.assertNotIn("@alice:example.org", joined)
        # The status itself survives: an operator debugging a proxy still
        # needs to see that a status arrived, and which one.
        self.assertIn("200", joined)

    def test_other_records_from_that_logger_are_untouched(self) -> None:
        self.logger.debug("Connecting to %s:%d", "proxy.example.org", 8080)
        self.assertIn("proxy.example.org", "\n".join(self.records))

    def test_the_guard_matches_the_installed_synapse(self) -> None:
        """The guard matches one exact format string. If a Synapse upgrade
        changes it the guard silently stops working, so the coupling is
        asserted rather than hoped for."""
        import inspect

        from synapse.http import connectproxyclient

        from synapse_pangea_chat.moderation.choreo_client import (
            _PROXY_STATUS_LOG_FORMAT,
        )

        source = inspect.getsource(connectproxyclient.HTTPConnectSetupClient)
        self.assertIn(
            f'logger.debug("{_PROXY_STATUS_LOG_FORMAT}"',
            source,
            "Synapse's CONNECT status log line has changed, so the filter "
            "that keeps a proxy's reason phrase out of the log no longer "
            "matches it",
        )

    def test_installing_twice_adds_one_filter(self) -> None:
        from synapse_pangea_chat.moderation.choreo_client import (
            _ProxyStatusFilter,
            install_proxy_log_guard,
        )

        install_proxy_log_guard()
        install_proxy_log_guard()
        self.assertEqual(
            sum(1 for f in self.logger.filters if isinstance(f, _ProxyStatusFilter)),
            1,
        )
