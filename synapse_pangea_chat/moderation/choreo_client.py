"""HTTP client for the choreographer's shared moderation handler.

POST {base_url}/choreo/moderate with a Matrix bearer token (the endpoint is
`has_matrix_account`-gated — any valid token on this homeserver; deployments
configure a dedicated moderation service account's token).

**The transport is Synapse's own shared, pooled agent**, reached as
`ModuleApi.http_client.agent`. That replaces a `twisted.web.client.Agent(reactor)`
built per request, which the repo copied from
`public_courses/course_plan_l2_lookup.py` - a convention that is itself the
bug. `Agent(reactor)` with no explicit pool constructs
`HTTPConnectionPool(reactor, False)`: **non-persistent**, whatever twisted's
own prose says, so every message paid a fresh TCP and TLS handshake. Synapse
builds its agent over a pool sized `max(100 * cache_factor, 5)` per host with a
two-minute cached-connection timeout, on a `@cache_in_self` client shared with
the rest of the process, carrying the proxy configuration and the IP
block/allow list.

**The agent and NOT `SimpleHttpClient`'s own `post_json_get_json` or
`request`.** Both were tried and both are disqualified, for three separate
reasons that are properties of that class rather than of how it is called:

1. `post_json_get_json` reads the body with `twisted.web.client.readBody`,
   which has no size cap and - as its own docstring says - **no timeout at all
   on reading the response body**. A peer that sends headers and then dribbles
   would leave a check pending forever, one per message, with nothing logged
   because nothing has failed. It also logs the request body:
   `logger.debug("HTTP POST %s -> %s", json_str, uri)` puts every moderated
   message into `synapse.http.client` at DEBUG.
2. `request()` wraps its request in its own `timeout_deferred`, whose returned
   Deferred is built with **no canceller**, so cancelling it from outside stops
   our wait and never reaches the socket. A response arriving after our
   deadline is then discarded with no body consumer attached, so the connection
   never returns to the idle pool and the pool's cached-connection timeout
   never applies to it. Those accumulate.
3. `request()`'s own `except` clause logs `e.args[0]`. A malformed status line
   produces a twisted `ResponseFailed` **carrying the bytes off the wire**, so
   a peer that replies `HTTP/1.1 not-a-code @alice:example.org` gets that
   Matrix ID written to `synapse.http.client` by Synapse, through a route no
   format string of ours mentions. The same line raises `IndexError` on an
   argument-less `CancelledError`, which is what our own deadline produces.

Going straight to the agent keeps every one of the reasons the shared client
was chosen - one pool, one set of connections, proxy support, the block list -
while the request Deferred is a real one with a real canceller, so a deadline
here aborts the connection instead of merely giving up on it. The cost is
Synapse's `outgoing_requests_counter`, which this module replaces with its own
latency histogram and outcome counter.
"""

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from synapse.logging.context import PreserveLoggingContext, make_deferred_yieldable
from twisted.internet import defer
from twisted.internet.protocol import Protocol, connectionDone
from twisted.python.failure import Failure
from twisted.web.client import PotentialDataLoss, ResponseDone
from twisted.web.http_headers import Headers

from synapse_pangea_chat.moderation.compat import _SecondsInterval
from synapse_pangea_chat.moderation.log_safety import _severed, scrubbing_logger

logger = scrubbing_logger(
    "synapse.modules.synapse_pangea_chat.moderation.choreo_client"
)

# Bounded so a slow provider can never back up the moderation queue; the
# choreo handler itself fails open well inside this. The budget covers the
# WHOLE exchange - connect, headers and body - not just the part twisted's
# `addTimeout` reaches on its own.
REQUEST_TIMEOUT_SECONDS = 15


# A verdict is a small JSON object. The cap is three orders of magnitude above
# anything the endpoint can legitimately return, and it exists because
# `twisted.web.client.readBody` has no cap at all: a peer that streams forever
# fills the process's memory, and a deadline alone does not stop it - it fires
# the deferred and leaves the body arriving.
MAX_RESPONSE_BYTES = 1024 * 1024

# What went wrong, as a closed vocabulary. The circuit breaker treats these
# differently and must not guess: a 401 that opened the breaker would disable
# moderation until somebody noticed, while a 503 that did not would hammer a
# dead provider once per message.
KIND_TRANSPORT = "transport"
KIND_TIMEOUT = "timeout"
KIND_SERVER_ERROR = "server_error"
KIND_RATE_LIMITED = "rate_limited"
KIND_CONFIG_ERROR = "config_error"
KIND_DECODE = "decode"
KIND_SHAPE = "shape"

FAILURE_KINDS = frozenset(
    {
        KIND_TRANSPORT,
        KIND_TIMEOUT,
        KIND_SERVER_ERROR,
        KIND_RATE_LIMITED,
        KIND_CONFIG_ERROR,
        KIND_DECODE,
        KIND_SHAPE,
    }
)


class ModerationCheckError(Exception):
    """The moderation service could not produce a verdict.

    Carries a reason type and nothing else, and leaves this module with no
    exception chain: `moderate_text` severs it at the boundary. Raising outside
    the local `except` block is not enough on its own - twisted resumes an
    awaiting coroutine from inside ITS active handler, so a parse error
    carrying the raw response line becomes our `__context__` however carefully
    our own frame is arranged - and anything that walks the chain would then
    serialise what the service sent, out of an error whose own message names
    only a type.

    `kind` is drawn from `FAILURE_KINDS` and is the only structured thing on
    it. It is a fixed vocabulary rather than anything derived from the
    response, so it can be used as a metric label and read by the breaker
    without carrying a single byte the service chose.
    """

    def __init__(self, message: str, kind: str = KIND_TRANSPORT) -> None:
        super().__init__(message)
        if kind not in FAILURE_KINDS:
            raise ValueError(f"unknown moderation failure kind {kind!r}")
        self.kind = kind


class _BoundedBody(Protocol):
    """Reads a response body under a size cap, and can be torn down.

    `readBody` is not used, for two reasons that are the same reason. Its
    cancellation path calls `transport.abortConnection()` only `if` the
    transport has one - and the transport an `Agent` response delivers is a
    `TransportProxyProducer`, which has `stopProducing` and `loseConnection`
    and no `abortConnection` at all. So cancelling a `readBody` on a real
    response fires the deferred and leaves the socket open with the body still
    arriving: the check "times out" and the peer keeps sending. And it has no
    size limit, so the same peer can stream until the process dies.

    This reads into a bounded buffer and tears the connection down through the
    methods the proxy actually has.
    """

    def __init__(self, finished: "defer.Deferred[bytes]") -> None:
        self._finished = finished
        self._chunks: List[bytes] = []
        self._length = 0
        self._done = False

    def dataReceived(self, data: bytes) -> None:
        if self._done:
            return
        self._length += len(data)
        if self._length > MAX_RESPONSE_BYTES:
            self._fail(
                ModerationCheckError(
                    f"moderation endpoint returned more than "
                    f"{MAX_RESPONSE_BYTES} bytes",
                    KIND_TRANSPORT,
                )
            )
            return
        self._chunks.append(data)

    def connectionLost(self, reason: Failure = connectionDone) -> None:
        if self._done:
            return
        self._done = True
        if reason.check(ResponseDone, PotentialDataLoss):
            self._finished.callback(b"".join(self._chunks))
        else:
            # The reason is dropped rather than wrapped: a transport failure's
            # own message can quote what was on the wire (ADR-10).
            self._finished.errback(
                ModerationCheckError(
                    f"moderation response body failed: {reason.type.__name__}",
                    KIND_TRANSPORT,
                )
            )

    def abort(self) -> None:
        """Stop the peer sending, which cancelling `readBody` does not do."""
        self._fail(
            ModerationCheckError("moderation response body timed out", KIND_TIMEOUT)
        )

    def tear_down_only(self) -> None:
        """End the exchange and leave the Deferred to somebody else.

        The cancellation path, and the difference from `abort` is the whole
        point of it. `abort` errbacks with a timeout of ours, which PRE-EMPTS
        the `CancelledError` twisted is about to deliver - so a cancelled
        worker saw a moderation failure, absorbed it, and carried on running
        behind a Deferred that had already fired. Here the connection is torn
        down and twisted's own cancellation is left to fire, so the
        cancellation reaches the coroutine that is being stopped.
        """
        if self._done:
            return
        self._done = True
        self._chunks = []
        self._safe_tear_down()

    def _fail(self, error: Exception) -> None:
        if self._done:
            return
        self._done = True
        self._chunks = []
        # Teardown first, but never at the cost of the errback. `_done` is
        # already set, so a teardown that raised used to mean the awaiting
        # coroutine was never resumed AT ALL - a check that neither completes
        # nor fails, one per occurrence, with nothing logged because nothing
        # has failed. Ending the wait is the guarantee; ending the connection
        # is best-effort on top of it.
        self._safe_tear_down()
        self._finished.errback(error)

    def _safe_tear_down(self) -> None:
        try:
            self._tear_down()
        except Exception:
            # silent-ok: a transport failure's own message can quote what was
            # on the wire, so it is named by type only (ADR-10).
            logger.warning("tier2 moderation response teardown failed")

    def _tear_down(self) -> None:
        """Get the peer to stop sending, as hard as the transport allows.

        Typed `Any` because what arrives here is neither an `ITransport` nor an
        `IPushProducer` but twisted's `TransportProxyProducer`, which implements
        a hand-picked part of both. `loseConnection` is a GRACEFUL close: it
        waits for buffered writes and registered producers, so a peer refusing
        to read leaves the socket open. `abortConnection` is the one that does
        not wait, the proxy does not have it, and the real transport underneath
        does - so it is used when it can be reached, with the graceful pair as
        the fallback. Reaching through a private attribute is not something to
        be pleased about; it is here because the alternative is a socket a
        misbehaving peer can hold open.
        """
        transport: Any = self.transport
        if transport is None:
            return
        transport.stopProducing()
        underlying = getattr(transport, "_producer", None)
        abort = getattr(transport, "abortConnection", None) or getattr(
            underlying, "abortConnection", None
        )
        if abort is not None:
            abort()
            return
        transport.loseConnection()


def _read_body(response: Any) -> Tuple["defer.Deferred[bytes]", _BoundedBody]:
    protocol: _BoundedBody

    def _cancel(_deferred: "defer.Deferred[bytes]") -> None:
        # Without a canceller a cancelled read fires the deferred and leaves
        # the connection open with the body still arriving, which is the same
        # defect the timeout was added to fix, reached by a different door.
        #
        # `tear_down_only`, not `abort`: the caller is being CANCELLED, and
        # errbacking with a timeout of ours here would replace the
        # `CancelledError` twisted is about to deliver. The cancellation has
        # to survive the transport, or the worker being stopped absorbs it as
        # an ordinary moderation failure and carries on.
        protocol.tear_down_only()

    finished: "defer.Deferred[bytes]" = defer.Deferred(_cancel)
    protocol = _BoundedBody(finished)
    response.deliverBody(protocol)
    return finished, protocol


def _validated_result(result: Any) -> Dict[str, Any]:
    """The service's response, shape-checked at the trust boundary.

    The caller receives a verdict whose `flagged` is a bool and whose
    `categories` is a list of strings, because those two are the values that go
    on to steer a redaction and to be written into a log line and a room. A
    response is data from a service we do not run, and validating it one frame
    later - or not at all - is how `categories: ["@alice:example.org"]` ends up
    logged verbatim. What the strings MEAN is checked separately, against the
    provider's documented vocabulary, by the caller.
    """
    if not isinstance(result, dict):
        raise ModerationCheckError(
            "moderation endpoint returned a non-object", KIND_SHAPE
        )
    if "flagged" not in result:
        # Absent is not False. A response of `{}`, or an error object the
        # endpoint returns with a 200, was read as "this message is fine" -
        # which is a verdict we were never given, reached by a default.
        raise ModerationCheckError(
            "moderation endpoint returned no verdict", KIND_SHAPE
        )
    flagged = result["flagged"]
    if not isinstance(flagged, bool):
        raise ModerationCheckError(
            "moderation endpoint returned a non-boolean flagged", KIND_SHAPE
        )
    categories = result.get("categories")
    if categories is not None and (
        not isinstance(categories, list)
        or not all(isinstance(category, str) for category in categories)
    ):
        raise ModerationCheckError(
            "moderation endpoint returned categories that are not a list of strings",
            KIND_SHAPE,
        )
    return result


def _status_kind(code: int) -> str:
    """Which failure a non-2xx status is, for the breaker.

    The distinction is load-bearing, not tidiness. A 401 from an expired
    service-account token repeats forever and is our fault; opening the
    breaker on it would disable moderation indefinitely while the state gauge
    blamed the provider. A 5xx or a 429 is the provider, and is exactly what
    the breaker exists to stop hammering.
    """
    if code == 429:
        return KIND_RATE_LIMITED
    if 400 <= code < 500:
        return KIND_CONFIG_ERROR
    return KIND_SERVER_ERROR


async def moderate_text(
    text: str,
    base_url: str,
    access_token: str,
    *,
    agent: Any,
    clock: Any,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Return the choreo ModerationResult dict for ``text``.

    Raises ModerationCheckError on transport/HTTP/decode/shape failure - the
    caller owns the fail-open disposition.

    `agent` is `ModuleApi.http_client.agent`; `clock` is the homeserver's
    `Clock`. Both are passed rather than reached for so a test can drive a
    stalled peer against a clock it controls - with the global reactor the
    stall assertions would be "wait fifteen seconds and hope", and there would
    be no way at all to assert the ABSENCE of a scheduled timeout, which is
    the defect the body deadline exists for.
    """
    try:
        return await _moderate_text(
            text, base_url, access_token, agent, clock, timeout_seconds
        )
    except ModerationCheckError as error:
        # Severed HERE, one frame out from every raise site, and re-raised
        # bare: the interpreter attaches the active exception at raise time, so
        # this is the only place it can be removed for certain. See
        # `log_safety._severed`.
        _severed(error)
        raise


async def _moderate_text(
    text: str,
    base_url: str,
    access_token: str,
    agent: Any,
    clock: Any,
    timeout_seconds: float,
) -> Dict[str, Any]:
    from io import BytesIO

    from twisted.web.client import FileBodyProducer

    body = json.dumps({"text": text}).encode("utf-8")
    headers = Headers(
        {
            b"Authorization": [f"Bearer {access_token}".encode("utf-8")],
            b"Content-Type": [b"application/json"],
        }
    )
    uri = f"{base_url.rstrip('/')}/choreo/moderate".encode("utf-8")

    deadline = clock.time() + timeout_seconds
    # Set by whichever deadline fires, and read by the classifier below. The
    # exception twisted delivers for an aborted request says only that the
    # response never arrived; it cannot say that WE ended it, and a timeout
    # reported as a transport error is a timeout the breaker's cooldown and
    # the operator's dashboard both read as the wrong thing.
    timed_out = [False]
    response: Any = None
    try:
        request = agent.request(b"POST", uri, headers, FileBodyProducer(BytesIO(body)))

        def _abort_headers() -> None:
            timed_out[0] = True
            _from_reactor(request.cancel)
            # Whatever the canceller did or failed to do, OUR wait ends here.
            # A canceller that raises would otherwise leave this coroutine
            # parked with no timer left to save it - a check that neither
            # completes nor fails, holding a worker and, when it is the
            # half-open probe, the breaker's only permit.
            _end_wait(request, "moderation request timed out")

        header_timeout = _arm(
            clock, deadline, _abort_headers, on_failure=_abort_headers
        )
        try:
            response = await make_deferred_yieldable(request)
        finally:
            if header_timeout is not None and header_timeout.active():
                header_timeout.cancel()
        # The body read needs its own deadline, and this is not a
        # belt-and-braces second one. The request deferred fires as soon as the
        # RESPONSE HEADERS arrive, so its timeout is spent by then: a peer that
        # sends headers and then holds the body open leaves the read pending
        # with no timeout scheduled anywhere and no way back. Those checks
        # accumulate, one per message, and nothing is logged, because nothing
        # has failed - the coroutine is simply never resumed. Synapse's own
        # `post_json_get_json` has exactly this hole and says so in its
        # docstring, which is why it is not used.
        body_deferred, body_protocol = _read_body(response)

        def _abort_body() -> None:
            timed_out[0] = True
            _from_reactor(body_protocol.abort)
            _end_wait(body_deferred, "moderation response body timed out")

        # `call_later`, not a deferred timeout: ending the wait is not enough,
        # the connection has to be torn down, and cancelling a read only fires
        # the deferred. See `_BoundedBody`.
        timeout = _arm(clock, deadline, _abort_body, on_failure=_abort_body)
        try:
            raw = await make_deferred_yieldable(body_deferred)
        finally:
            if timeout is not None and timeout.active():
                timeout.cancel()
    except defer.CancelledError:
        # NOT converted. The transport is the last place a cancellation can be
        # turned into an ordinary moderation failure, and turning it into one
        # is how a cancelled worker absorbs its own stop signal and carries on
        # running behind a Deferred that has already fired. It travels up
        # through `ChoreoChecker` and the worker's `_run`, both of which
        # re-raise it for the same reason.
        raise
    except ModerationCheckError:
        # Already classified at the raise site, and left alone. The body
        # reader is the only thing that raises this from inside the block, it
        # knows whether it timed out or lost the connection, and re-labelling
        # it here would flatten that back to one kind.
        raise
    except Exception as e:
        # The chain is severed by `ModerationCheckError` itself, not by this
        # `from None` - see the class, and ADR-10. `from None` alone leaves
        # `__context__` holding the original, and a `json.JSONDecodeError`
        # carries the whole response body on `.doc`.
        raise ModerationCheckError(
            f"moderation request failed: {type(e).__name__}",
            KIND_TIMEOUT if timed_out[0] else _transport_kind(e),
        ) from None

    if response.code >= 400:
        raise ModerationCheckError(
            f"moderation endpoint returned {response.code}",
            _status_kind(response.code),
        )
    try:
        result = json.loads(raw)
    except Exception as e:
        # Not just `JSONDecodeError`: a body that is not valid UTF-8 raises
        # `UnicodeDecodeError`, which is not a subclass of it. That escaped the
        # module entirely, reached `run_as_background_process`, and was logged
        # by `logger.exception` with the undecodable bytes in its args - the
        # response body, in a plaintext log, by a route no format string of
        # ours mentions.
        raise ModerationCheckError(
            f"moderation endpoint returned a body we could not read: "
            f"{type(e).__name__}",
            KIND_DECODE,
        ) from None
    return _validated_result(result)


def _arm(
    clock: Any,
    deadline: float,
    action: Callable[[], None],
    *,
    on_failure: Callable[[], None],
) -> Any:
    """Schedule ``action`` for ``deadline``, or end the exchange now.

    `Clock.call_later` raises once the clock has been shut down, and the
    request has already been issued by the time that happens. Returning a
    failure and walking away would leave the exchange alive with nobody
    reading it and no timer to end it; so if the deadline cannot be scheduled,
    it is applied immediately instead.
    """
    try:
        return clock.call_later(
            _SecondsInterval(max(deadline - clock.time(), 0)), action
        )
    except Exception:
        # silent-ok: the clock is down, which means the process is stopping.
        # The exchange is ended rather than reported on.
        on_failure()
        return None


def _end_wait(deferred: "defer.Deferred[Any]", message: str) -> None:
    """Make sure a deadline ends the WAIT, whatever the teardown managed.

    The teardown is best-effort - a canceller can raise, a transport can
    already be gone - but the coroutine awaiting this Deferred must be
    resumed either way. Without this, a teardown that raised produced a check
    that never completes and never fails: no log, no metric, and a worker held
    for the life of the process.
    """
    if deferred.called:
        return
    with PreserveLoggingContext():
        deferred.errback(ModerationCheckError(message, KIND_TIMEOUT))


def _from_reactor(action: Callable[[], None]) -> None:
    """Run a deadline's teardown from a reactor callback, safely.

    Two things, and neither is decoration.

    `PreserveLoggingContext`: cancelling a request, or aborting a body read,
    fires a Deferred, which resumes the awaiting coroutine from inside THIS
    frame - and that coroutine restores its own logcontext as it goes. Without
    the wrapper the reactor is handed back whatever context the resumed
    coroutine left set, which is the leak class this module's whole handoff
    design is written around, and which Synapse's 1.159 `clock.py` reports as
    "Expected logging context call_later was lost". Synapse wraps its own
    `timeout_deferred` cancellation identically.

    And the `try`: this runs from a `Clock.call_later` callback, where an
    exception is not caught by the coroutine that scheduled it. A teardown
    that raised would leave the deadline half-applied and the exception
    reported by the reactor rather than by us.
    """
    try:
        with PreserveLoggingContext():
            action()
    except Exception:
        # silent-ok: the deadline has already been recorded by the caller's
        # `timed_out` flag, and the awaiting coroutine ends either way - by
        # the teardown that did work, or by the deadline the caller applies.
        # Logging the exception here would name what was on the wire.
        logger.warning("tier2 moderation deadline teardown failed")


def _transport_kind(error: BaseException) -> str:
    """Classify a transport-layer exception without quoting it.

    Only the exception's TYPE NAME is looked at, never its message or args: a
    connection error routinely names the host and a cancellation carries
    whatever the canceller passed. The type name is ours to read; the rest of
    the exception never leaves this function.
    """
    name = type(error).__name__
    if name in ("CancelledError", "TimeoutError", "RequestTimedOutError"):
        return KIND_TIMEOUT
    return KIND_TRANSPORT


def failure_kind(error: BaseException) -> Optional[str]:
    """The failure kind of ``error``, or None if it is not one of ours."""
    kind = getattr(error, "kind", None)
    return kind if isinstance(kind, str) and kind in FAILURE_KINDS else None


class ChoreoChecker:
    """One moderation call, with the breaker and the metrics around it.

    The transport above answers "what did the wire say"; this answers "do we
    have a verdict", which is a different question and the one the caller
    needs. `check` returns a verdict or `None`, and `None` always means the
    same thing: **no verdict, leave the message alone**. Fail-open is not a
    special case here, it is the only failure behaviour - a moderation outage
    must never be a reason a message is held, removed, or delayed.

    Nothing about the response is logged beyond its shape. The endpoint is
    handed private message bodies and answers with categories of its own
    choosing, so a log line that quoted either would move the message into a
    plaintext log by a route no format string of ours mentions.
    """

    def __init__(
        self,
        *,
        agent: Any,
        clock: Any,
        base_url: str,
        access_token: str,
        breaker: Any,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._agent = agent
        self._clock = clock
        self._base_url = base_url
        self._access_token = access_token
        self._breaker = breaker
        self._timeout_seconds = timeout_seconds

    async def check(self, text: str) -> Optional[Dict[str, Any]]:
        from synapse_pangea_chat.moderation import metrics

        refusal, ticket = self._breaker.check()
        if refusal is not None:
            # No HTTP call at all. Counted, because a shed check is a message
            # that went unmoderated and the count is the only thing that says
            # so.
            metrics.record_drop(refusal)
            return None

        try:
            return await self._check_admitted(text, ticket)
        finally:
            # Wrapping EVERYTHING after admission, reporting included. The
            # half-open state admits exactly ONE probe and stays half-open
            # until that probe reports; `release` covers the path where the
            # caller is cancelled and never reports at all, which would
            # otherwise hold the latch and refuse every later check as
            # `breaker_probe_busy` for the life of the process. It is
            # idempotent and a no-op once an outcome has been recorded - but
            # a `finally` placed so that it ran BEFORE the outcome was
            # recorded would reopen the breaker on every successful probe and
            # make the report itself stale. That is not hypothetical; it is
            # what the first version of this function did.
            self._breaker.release(ticket)

    async def _check_admitted(
        self, text: str, ticket: Optional[int]
    ) -> Optional[Dict[str, Any]]:
        from synapse_pangea_chat.moderation import metrics

        started = self._clock.time()
        try:
            try:
                result = await moderate_text(
                    text,
                    base_url=self._base_url,
                    access_token=self._access_token,
                    agent=self._agent,
                    clock=self._clock,
                    timeout_seconds=self._timeout_seconds,
                )
            finally:
                metrics.TIER2_LATENCY.observe(max(self._clock.time() - started, 0.0))
        except defer.CancelledError:
            # NOT caught, and it is the one exception that is not. Everything
            # else here is a moderation failure to be absorbed; a cancellation
            # is somebody asking the WORKER running this check to stop, and
            # swallowing it leaves that worker alive behind a Deferred that
            # has already fired - which is how a pool of eight quietly becomes
            # a pool of twelve. The `finally` above still hands the probe
            # permit back, so the breaker does not wedge either.
            raise
        except Exception as exc:
            # `Exception`, not `ModerationCheckError`, and the widening is the
            # point: whatever escapes this frame reaches
            # `run_as_background_process`, which calls `logger.exception` on
            # it - so an unmapped exception would be logged in full, with
            # whatever it was carrying, by Synapse rather than by us.
            self._record_failure(exc, ticket)
            metrics.record_check("error")
            return None

        if result.get("evaluated") is False:
            # The documented shape of a provider outage. The choreo handler
            # catches `Exception` and answers HTTP 200 with
            # `evaluated: false`, so to a transport breaker an OpenAI outage
            # looks exactly like a healthy server - which is how a breaker
            # counting only transport errors sits closed through the whole
            # thing. `is False` rather than a falsy test: an endpoint that
            # does not send the key at all is an older endpoint, not a failing
            # one, and inventing failures from a missing field would open the
            # breaker against a service that was working.
            self._breaker.record_failure(ticket)
            metrics.record_check("unevaluated")
            logger.warning(
                "tier2 moderation endpoint returned no evaluation; "
                "message left unchecked"
            )
            return None

        self._breaker.record_success(ticket)
        metrics.record_check("flagged" if result.get("flagged") else "clean")
        return result

    def _record_failure(self, exc: BaseException, ticket: Optional[int]) -> None:
        kind = failure_kind(exc)
        if kind == KIND_CONFIG_ERROR:
            # Never opens the breaker. A bad or expired service-account token
            # answers 401 to every request forever; opening on that would
            # disable moderation until a human noticed, with the breaker's own
            # gauge blaming the provider. Logged at most once per cooldown so
            # the signal is steady rather than one ERROR per message.
            if self._breaker.record_config_error(ticket):
                logger.error(
                    "tier2 moderation is rejected by the endpoint (%s); check "
                    "moderation.choreo_access_token and choreo_base_url",
                    kind,
                )
            return
        self._breaker.record_failure(ticket)
        logger.warning(
            "tier2 moderation check unavailable (%s/%s)",
            kind or "unmapped",
            type(exc).__name__,
        )
