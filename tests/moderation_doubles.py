"""Shared doubles for the moderation tests.

A module rather than a copy in each test file. Three test files now need the
same homeserver surface, and the version of it that matters - the clock whose
`call_later` does NOT run its callback inline - is precisely the thing a
second, slightly different copy would get wrong. `tests/base_e2e.py` is the
same idea one level up.
"""

import sqlite3
from functools import lru_cache
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple, cast
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

    def time_msec(self) -> int:
        return int(self.now * 1000)

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


@lru_cache(maxsize=1)
def sqlite_engine() -> Any:
    """Synapse's real SQLite engine, for its real `convert_param_style`.

    A REAL instance and not the unbound method: a stand-in for the converter
    is exactly the thing the previous double got wrong, so this file must not
    contain one.
    """
    from synapse.storage.engines.sqlite import Sqlite3Engine

    return Sqlite3Engine({"args": {"database": ":memory:"}})


class _TransactionDouble:
    """`LoggingTransaction`, reduced to what a module's SQL actually uses.

    SQLite rather than a dictionary, on purpose. A dictionary double proves
    that the store was called; it proves nothing about the statement, so a
    typo, a missing column or a conflict clause an engine rejects would pass
    every test and fail on the first real message.

    **The SQL is run VERBATIM.** An earlier version of this double rewrote
    `%s` into `?` before executing, which is not what Synapse does:
    `Sqlite3Engine.convert_param_style` returns the SQL unchanged and only
    `PostgresEngine` rewrites (`?` into `%s`). The rewrite repaired the
    module's statements on the way past, so Postgres-only SQL passed every
    test here and would have raised `OperationalError: near "%"` on the first
    message of a SQLite deployment. A double that fixes the code it is testing
    is not a test.
    """

    def __init__(self, connection: "sqlite3.Connection") -> None:
        self._cursor = connection.cursor()

    def execute(self, sql: str, args: Any = ()) -> None:
        # Through Synapse's OWN converter, not through a hand-written stand-in
        # for it. That is the coupling the previous double got wrong, and the
        # only way this file can be right about it is to call the same code
        # the real transaction calls.
        self._cursor.execute(sqlite_engine().convert_param_style(sql), tuple(args))

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> Any:
        return self._cursor.fetchall()

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount


class DbPoolDouble:
    """`db_pool`, reduced to `runInteraction` over one in-memory database.

    Shared between two `ChatModeration` instances by a test that needs a
    restart or a second process: that is the whole point of a durable table,
    so a double that cannot be shared cannot test it.
    """

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        #: Set by a test to make the next interaction fail, the way a database
        #: that is down or a table that could not be created would.
        self.error: Optional[Exception] = None
        self.interactions: List[str] = []
        #: Called with each interaction's description as it starts. A hook
        #: rather than a test monkey-patching `runInteraction`: assigning over
        #: a method is a type error, and silencing it would be a suppression
        #: in a file whose whole job is to not need one.
        self.on_interaction: Optional[Callable[[str], None]] = None

    def __del__(self) -> None:
        # An in-memory database left to the collector raises a ResourceWarning
        # into whichever test happened to trigger the collection, which is
        # noise the next reader has to rule out.
        try:
            self.connection.close()
        except Exception:
            pass

    async def runInteraction(
        self, desc: str, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        self.interactions.append(desc)
        if self.on_interaction is not None:
            self.on_interaction(desc)
        if self.error is not None:
            raise self.error
        txn = _TransactionDouble(self.connection)
        result = func(txn, *args, **kwargs)
        self.connection.commit()
        return result


class EventStoreDouble:
    def __init__(self, redacted: bool = False, missing: bool = False) -> None:
        self.redacted = redacted
        self.missing = missing
        self.error: Optional[Exception] = None
        self.reads: List[str] = []
        self.db_pool = DbPoolDouble()
        #: Called on every read, so a test can assert the ORDER of the
        #: re-read against the disposition claim.
        self.on_read: Optional[Callable[[], None]] = None

    async def get_event(self, event_id: str, allow_none: bool = False) -> Any:
        self.reads.append(event_id)
        if self.on_read is not None:
            self.on_read()
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


def http_client_double(
    *,
    http_proxy: Optional[str] = None,
    https_proxy: Optional[str] = None,
    no_proxy: Optional[str] = None,
) -> Any:
    """`ModuleApi.http_client`, with the proxy surface Tier 2 inspects.

    A real object rather than whatever `create_autospec` invents, because the
    thing under test is a NEGATIVE: Tier 2 refuses to start when a proxy sits
    in front of the moderation endpoint. An autospec mock answers every
    attribute with another mock, so `https_proxy_endpoint` is truthy on a
    double that was meant to represent a homeserver with no proxy at all - the
    check would refuse in every test and the tests would have to be written
    around it rather than against it.
    """
    return SimpleNamespace(
        agent=SimpleNamespace(
            http_proxy_endpoint=object() if http_proxy else None,
            https_proxy_endpoint=object() if https_proxy else None,
            proxy_config=SimpleNamespace(
                http_proxy=http_proxy,
                https_proxy=https_proxy,
                no_proxy_hosts=[] if no_proxy is None else no_proxy.split(","),
                get_proxies_dictionary=lambda: {
                    key: value
                    for key, value in (
                        ("http", http_proxy),
                        ("https", https_proxy),
                        ("no", no_proxy),
                    )
                    if value
                },
            ),
        )
    )


def module_api(
    homeserver: Optional[HomeServerDouble] = None,
    *,
    run_background_tasks: bool = True,
    http_client: Optional[Any] = None,
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
    api.http_client = http_client_double() if http_client is None else http_client
    return cast(ModuleApi, api)


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


class Tier2MatcherProbe:
    """Runs text through the PRODUCTION Tier-2 handler and reports what the
    deterministic wordlist matcher recorded.

    The coverage claim - "everything Tier 1 stopped blocking is caught by
    Tier 2" - used to be asserted by calling `profanity.contains_profanity`
    from the test. Nothing in production called that function, so the claim
    was about a helper rather than about the system: stubbing the real
    `ChoreoChecker.check` to return None left the test green while Tier 2 was
    LLM-only. The matcher is wired now, and this probe is what makes the test
    exercise the wiring: delete the call site and every assertion made through
    here fails.

    The service is stubbed CLEAN on purpose. That is the case the agreement
    matrix exists to measure - the model said nothing and our wordlist did -
    and it is the one that would be invisible if the test asked the matcher
    directly.
    """

    _METRIC = "pangea_moderation_tier2_matcher_agreement_total"

    def __init__(self, test: Any) -> None:
        import asyncio
        from unittest.mock import create_autospec, patch

        from synapse_pangea_chat.config import PangeaChatConfig
        from synapse_pangea_chat.moderation import ChatModeration
        from synapse_pangea_chat.moderation.choreo_client import moderate_text
        from synapse_pangea_chat.moderation.dispatch import ModerationJob

        self._asyncio = asyncio
        self._ModerationJob = ModerationJob
        self._homeserver = HomeServerDouble()
        module = ChatModeration(
            module_api(self._homeserver),
            PangeaChatConfig(
                cms_base_url="http://cms.invalid",
                cms_service_api_key="k",
                moderation_tier1_enabled=False,
                moderation_tier2_enabled=True,
                moderation_choreo_base_url="http://choreo.invalid",
                moderation_choreo_access_token="syt_test",
            ),
        )
        self._module = module
        test.addCleanup(self._stop)

        async def _clean(*_args: Any, **_kwargs: Any) -> Any:
            return {"flagged": False, "categories": [], "evaluated": True}

        patcher = patch(
            "synapse_pangea_chat.moderation.choreo_client.moderate_text",
            create_autospec(moderate_text, side_effect=_clean),
        )
        patcher.start()
        test.addCleanup(patcher.stop)
        self._reader = MetricReader()
        self._sequence = 0

    def _stop(self) -> None:
        dispatcher = self._module._dispatcher
        if dispatcher is None:
            return
        dispatcher._stopping = True
        dispatcher._wake_all()

    def matcher_hit(self, text: str) -> bool:
        """True when the production Tier-2 path recorded a matcher hit."""
        self._sequence += 1
        job = self._ModerationJob(
            event_id=f"$probe{self._sequence}",
            room_id="!room:example.org",
            sender="@learner:example.org",
            text=text,
            enqueued_at=0.0,
        )
        self._reader.snapshot(self._METRIC, service="clean", matcher="hit")
        self._asyncio.run(self._module._check_and_redact(job))
        return self._reader.delta(self._METRIC, service="clean", matcher="hit") > 0
