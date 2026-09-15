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
        self._pending: List[Tuple[float, int, Callable[..., Any], tuple, dict]] = []
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
        self._seq += 1
        when = self._now + float(delay)
        self._pending.append((when, self._seq, callback, args, kwargs))
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
            _when, _seq, callback, args, kwargs = entry
            _fire_as_synapse_would(callback, args, kwargs)
            fired += 1
        return fired

    def advance(self, seconds: float) -> None:
        self._now += seconds
        self.run_pending()

    def fire_looping(self) -> None:
        for f, _interval, args in list(self.looping):
            f(*args)


class MetricReader:
    """Reads a metric's value, by name, out of the default registry.

    By name and not by reaching into a collector's private `_value`: the
    assertion a test wants to make is "an operator scraping this server sees
    the drop", and the only thing that establishes that is the sample the
    registry exposes.
    """

    def __init__(self) -> None:
        from prometheus_client import REGISTRY

        self._registry = REGISTRY
        self._base: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}

    def _read(self, name: str, **labels: str) -> float:
        value = self._registry.get_sample_value(name, labels or None)
        return 0.0 if value is None else float(value)

    def snapshot(self, name: str, **labels: str) -> None:
        self._base[(name, tuple(sorted(labels.items())))] = self._read(name, **labels)

    def delta(self, name: str, **labels: str) -> float:
        key = (name, tuple(sorted(labels.items())))
        return self._read(name, **labels) - self._base.get(key, 0.0)

    def value(self, name: str, **labels: str) -> float:
        return self._read(name, **labels)


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

    def test_closed_admits_and_stays_closed_under_the_threshold(self) -> None:
        self.assertIsNone(self.breaker.check())
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, BREAKER_CLOSED)
        self.assertIsNone(self.breaker.check())

    def test_a_success_resets_the_consecutive_count(self) -> None:
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.breaker.record_success()
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, BREAKER_CLOSED)

    def test_consecutive_failures_open_it_and_it_then_refuses(self) -> None:
        for _ in range(3):
            self.breaker.record_failure()
        self.assertEqual(self.breaker.state, BREAKER_OPEN)
        self.assertEqual(self.breaker.check(), "breaker_open")

    def test_cooldown_admits_exactly_one_probe(self) -> None:
        for _ in range(3):
            self.breaker.record_failure()
        self.clock.advance(29.0)
        self.assertEqual(self.breaker.check(), "breaker_open")
        self.clock.advance(2.0)
        self.assertIsNone(self.breaker.check())
        self.assertEqual(self.breaker.state, BREAKER_HALF_OPEN)
        # The latch: a second job arriving while the probe is outstanding is
        # refused rather than becoming a second probe.
        self.assertEqual(self.breaker.check(), "breaker_probe_busy")

    def test_probe_success_closes_and_resets_the_cooldown(self) -> None:
        for _ in range(3):
            self.breaker.record_failure()
        self.clock.advance(31.0)
        self.assertIsNone(self.breaker.check())
        self.breaker.record_success()
        self.assertEqual(self.breaker.state, BREAKER_CLOSED)
        self.assertIsNone(self.breaker.check())
        # Cooldown is back to the base value, not the doubled one.
        for _ in range(3):
            self.breaker.record_failure()
        self.clock.advance(31.0)
        self.assertIsNone(self.breaker.check())

    def test_probe_failure_reopens_with_a_doubled_cooldown(self) -> None:
        for _ in range(3):
            self.breaker.record_failure()
        self.clock.advance(31.0)
        self.assertIsNone(self.breaker.check())
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, BREAKER_OPEN)
        # 60s now, not 30s: at 31s past the reopen it is still refusing.
        self.clock.advance(31.0)
        self.assertEqual(self.breaker.check(), "breaker_open")
        self.clock.advance(30.0)
        self.assertIsNone(self.breaker.check())

    def test_cooldown_is_capped(self) -> None:
        for _ in range(3):
            self.breaker.record_failure()
        for _ in range(8):
            self.clock.advance(1000.0)
            self.assertIsNone(self.breaker.check())
            self.breaker.record_failure()
        self.clock.advance(121.0)
        self.assertIsNone(self.breaker.check(), "cooldown grew past its cap")

    def test_config_error_never_opens_it(self) -> None:
        for _ in range(50):
            self.breaker.record_config_error()
        self.assertEqual(self.breaker.state, BREAKER_CLOSED)
        self.assertIsNone(self.breaker.check())

    def test_config_error_logging_is_rate_limited_to_one_per_cooldown(self) -> None:
        self.assertTrue(self.breaker.record_config_error())
        self.assertFalse(self.breaker.record_config_error())
        self.clock.advance(31.0)
        self.assertTrue(self.breaker.record_config_error())

    def test_a_config_error_does_not_reset_a_real_failure_run(self) -> None:
        # A bad token arriving between two genuine outages must neither open
        # the breaker nor paper over the outage by clearing the count.
        self.breaker.record_failure()
        self.breaker.record_failure()
        self.breaker.record_config_error()
        self.breaker.record_failure()
        self.assertEqual(self.breaker.state, BREAKER_OPEN)

    def test_state_is_published_as_a_gauge(self) -> None:
        reader = MetricReader()
        self.assertEqual(
            reader.value("pangea_moderation_tier2_breaker_state"),
            float(mod_metrics.BREAKER_STATE_VALUES[BREAKER_CLOSED]),
        )
        for _ in range(3):
            self.breaker.record_failure()
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

    def __init__(self) -> None:
        self.shutdown_handlers: List[Any] = []

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
        self.raise_for: set = set()

    async def __call__(self, job: Any) -> None:
        self.started.append(job.event_id)
        if self.hold:
            gate: "defer.Deferred[None]" = defer.Deferred()
            self.gates[job.event_id] = gate
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
        self.hs = _Hs()
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

    def test_shutdown_returns_even_with_a_dead_clock(self) -> None:
        # The drain deadline is itself a `call_later`, and the clock may
        # already be down by the time a "before shutdown" trigger runs. A
        # drain that depended on scheduling one more call would hang there.
        self.dispatcher.start()
        self.handler.hold = True
        self._drain()
        self.dispatcher.enqueue(self._job("$stuck"))
        self._drain()
        self.clock.shutdown = True
        drained = start_worker(self.dispatcher.shutdown)
        self.assertTrue(drained.called)

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
            http_client=object(),
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
                self.breaker.record_success()
                self.client.responses = [error]
                self.assertIsNone(self._check())

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
