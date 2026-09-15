"""Shared doubles for the moderation tests.

A module rather than a copy in each test file. Three test files now need the
same homeserver surface, and the version of it that matters - the clock whose
`call_later` does NOT run its callback inline - is precisely the thing a
second, slightly different copy would get wrong. `tests/base_e2e.py` is the
same idea one level up.
"""

from types import SimpleNamespace
from typing import Any, Callable, List, Optional, Tuple, cast
from unittest.mock import create_autospec

from synapse.module_api import ModuleApi


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


class RecordingDelayedCall:
    def __init__(self, clock: "RecordingClock", seq: int) -> None:
        self._clock = clock
        self.seq = seq

    def active(self) -> bool:
        return any(entry[1] == self.seq for entry in self._clock.pending)

    def cancel(self) -> None:
        self._clock.pending = [
            entry for entry in self._clock.pending if entry[1] != self.seq
        ]


class RecordingClock:
    """Synapse's `Clock`, reduced to `time`, `call_later` and `looping_call`.

    `call_later` records rather than runs. Running the callback inline would
    hide the reactor boundary the Tier-2 handoff is built on, and every test
    that depends on "enqueue does no consumer work" would pass against a
    design that did it inline.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.pending: List[Tuple[float, int, Callable[..., Any], tuple, bool]] = []
        self.looping: List[Tuple[Callable[..., Any], float]] = []
        self._seq = 0
        self.shutdown = False

    def time(self) -> float:
        return self.now

    def call_later(
        self, delay: Any, callback: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> RecordingDelayedCall:
        if self.shutdown:
            raise Exception("Cannot start delayed call. Clock has been shutdown")
        return self._schedule(delay, callback, args, wrapped=True)

    def reactor_call_later(
        self, delay: float, callback: Callable[..., Any], *args: Any
    ) -> RecordingDelayedCall:
        """`reactor.callLater`, which does NOT wrap the callback in a
        logcontext the way `synapse.util.Clock.call_later` does."""
        return self._schedule(delay, callback, args, wrapped=False)

    def _schedule(
        self, delay: Any, callback: Any, args: Any, *, wrapped: bool
    ) -> RecordingDelayedCall:
        self._seq += 1
        self.pending.append(
            (self.now + float(delay), self._seq, callback, args, wrapped)
        )
        return RecordingDelayedCall(self, self._seq)

    def looping_call(
        self, f: Callable[..., Any], interval: Any, *args: Any, **kwargs: Any
    ) -> Any:
        self.looping.append((f, float(interval)))
        return object()

    def run_pending(self, limit: int = 200) -> int:
        fired = 0
        for _ in range(limit):
            due = [entry for entry in self.pending if entry[0] <= self.now]
            if not due:
                break
            due.sort(key=lambda entry: (entry[0], entry[1]))
            entry = due[0]
            self.pending.remove(entry)
            if entry[4]:
                _fire_as_synapse_would(entry[2], entry[3], {})
            else:
                entry[2](*entry[3])
            fired += 1
        return fired

    def drain(self) -> None:
        for _ in range(20):
            if not self.run_pending():
                return

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.drain()


class StoredEvent:
    """What `get_event` gives back, as far as the redaction guard reads it."""

    def __init__(self, redacted: bool = False) -> None:
        self.internal_metadata = SimpleNamespace(is_redacted=lambda: redacted)


class EventStoreDouble:
    def __init__(self, redacted: bool = False, missing: bool = False) -> None:
        self.redacted = redacted
        self.missing = missing
        self.error: Optional[Exception] = None
        self.reads: List[str] = []

    async def get_event(self, event_id: str, allow_none: bool = False) -> Any:
        self.reads.append(event_id)
        if self.error is not None:
            raise self.error
        if self.missing:
            return None
        return StoredEvent(redacted=self.redacted)


class HomeServerDouble:
    """The homeserver surface the moderation module reaches through."""

    hostname = "example.org"

    def __init__(self, store: Optional[EventStoreDouble] = None) -> None:
        self.clock = RecordingClock()
        self.store = store if store is not None else EventStoreDouble()
        self.shutdown_handlers: List[Any] = []

    def get_clock(self) -> RecordingClock:
        return self.clock

    def get_reactor(self) -> Any:
        # The drain deadline is taken from the REACTOR, not from the Clock,
        # because `Clock.shutdown()` cancels everything the Clock tracks. The
        # double keeps both on one timebase so a test can still step it, while
        # firing reactor callbacks bare the way the reactor does.
        return SimpleNamespace(callLater=self.clock.reactor_call_later)

    def get_datastores(self) -> Any:
        return SimpleNamespace(main=self.store)

    def register_async_shutdown_handler(
        self, *, phase: str, eventType: str, shutdown_func: Any
    ) -> None:
        assert phase == "before" and eventType == "shutdown"
        self.shutdown_handlers.append(shutdown_func)


def module_api(
    homeserver: Optional[HomeServerDouble] = None,
    *,
    run_background_tasks: bool = True,
) -> ModuleApi:
    """A `ModuleApi` double that checks the signature of every call.

    `create_autospec`, not `MagicMock`: an unrestricted mock accepts every
    call, so a test written against one asserts that our code called
    SOMETHING, not that it called the thing correctly.

    `_hs` is attached by hand because it is set in `ModuleApi.__init__`
    rather than declared on the class, so autospec cannot know about it.
    """
    api = create_autospec(ModuleApi, instance=True)
    api._hs = homeserver if homeserver is not None else HomeServerDouble()
    api.should_run_background_tasks.return_value = run_background_tasks
    return cast(ModuleApi, api)
