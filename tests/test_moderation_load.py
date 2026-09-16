"""Tier 2 under the load it is sized for, measured rather than asserted.

The capacity claim is arithmetic - 1,000 concurrent students at one message
per 30 seconds is about 33 messages/second, and sixteen workers batching
thirty-two messages per ~2-second provider call clear roughly three times
that. Arithmetic in a comment is not evidence, and the previous sizing
(`workers: 8`, `queue_size: 40`, one message per call) was exactly that: a
number nobody had driven traffic against. It could not have cleared 4
messages/second, and nothing in the suite said so.

So this drives the REAL `Tier2Dispatcher` - the real queue, the real worker
pool, the real batching and linger, the real drop accounting - at the target
rate, and reports achieved throughput, queue depth over time, drops by cause
and end-to-end latency percentiles. **It fails if throughput regresses below
the documented target.**

**What is simulated and what is not.** The provider is a stub that consumes
`PROVIDER_SECONDS` of the fake clock per call, because hitting the real
endpoint in CI would be slow, flaky and expensive. So the 2-second figure is
an assumption carried in from measurement, not something this test
establishes - and it is stated here rather than implied, because a load test
that quietly measures its own stub is worse than none. What the test DOES
establish is everything downstream of that assumption: that the pipeline
amortises one provider call across many messages, that it clears the target
rate without dropping, and that it stops doing so if batching is weakened.
`test_without_batching_the_target_is_missed` is the control - it runs the
same traffic with `max_batch=1` and requires the run to fail the target, so a
green result here cannot come from a stub that is simply too fast.

Time is a fake clock throughout, so the whole run costs milliseconds of real
time regardless of the simulated minutes it covers.
"""

import unittest
from typing import Any, Dict, List, Tuple

from synapse.logging.context import make_deferred_yieldable
from twisted.internet import defer

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.moderation.dispatch import ModerationJob, Tier2Dispatcher
from tests.moderation_doubles import HomeServerDouble, MetricReader

# The load being sized for: 1,000 concurrent students, one message every 30
# seconds. Stated as its two factors rather than as 33.3, so that changing the
# population or the cadence changes the target rather than silently leaving it
# behind.
CONCURRENT_STUDENTS = 1000
SECONDS_BETWEEN_MESSAGES = 30.0
TARGET_RATE = CONCURRENT_STUDENTS / SECONDS_BETWEEN_MESSAGES

# The measured cost of one `/choreo/moderate` call, which the provider
# dominates. An ASSUMPTION here - see the module docstring.
PROVIDER_SECONDS = 2.0

# What share of messages the screen flags, each costing one extra single-text
# confirmation. A classroom estimate, and the term in the arithmetic with the
# least evidence behind it; `pangea_moderation_tier2_screen_total` is what
# turns it into a measurement on real traffic.
FLAG_RATE = 0.05

# How finely the driver steps the fake clock. The measurement's resolution: a
# driver that jumped a whole inter-arrival interval at once reported a 2-second
# check as 20 seconds at a ten-second cadence, which is the tick size and not
# the system. Small enough to resolve one provider call, large enough that a
# two-minute run is a few thousand steps.
TICK_SECONDS = 0.25

# The shipped defaults. Read from the config rather than retyped, so this
# measures what a deployment actually runs - and so a change to the sizing
# cannot leave the load test measuring the old numbers.
_DEFAULTS = PangeaChatConfig(cms_base_url="http://cms.invalid", cms_service_api_key="k")


class _SimulatedProvider:
    """A `/choreo/moderate` that costs time on the fake clock and nothing else.

    It models the two calls a batch really makes - one batched screen, plus
    one single-text confirmation per flagged message - because the
    confirmation traffic is what decides whether the pool keeps up, and a stub
    that ignored it would report a capacity the deployment does not have.
    """

    def __init__(self, clock: Any, flag_every: int) -> None:
        self._clock = clock
        self._flag_every = flag_every
        self.calls = 0
        self.batch_sizes: List[int] = []
        # (event_id, seconds from enqueue to verdict applied, absolute time)
        self.completed: List[Tuple[str, float, float]] = []

    def _flagged(self, job: ModerationJob) -> bool:
        return int(job.event_id.rsplit("-", 1)[1]) % self._flag_every == 0

    async def _spend(self, seconds: float) -> None:
        gate: "defer.Deferred[None]" = defer.Deferred()
        self._clock.call_later(seconds, gate.callback, None)
        await make_deferred_yieldable(gate)

    async def __call__(self, jobs: Tuple[ModerationJob, ...]) -> None:
        self.calls += 1
        self.batch_sizes.append(len(jobs))
        await self._spend(PROVIDER_SECONDS)
        for job in jobs:
            if self._flagged(job):
                # The confirmation: one more provider call, carrying only this
                # message. See `ChatModeration._screen_batch`.
                self.calls += 1
                await self._spend(PROVIDER_SECONDS)
        now = self._clock.time()
        for job in jobs:
            # Every job in the batch is recorded as finishing when the WHOLE
            # batch does, including its confirmations. Pessimistic against the
            # real module, which decides a clean message as it reaches it in
            # the loop rather than after the batch's last confirmation - so
            # the latency reported here is an upper bound on the real one.
            self.completed.append((job.event_id, now - job.enqueued_at, now))


class _Run:
    """One traffic run and everything measured during it."""

    def __init__(
        self,
        *,
        rate: float,
        seconds: float,
        workers: int,
        queue_size: int,
        max_batch: int,
        batch_max_wait: float,
    ) -> None:
        self.clock = HomeServerDouble().clock
        self.hs = HomeServerDouble()
        self.hs.clock = self.clock
        self.provider = _SimulatedProvider(
            self.clock, flag_every=max(int(1 / FLAG_RATE), 1)
        )
        self.dispatcher = Tier2Dispatcher(
            homeserver=self.hs,
            clock=self.clock,
            handler=self.provider,
            workers=workers,
            queue_size=queue_size,
            supervisor_interval_seconds=30.0,
            drain_timeout_seconds=10.0,
            max_batch=max_batch,
            batch_max_wait_seconds=batch_max_wait,
        )
        self.rate = rate
        self.seconds = seconds
        self.offered_window = seconds
        self.queue_depths: List[int] = []
        self.offered = 0
        self.admitted = 0

    def _advance(self, seconds: float) -> None:
        """Step the clock in bounded increments.

        One jump of the whole interval would let a ~2-second provider call and
        a ~30-millisecond queue wait land in the same instant, so every
        latency came back quantised to the inter-arrival time. The steps cost
        nothing - the clock is fake - and they are what makes the latency
        percentiles below mean anything.
        """
        remaining = seconds
        while remaining > 0:
            step = min(TICK_SECONDS, remaining)
            self.clock.advance(step)
            remaining -= step

    def go(self) -> None:
        self.dispatcher.start()
        interval = 1.0 / self.rate
        ticks = int(self.seconds * self.rate)
        self.started = self.clock.time()
        for index in range(ticks):
            job = ModerationJob(
                event_id=f"$load-{index}",
                room_id="!room:example.org",
                sender="@learner:example.org",
                text=f"message {index}",
                enqueued_at=self.clock.time(),
            )
            self.offered += 1
            if self.dispatcher.enqueue(job):
                self.admitted += 1
            self._advance(interval)
            self.queue_depths.append(self.dispatcher.queue_depth)
        self.offered_window = self.clock.time() - self.started
        # Let whatever is in flight finish, so nothing is reported as lost
        # that was merely still running. Bounded: a run that cannot drain in a
        # simulated ten minutes has not kept up, and the assertions say so.
        for _ in range(600):
            if not self.dispatcher.queue_depth and not self.dispatcher.inflight:
                break
            self._advance(1.0)
        self.elapsed = self.clock.time() - self.started
        self.dispatcher._stopping = True
        self.dispatcher._wake_all()
        self.clock.drain()

    @property
    def sustained_throughput(self) -> float:
        """Messages cleared per second DURING the offering window.

        The drain tail is deliberately excluded. Including it divides a fixed
        number of completions by a slightly longer wall time and reports a
        system that kept up perfectly as very slightly short of the arrival
        rate - which is an artefact of where the traffic stopped, not a
        capacity shortfall.

        Note what this can and cannot show: while the pool keeps up, this
        number IS the arrival rate, because nothing else is offered. It only
        measures capacity when the system is saturated, which is what
        `test_saturated_capacity_clears_the_documented_multiple_of_target`
        drives it to.
        """
        if self.offered_window <= 0:
            return 0.0
        end = self.started + self.offered_window
        during = sum(1 for _e, _w, at in self.provider.completed if at <= end)
        return during / self.offered_window

    @property
    def mean_batch(self) -> float:
        sizes = self.provider.batch_sizes
        return sum(sizes) / len(sizes) if sizes else 0.0

    def latency(self, percentile: float) -> float:
        """The `percentile`-th end-to-end wait, `latency(100)` being the worst.

        Nearest-rank, which is what a load report wants: every value returned
        is a wait some message actually had, rather than an interpolation
        between two of them that nothing experienced.
        """
        waits = sorted(wait for _event_id, wait, _at in self.provider.completed)
        if not waits:
            return float("inf")
        index = min(int(percentile / 100.0 * len(waits)), len(waits) - 1)
        return waits[index]

    def report(self, name: str) -> str:
        return "\n".join(
            [
                f"--- {name} ---",
                f"offered              {self.offered} over "
                f"{self.elapsed:.1f}s simulated",
                f"admitted             {self.admitted}",
                f"completed            {len(self.provider.completed)}",
                f"sustained throughput {self.sustained_throughput:.1f} msg/s "
                f"(target {TARGET_RATE:.1f})",
                f"drain tail           " f"{self.elapsed - self.offered_window:.1f}s",
                f"provider calls       {self.provider.calls}",
                f"mean batch           {self.mean_batch:.1f}",
                f"queue depth max/mean {max(self.queue_depths)}/"
                f"{sum(self.queue_depths) / len(self.queue_depths):.1f}",
                f"latency p50/p95/p99  {self.latency(50):.2f}s / "
                f"{self.latency(95):.2f}s / {self.latency(99):.2f}s",
                f"drops                {self.offered - self.admitted}",
            ]
        )


class Tier2LoadTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.reader = MetricReader()

    def _drops(self) -> Dict[str, float]:
        causes = ("queue_full", "handler_error", "cancelled", "shutdown")
        return {
            cause: self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause=cause
            )
            for cause in causes
        }

    def _snapshot_drops(self) -> None:
        for cause in ("queue_full", "handler_error", "cancelled", "shutdown"):
            self.reader.snapshot("pangea_moderation_tier2_dropped_total", cause=cause)

    def test_the_shipped_defaults_keep_up_at_the_target_rate(self) -> None:
        """At the target rate, EVERY message is checked and none is dropped.

        This is the claim stated in the form it can actually be proved. While
        the pool keeps up, throughput is the arrival rate by definition -
        nothing more is offered - so a throughput assertion here would be
        measuring the driver. What is falsifiable, and what the old sizing
        failed, is that all 4,000 messages get a verdict, nothing hits
        `queue_full`, and the queue stays far enough below its capacity to
        absorb a burst on top. `test_saturated_capacity_...` is where the
        ceiling itself is measured.
        """
        self._snapshot_drops()
        run = _Run(
            rate=TARGET_RATE,
            seconds=120.0,
            workers=_DEFAULTS.moderation_tier2_workers,
            queue_size=_DEFAULTS.moderation_tier2_queue_size,
            max_batch=_DEFAULTS.moderation_tier2_max_batch,
            batch_max_wait=_DEFAULTS.moderation_tier2_batch_max_wait_seconds,
        )
        run.go()
        report = run.report("shipped defaults at target rate")
        drops = self._drops()
        self.assertEqual(
            len(run.provider.completed),
            run.offered,
            f"messages went unchecked at the target rate.\n{report}",
        )
        self.assertEqual(
            {cause: value for cause, value in drops.items() if value},
            {},
            f"messages were dropped at the target rate, which means a "
            f"classroom message went unmoderated.\n{report}",
        )
        self.assertLess(
            max(run.queue_depths),
            _DEFAULTS.moderation_tier2_queue_size // 4,
            f"the queue ran past a quarter of its capacity at the SUSTAINED "
            f"target rate, leaving no room for the synchronised burst it is "
            f"sized for.\n{report}",
        )
        self.assertLessEqual(
            run.latency(99),
            30.0,
            f"the 99th-percentile window between a message appearing and its "
            f"verdict exceeded the 30 s student message cadence.\n{report}",
        )

    def test_saturated_capacity_clears_the_documented_multiple_of_target(
        self,
    ) -> None:
        """The ceiling, measured where a regression is visible.

        Offered four times the target, so the pool is saturated and its
        sustained throughput is its actual capacity rather than the arrival
        rate. The config documents ~98 msg/s from
        `16 * 32/(2*(1+32*0.05))`; this requires at least twice the target,
        which the unbatched control (about 8 msg/s) comfortably fails. **This
        is the assertion that fails if batching regresses.**
        """
        self._snapshot_drops()
        run = _Run(
            rate=TARGET_RATE * 4,
            seconds=120.0,
            workers=_DEFAULTS.moderation_tier2_workers,
            queue_size=_DEFAULTS.moderation_tier2_queue_size,
            max_batch=_DEFAULTS.moderation_tier2_max_batch,
            batch_max_wait=_DEFAULTS.moderation_tier2_batch_max_wait_seconds,
        )
        run.go()
        report = run.report("four times the target rate")
        self.assertGreaterEqual(
            run.sustained_throughput,
            TARGET_RATE * 2,
            f"saturated capacity is {run.sustained_throughput:.1f} msg/s, "
            f"under twice the {TARGET_RATE:.1f} msg/s target - so the "
            f"deployment has no headroom over the load it is sized "
            f"for.\n{report}",
        )
        self.assertGreater(
            self._drops()["queue_full"],
            0.0,
            f"four times the target rate dropped nothing, so the documented "
            f"~98 msg/s ceiling understates what the pool can do and an "
            f"operator sizing a deployment from it would over-provision. The "
            f"documented number should be corrected upwards.\n{report}",
        )

    def test_without_batching_the_target_is_missed(self) -> None:
        """The control, and the reason a green result above means anything.

        Same traffic, same pool, `max_batch=1`. If this ALSO kept up then the
        simulated provider would be too cheap to be measuring anything, and
        the test above would pass whatever the dispatcher did. Sixteen
        workers at one ~2-second call each is 8 msg/s against a 33 msg/s
        target, so the queue fills and drop-newest starts discarding.
        """
        self._snapshot_drops()
        run = _Run(
            rate=TARGET_RATE,
            seconds=120.0,
            workers=_DEFAULTS.moderation_tier2_workers,
            queue_size=_DEFAULTS.moderation_tier2_queue_size,
            max_batch=1,
            batch_max_wait=0.0,
        )
        run.go()
        report = run.report("no batching at target rate")
        self.assertLess(
            run.sustained_throughput,
            TARGET_RATE,
            f"an unbatched pool kept up with the target rate, so the "
            f"simulated provider is too cheap for this suite to be measuring "
            f"capacity at all.\n{report}",
        )
        self.assertGreater(
            self._drops()["queue_full"],
            0.0,
            f"an unbatched pool overflowed nothing, so the queue is not the "
            f"constraint this test believes it is.\n{report}",
        )

    def test_a_lone_message_is_not_slowed_by_batching(self) -> None:
        """Latency at a trickle, which is where a batching window would show.

        One message every ten seconds into an empty pool. Every one of them
        should be picked up immediately and cost one provider call and
        nothing else: if the linger were reaching a lone message, the p99
        here would carry the window on top.
        """
        run = _Run(
            rate=0.1,
            seconds=100.0,
            workers=_DEFAULTS.moderation_tier2_workers,
            queue_size=_DEFAULTS.moderation_tier2_queue_size,
            max_batch=_DEFAULTS.moderation_tier2_max_batch,
            batch_max_wait=_DEFAULTS.moderation_tier2_batch_max_wait_seconds,
        )
        run.go()
        report = run.report("one message per ten seconds")
        self.assertEqual(
            run.mean_batch,
            1.0,
            f"a trickle was batched, so messages waited for each other.\n" f"{report}",
        )
        self.assertLessEqual(
            run.latency(50),
            PROVIDER_SECONDS + 2 * TICK_SECONDS,
            f"the median lone message paid measurably more than the single "
            f"provider call it had to make, so batching is reaching an idle "
            f"system.\n{report}",
        )
        # The tail is the FLAGGED message, and it is meant to be there: a
        # flagged message pays a screen and then a single-text confirmation,
        # which is what makes a redaction safe to take. Two provider calls is
        # the most any one message can owe, so that is what is pinned - a
        # third would mean something is asking twice.
        self.assertLessEqual(
            run.latency(100),
            2 * PROVIDER_SECONDS + 2 * TICK_SECONDS,
            f"a lone message paid more than a screen plus one confirmation, "
            f"which is the most any single message can owe.\n{report}",
        )


class LoadReportTestCase(unittest.TestCase):
    """The numbers themselves, printed where a reviewer can read them.

    Not an assertion - the assertions are above. This exists because "what
    does it actually do" is the first question asked of a capacity change,
    and the answer should not require re-deriving it from the test names.
    """

    def test_report(self) -> None:
        reports: List[str] = []
        for name, rate, max_batch in (
            ("target rate, batching", TARGET_RATE, 32),
            ("target rate, no batching", TARGET_RATE, 1),
            ("4x target, batching", TARGET_RATE * 4, 32),
        ):
            run = _Run(
                rate=rate,
                seconds=120.0,
                workers=_DEFAULTS.moderation_tier2_workers,
                queue_size=_DEFAULTS.moderation_tier2_queue_size,
                max_batch=max_batch,
                batch_max_wait=(
                    _DEFAULTS.moderation_tier2_batch_max_wait_seconds
                    if max_batch > 1
                    else 0.0
                ),
            )
            run.go()
            reports.append(run.report(name))
        print("\n\n".join(reports))
        self.assertTrue(reports)


if __name__ == "__main__":
    unittest.main()
