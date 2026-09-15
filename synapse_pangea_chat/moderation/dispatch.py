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
    reraise_if_cancelled,
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
        # The AUTHORITATIVE liveness signal, set by the worker coroutine
        # itself and cleared in its own `finally`.
        #
        # The obvious signal - "has the worker's background-process Deferred
        # fired" - is wrong, and wrong in the direction that does damage.
        # Cancelling that Deferred mid-job raises `CancelledError` inside the
        # coroutine; anything that catches `Exception` catches it too, so the
        # coroutine can carry on and park again while its Deferred is already
        # `called`. A supervisor reading the Deferred then sees a dead worker,
        # starts a replacement, and the pool grows by one every time it
        # happens - with `workers=1`, six live consumers.
        self._worker_alive: List[bool] = [False] * workers
        self._inflight: Set[str] = set()
        self._running: Set[str] = set()
        self._stopping = False
        self._started = False
        self._drained = False
        self._wakeup_call: Any = None
        self._drain_waiters: List["defer.Deferred[None]"] = []
        self._drain_deadline: Any = None

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
        self._worker_alive[index] = True
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
            reraise_if_cancelled(exc)
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
        if not self._waiters:
            # No parked worker to wake. Whichever worker is running will take
            # this job when it comes back for one.
            return True
        if self._wakeup_pending():
            # A wakeup is already scheduled, and it wakes as many workers as
            # there is work for, so coalescing cannot strand a job.
            return True
        try:
            self._wakeup_call = self._clock.call_later(
                _SecondsInterval(0.0), self._wake_available
            )
        except Exception:
            # `Clock.call_later` raises once the clock has been shut down.
            self._wakeup_call = None
            return False
        return True

    def _wakeup_pending(self) -> bool:
        """Is a wakeup actually still going to happen?

        The handle, not a boolean. `Clock.shutdown()` CANCELS every delayed
        call the clock is tracking, so a flag set when the wakeup was
        scheduled goes on claiming a wakeup that will never fire - and every
        later enqueue then coalesces into it and is accepted, leaving jobs on
        the queue with every worker parked, no wakeup pending, and nothing
        counted. Asking the handle whether it is still active cannot be wrong
        in that direction.
        """
        call = self._wakeup_call
        if call is None:
            return False
        try:
            return bool(call.active())
        except Exception:
            return False

    def _wake_available(self) -> None:
        self._wakeup_call = None
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
        try:
            await self._worker_loop(index)
        finally:
            # Whatever ends this coroutine - a return, an exception, a
            # cancellation - the slot is free from here and only from here.
            self._worker_alive[index] = False

    async def _worker_loop(self, index: int) -> None:
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
            # Counted on the way past - the message was accepted and will not
            # be checked, and an uncounted one is a silent drop whichever door
            # it left by - and then re-raised rather than absorbed.
            reraise_if_cancelled(exc, lambda: metrics.record_drop("cancelled"))
            # silent-ok: fail-open by contract, and the loop has to survive.
            # A worker that dies leaves the pool one short for the life of
            # the process, and `run_as_background_process` swallows what
            # reaches it - so an unhandled exception here is invisible.
            #
            # Counted for the same reason as the cancellation above: a job
            # that fails BEFORE the checker runs never reaches the checker's
            # own outcome counter, so without this it disappears from the
            # metrics entirely - accepted, never checked, never accounted
            # for.
            metrics.record_drop("handler_error")
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
            #
            # The limit, stated rather than discovered: on the cancellation
            # path the claim is released while a redaction this job had
            # already submitted may still be committing, so a redelivery of
            # the same event could be admitted, re-read a target that is not
            # yet redacted, and send a second redaction. Nothing in production
            # cancels a worker - the drain abandons rather than cancels, the
            # supervisor restarts rather than cancels, and the client's
            # deadlines cancel the HTTP request and not the worker - so the
            # window is not reachable today. Closing it for good needs a
            # durable claim that outlives the process, which is the same thing
            # cross-instance idempotency needs and is tracked with it.
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
            for index, alive in enumerate(self._worker_alive):
                if alive:
                    continue
                logger.warning("restarting dead tier2 moderation worker %d", index)
                metrics.TIER2_WORKERS_RESTARTED.inc()
                self._start_worker(index)
        except Exception as exc:
            reraise_if_cancelled(exc)
            # silent-ok: raising here would kill the supervisor itself, which
            # is the one thing standing between a dead worker and a pool that
            # never recovers.
            logger.warning(
                "tier2 worker supervision failed at %s (%s)",
                error_site(exc),
                type(exc).__name__,
            )

    @property
    def actions_permitted(self) -> bool:
        """May a job still act on a room?

        False from the moment the drain ENDS - not from the moment it starts.
        A drain is for finishing the work, so a job that completes inside the
        window must still be able to send its redaction; a job the drain wrote
        off must not, and cancelling it is not enough on its own to guarantee
        that. `CancelledError` IS an `Exception`, and every handler in this
        module has a broad `except` by design, so a handler can absorb the
        cancellation and carry on. The redaction path asks this before it
        sends anything, which is a check no exception can swallow.
        """
        return not self._drained

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    @property
    def live_workers(self) -> int:
        return sum(1 for alive in self._worker_alive if alive)

    def _kill_worker_for_test(self, index: int) -> None:
        """Stop one worker the way a crash would: silently.

        Test-only, and it lives here rather than in the test so it kills a
        worker through the same door a real failure uses - the coroutine ends
        and its Deferred fires - instead of reaching into private state from
        outside and proving something else.
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

        **Bounded on every path, and the bound does not depend on anything
        that can be taken away.** The deadline is a timer taken from the
        REACTOR, not from `synapse.util.Clock`: `HomeServer.shutdown()` starts
        its async shutdown handlers without awaiting them and then calls
        `Clock.shutdown()`, which cancels every delayed call the clock is
        tracking - including, if we used it, the one that would end this wait.
        A stalled job would then leave this coroutine parked and `_running`
        populated for the life of the process.

        **The reactor does not reliably wait for this either.** Synapse
        1.159's `add_system_event_trigger` launches the callback with
        `run_in_background` and discards the Deferred, so on that path the
        drain is advisory. Everything that must happen regardless - refusing
        new work, counting what was queued, waking parked workers - happens
        synchronously, before the first `await`.
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

        # Recorded SYNCHRONOUSLY, before anything that can be awaited or
        # cancelled. The drain that follows may never finish: Synapse 1.159
        # launches a registered shutdown handler and discards its Deferred, so
        # if the reactor stops first, neither the deadline nor the
        # abandonment accounting runs at all. This gauge and this log line are
        # what an operator has in that case - the number of checks that were
        # in flight when the stop began - and neither of them depends on the
        # drain reaching its end.
        still_running = len(self._running)
        metrics.TIER2_SHUTDOWN_INFLIGHT.set(still_running)
        if still_running:
            logger.warning(
                "tier2 moderation shutting down with %d checks in flight",
                still_running,
            )

        if self._drained or not self._running:
            # Nothing to wait for, or a previous drain already accounted for
            # everything. A second call must not count the same jobs again.
            self._drained = True
            return

        waiter: "defer.Deferred[None]" = defer.Deferred()
        self._drain_waiters.append(waiter)
        self._arm_drain_deadline()
        if self._drained:
            # The deadline could not be armed at all, so the drain was
            # abandoned synchronously and the waiter has already fired.
            return
        try:
            await make_deferred_yieldable(waiter)
        finally:
            # Removed on EVERY exit, cancellation included. A caller that
            # cancels its own shutdown would otherwise leave its waiter on the
            # list for as long as the stalled job lasts, one per attempt.
            if waiter in self._drain_waiters:
                self._drain_waiters.remove(waiter)

    def _arm_drain_deadline(self) -> None:
        if self._drain_deadline is not None:
            return
        reactor = getattr(self._hs, "get_reactor", None)
        if reactor is None:
            self._abandon_drain()
            return
        try:
            self._drain_deadline = reactor().callLater(
                self._drain_timeout, self._on_drain_deadline
            )
        except Exception:
            # The reactor is already stopping, so there is no way to bound a
            # wait - and an unbounded one is worse than an abandoned one.
            self._abandon_drain()

    def _on_drain_deadline(self) -> None:
        # Straight from the reactor, so nothing has wrapped this in a
        # logcontext; `_finish_drain` resumes the waiting coroutine and does
        # its own wrapping.
        self._drain_deadline = None
        self._abandon_drain()

    def _notify_drained(self) -> None:
        if not self._drain_waiters or self._running:
            return
        self._finish_drain()

    def _abandon_drain(self) -> None:
        abandoned = sorted(self._running)
        if abandoned:
            metrics.record_drop("drain_timeout", len(abandoned))
            logger.warning(
                "tier2 moderation abandoned %d in-flight checks at the drain "
                "deadline",
                len(abandoned),
            )
            # Forgotten as well as counted, so a second shutdown does not
            # wait on them again and count them again.
            for event_id in abandoned:
                self._running.discard(event_id)
                self._inflight.discard(event_id)
            metrics.TIER2_INFLIGHT.set(len(self._running))
        # STOPPED, and not merely written off. Clearing the accounting and
        # walking away left the coroutines running: the drain reported the job
        # abandoned and returned, and the job then woke up, read the database
        # and sent a redaction into a room after the homeserver had been told
        # moderation was finished. Cancelling the worker ends it at whatever
        # `await` it is sitting on.
        #
        # `_finish_drain` runs first so `actions_permitted` is already false
        # by the time a cancelled coroutine resumes: cancellation is the
        # mechanism and the flag is the guarantee, because a handler that
        # catches `Exception` catches `CancelledError` with it.
        self._finish_drain()
        self._cancel_workers()

    def _cancel_workers(self) -> None:
        # Under `PreserveLoggingContext`, for the same reason `_wake_one` is:
        # cancelling a Deferred RESUMES the coroutine awaiting it, right here,
        # and that coroutine restores its own context as it goes. Without the
        # wrapper the caller's context is handed to the worker and lost - the
        # leak class of commit 33f7ead, and on Synapse 1.159 a leak that
        # happens inside a timer permanently kills the timer.
        with PreserveLoggingContext():
            for index, worker in enumerate(self._workers):
                if worker is None or worker.called:
                    continue
                try:
                    worker.cancel()
                except Exception:
                    # A canceller that raises must not stop the rest being
                    # cancelled, and there is nothing useful to do about it
                    # here: the process is on its way down.
                    logger.warning("tier2 worker %d could not be cancelled", index)

    def _finish_drain(self) -> None:
        if not self._drained:
            # The END of the drain, and it is logged separately from the start
            # on purpose: "shutting down with N in flight" proves only that
            # the handler was entered, and an end-to-end test asserting on it
            # passes just as well against a `shutdown` that returns
            # immediately, registering no waiter and running no deadline.
            logger.info(
                "tier2 moderation drain finished with %d checks unaccounted for",
                len(self._running),
            )
        self._drained = True
        if self._drain_deadline is not None and self._drain_deadline.active():
            self._drain_deadline.cancel()
        self._drain_deadline = None
        waiters, self._drain_waiters = self._drain_waiters, []
        for waiter in waiters:
            if waiter.called:
                continue
            with PreserveLoggingContext():
                waiter.callback(None)
