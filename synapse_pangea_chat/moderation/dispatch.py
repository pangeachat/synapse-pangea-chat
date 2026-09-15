"""The Tier-2 queue, worker pool and drain.

`ThirdPartyEventRules.on_new_event` is **awaited inline** by
`Notifier.notify_new_room_events`, so whatever it does is paid for by every
event on the homeserver. Before this, it started one
`run_as_background_process` per message and built a fresh
`twisted.web.client.Agent` inside each one: at a few hundred concurrent
learners that is unbounded fan-out, one TCP and TLS handshake per message, no
backpressure, no overflow policy and no way to see any of it.

What replaces it is deliberately small: a bounded buffer, a fixed number of
long-lived consumers, and one scheduled wakeup between them.

**Why a plain `deque` and not `DeferredQueue`.** `DeferredQueue` looks right -
`put()` raises `QueueOverflow` synchronously, which is an observable drop you
can count, where `deque(maxlen=N)` discards silently with no exception, hook or
count. But `put()` also does this (`twisted/internet/defer.py`)::

    def put(self, obj):
        if self.waiting:
            self.waiting.pop(0).callback(obj)

When a consumer is parked in `get()`, `put()` **runs that consumer inline,
inside the producer's call**, until its next suspension. Two consequences, both
disqualifying here: enqueueing would execute moderation work inside the
notifier's await - the exact back-pressure this design exists to remove - and
the producer's logcontext would be handed to the consumer, which is the leak
class of commit `33f7ead`. So: our own capacity check on a plain `deque`,
which gives the observable drop without the inline resume.

**Why the wakeup goes through `Clock.call_later`.** `call_later` wraps both the
scheduling and the firing in `PreserveLoggingContext`, and since 1.159 it
asserts the sentinel context when the callback runs. The producer's context
therefore cannot reach the reactor, and a parked worker is resumed from the
sentinel rather than from the notifier's stack, with `make_deferred_yieldable`
restoring the worker's own context as it wakes. That is the entire difference
from `DeferredQueue.put`, and the callback itself does nothing but fire a
Deferred - the real work runs inside the worker's own
`run_as_background_process`.

**Overflow is drop-newest, and that is a decision about moderation rather than
a default.** Three reasons. A message's moderation value decays: a verdict that
arrives after everyone has read the message has already lost most of what it
was for, so spending the queue on the oldest waiting work keeps latency bounded
for everything admitted. The drop lands at a single, synchronous, well-defined
point - admission - where it can be counted before any work is done, rather
than as a later eviction off the admission path. And a rejected message is
never half-processed, so there is no state to unwind.

The honest limit: neither policy is safe under adversarial load. A benign flood
can fill the queue before a dangerous message arrives, and drop-newest then
keeps the flood and discards the dangerous message. Priority would change which
flood displaced which message, not whether one could. The drop counter is what
tells an operator the heuristic is being overwhelmed, and it is the reason a
silent drop is not acceptable at any capacity.
"""

from collections import deque
from typing import Any, Callable, Deque, List, Optional, Set

import attr
from synapse.logging.context import (
    PreserveLoggingContext,
    make_deferred_yieldable,
    run_in_background,
)
from synapse.metrics.background_process_metrics import run_as_background_process
from twisted.internet import defer

from synapse_pangea_chat.moderation import metrics
from synapse_pangea_chat.moderation.compat import (
    _SecondsInterval,
    background_process_args,
    looping_call_interval,
    register_shutdown_handler,
)
from synapse_pangea_chat.moderation.log_safety import error_site, scrubbing_logger

logger = scrubbing_logger("synapse.modules.synapse_pangea_chat.moderation.dispatch")


@attr.s(auto_attribs=True, frozen=True, slots=True)
class ModerationJob:
    """One message waiting to be checked.

    Ids and one copy of the text, never the `EventBase`: an event holds its
    signatures, its unsigned block and its room's cached state, and a queue of
    them would be an unbounded amount of memory held for a bounded amount of
    work.
    """

    event_id: str
    room_id: str
    sender: str
    text: str
    enqueued_at: float


class Tier2Dispatcher:
    """A bounded queue drained by a fixed pool of long-lived workers."""

    def __init__(
        self,
        *,
        homeserver: Any,
        clock: Any,
        handler: Callable[[ModerationJob], Any],
        workers: int,
        queue_size: int,
        supervisor_interval_seconds: float,
        drain_timeout_seconds: float,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1")
        self._hs = homeserver
        self._clock = clock
        self._handler = handler
        self._worker_count = workers
        self._queue_size = queue_size
        self._supervisor_interval = supervisor_interval_seconds
        self._drain_timeout = drain_timeout_seconds

        self._queue: Deque[ModerationJob] = deque()
        self._waiters: List["defer.Deferred[None]"] = []
        self._workers: List[Optional["defer.Deferred[Any]"]] = [None] * workers
        self._inflight: Set[str] = set()
        self._running: Set[str] = set()
        self._stopping = False
        self._started = False
        self._wakeup_scheduled = False
        self._drain_waiters: List["defer.Deferred[None]"] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for index in range(self._worker_count):
            self._start_worker(index)
        # `Clock.looping_call` directly, matching `delete_user.py` and
        # `export_user_data.py`. `ModuleApi.looping_background_call` would
        # route through `HomeServer.run_as_background_process`, which RAISES
        # once shutdown begins - and a looping call whose function raises is
        # logged "Looping call died" and then never runs again. On the way
        # down that is merely noisy; the habit is what is dangerous.
        self._clock.looping_call(
            self._supervise,
            looping_call_interval(self._supervisor_interval),
        )
        registered = register_shutdown_handler(self._hs, self._on_shutdown)
        if not registered:
            logger.warning(
                "moderation dispatcher could not register a shutdown handler; "
                "queued work will be lost without being counted on a restart"
            )

    def _start_worker(self, index: int) -> None:
        self._workers[index] = run_as_background_process(
            *background_process_args(
                self._hs, "pangea_moderation_tier2_worker", self._worker
            ),
            index,
        )

    def _on_shutdown(self) -> Any:
        # Returns the Deferred so a reactor trigger that honours one waits for
        # the drain. Synapse 1.159's own wrapper does not - it launches the
        # callback with `run_in_background` and discards the result - which is
        # why `shutdown` is written to be correct without being awaited:
        # bounded, and counting whatever it abandons.
        return run_in_background(self.shutdown)

    # ------------------------------------------------------------------
    # Admission
    # ------------------------------------------------------------------

    def enqueue(self, job: ModerationJob) -> bool:
        """Accept ``job``, or refuse it and say why. Never raises, never waits.

        This runs inside `Notifier.notify_new_room_events`, which awaits it
        for every event on the homeserver. Anything that could raise out of
        here, or block, is a defect that shows up as delayed event
        persistence for every user in every room.
        """
        try:
            return self._enqueue(job)
        except Exception as exc:
            # silent-ok: fail-open by contract. A moderation queue that could
            # raise into the notifier would make a moderation bug an outage.
            logger.warning(
                "tier2 enqueue failed for %s at %s (%s)",
                job.event_id,
                error_site(exc),
                type(exc).__name__,
            )
            return False

    def _enqueue(self, job: ModerationJob) -> bool:
        if self._stopping:
            metrics.record_drop("stopping")
            return False
        if job.event_id in self._inflight:
            # `on_new_event` fires once per notifier call, and the same event
            # reaches the notifier from the local persister AND from the
            # replication stream. Checking it twice would mean two moderation
            # calls and two redaction attempts for one message.
            metrics.record_drop("duplicate")
            return False
        if len(self._queue) >= self._queue_size:
            metrics.record_drop("queue_full")
            return False

        self._inflight.add(job.event_id)
        self._queue.append(job)
        metrics.TIER2_QUEUE_DEPTH.set(len(self._queue))
        if not self._schedule_wakeup():
            # The clock is gone, so nothing will ever wake a worker for this
            # job. Leaving it on the queue would count as accepted and never
            # be checked, which is the silent drop this whole file is about.
            self._queue.pop()
            self._inflight.discard(job.event_id)
            metrics.TIER2_QUEUE_DEPTH.set(len(self._queue))
            metrics.record_drop("no_clock")
            return False
        metrics.TIER2_ENQUEUED.inc()
        return True

    def _schedule_wakeup(self) -> bool:
        if not self._waiters or self._wakeup_scheduled:
            # No parked worker to wake, or a wakeup already pending. The
            # pending one wakes as many workers as there is work for, so
            # coalescing cannot strand a job.
            return True
        try:
            self._clock.call_later(_SecondsInterval(0.0), self._wake_available)
        except Exception:
            # `Clock.call_later` raises once the clock has been shut down.
            return False
        self._wakeup_scheduled = True
        return True

    def _wake_available(self) -> None:
        self._wakeup_scheduled = False
        count = min(len(self._waiters), len(self._queue))
        for _ in range(count):
            self._wake_one()

    def _wake_one(self) -> None:
        if not self._waiters:
            return
        waiter = self._waiters.pop(0)
        if waiter.called:
            return
        # Firing a Deferred resumes the coroutine awaiting it, right here,
        # and that coroutine restores its own logcontext as it goes. Without
        # the wrapper the reactor is handed back whatever context the resumed
        # worker left set. Synapse wraps every such handoff the same way -
        # `ReadWriteLock`, its wakeup helper, `timeout_deferred`.
        with PreserveLoggingContext():
            waiter.callback(None)

    def _wake_all(self) -> None:
        while self._waiters:
            self._wake_one()

    def _discard_waiter(self, waiter: "defer.Deferred[None]") -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            # Already taken by `_wake_one`, which is the ordinary path.
            pass

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    async def _worker(self, index: int) -> None:
        while not self._stopping:
            job = self._take()
            if job is None:
                waiter: "defer.Deferred[None]" = defer.Deferred()
                self._waiters.append(waiter)
                # No `await` between `_take` returning None and parking, and
                # the reactor is single-threaded, so there is no window in
                # which a job could arrive unseen by both.
                try:
                    await make_deferred_yieldable(waiter)
                finally:
                    # The `finally` is load-bearing. A worker can leave this
                    # await by being cancelled - a crash, a drain - and a
                    # waiter belonging to a worker that is gone would stay in
                    # the list, absorb the next wakeup, and leave the live
                    # workers asleep with work queued. The queue would then
                    # fill and start dropping while nothing was running.
                    self._discard_waiter(waiter)
                continue
            await self._run(job)

    def _take(self) -> Optional[ModerationJob]:
        if not self._queue:
            return None
        job = self._queue.popleft()
        metrics.TIER2_QUEUE_DEPTH.set(len(self._queue))
        metrics.TIER2_QUEUE_WAIT.observe(max(self._clock.time() - job.enqueued_at, 0.0))
        return job

    async def _run(self, job: ModerationJob) -> None:
        self._running.add(job.event_id)
        metrics.TIER2_INFLIGHT.set(len(self._running))
        try:
            await self._handler(job)
        except Exception as exc:
            # silent-ok: fail-open by contract, and the loop has to survive.
            # A worker that dies leaves the pool one short for the life of
            # the process, and `run_as_background_process` swallows what
            # reaches it - so an unhandled exception here is invisible.
            logger.warning(
                "tier2 job failed for %s at %s (%s)",
                job.event_id,
                error_site(exc),
                type(exc).__name__,
            )
        finally:
            # In a `finally` on every path - completion, failure,
            # cancellation. An id left behind does not just leak: it
            # permanently blocks that event from ever being moderated again.
            self._running.discard(job.event_id)
            self._inflight.discard(job.event_id)
            metrics.TIER2_INFLIGHT.set(len(self._running))
            self._notify_drained()

    def _supervise(self) -> None:
        """Restart any worker that is no longer running.

        Necessary because `run_as_background_process` catches and logs
        whatever escapes its coroutine and then returns - so a worker that
        dies dies silently, and the pool shrinks with nothing to show for it.

        This is a looping call, so it must never raise: a looping call whose
        function raises is logged "Looping call died" and stops permanently.
        """
        try:
            if self._stopping:
                return
            for index, worker in enumerate(self._workers):
                if worker is not None and not worker.called:
                    continue
                logger.warning("restarting dead tier2 moderation worker %d", index)
                metrics.TIER2_WORKERS_RESTARTED.inc()
                self._start_worker(index)
        except Exception as exc:
            # silent-ok: raising here would kill the supervisor itself, which
            # is the one thing standing between a dead worker and a pool that
            # never recovers.
            logger.warning(
                "tier2 worker supervision failed at %s (%s)",
                error_site(exc),
                type(exc).__name__,
            )

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    @property
    def live_workers(self) -> int:
        return sum(
            1 for worker in self._workers if worker is not None and not worker.called
        )

    def _kill_worker_for_test(self, index: int) -> None:
        """Stop one worker the way a crash would: silently.

        Test-only, and it lives here rather than in the test so it kills a
        worker through the same door a real failure uses - the coroutine
        ends and its Deferred fires - instead of reaching into private state
        from outside and proving something else.
        """
        worker = self._workers[index]
        if worker is None or worker.called:
            return
        worker.cancel()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Stop accepting, count what is lost, wait briefly for the rest.

        Bounded on every path. The reactor does not reliably wait for this -
        Synapse 1.159 launches a registered async shutdown handler and
        discards the Deferred - so a drain that could block would block
        nothing useful, while a drain that could hang would hang a test
        harness and, on the fallback registration path, the reactor itself.
        """
        self._stopping = True

        dropped = len(self._queue)
        while self._queue:
            job = self._queue.popleft()
            self._inflight.discard(job.event_id)
        metrics.TIER2_QUEUE_DEPTH.set(0)
        if dropped:
            # The queue is in memory, so a stop loses it. What must not happen
            # is losing it silently: these messages were accepted and will
            # never be checked, and nothing replays them - stream positions
            # resume from shared database state, not from a moderation cursor.
            metrics.record_drop("shutdown", dropped)
            logger.warning(
                "tier2 moderation dropped %d queued messages on shutdown", dropped
            )

        self._wake_all()

        if not self._running:
            return

        waiter: "defer.Deferred[None]" = defer.Deferred()
        self._drain_waiters.append(waiter)
        try:
            timeout: Any = self._clock.call_later(
                _SecondsInterval(self._drain_timeout), self._abandon_drain
            )
        except Exception:
            # The clock is already down, so there is no way to bound a wait -
            # and an unbounded one is worse than an abandoned one.
            self._abandon_drain()
            return
        try:
            await make_deferred_yieldable(waiter)
        finally:
            if timeout.active():
                timeout.cancel()

    def _notify_drained(self) -> None:
        if not self._drain_waiters or self._running:
            return
        self._finish_drain()

    def _abandon_drain(self) -> None:
        abandoned = len(self._running)
        if abandoned:
            metrics.record_drop("drain_timeout", abandoned)
            logger.warning(
                "tier2 moderation abandoned %d in-flight checks at the drain "
                "deadline",
                abandoned,
            )
        self._finish_drain()

    def _finish_drain(self) -> None:
        waiters, self._drain_waiters = self._drain_waiters, []
        for waiter in waiters:
            with PreserveLoggingContext():
                waiter.callback(None)
