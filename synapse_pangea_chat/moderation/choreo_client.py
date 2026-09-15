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

**Two limits of the HTTPS-proxy path, which no choice of client avoids.** Both
are properties of `synapse.http.proxyagent` / `connectproxyclient`, reached
identically through `SimpleHttpClient.request`, and both are recorded here
rather than left to be rediscovered:

- During proxy CONNECT the agent waits on `HTTPProxiedClientFactory.on_connection`,
  a Deferred with **no canceller**. Our deadline therefore ends the wait
  without closing the proxy socket, so a proxy that accepts connections and
  stalls the CONNECT leaks one socket per check. The deadline still bounds the
  check, the breaker still opens on the resulting timeouts, and the drop is
  counted - what is not bounded is the socket.
- `HTTPConnectSetupClient.handleStatus` logs the CONNECT **reason phrase
  verbatim** at DEBUG on `synapse.http.connectproxyclient`, which is outside
  this module's logger namespace and so outside its scrubbing. A hostile or
  compromised HTTPS proxy replying `HTTP/1.1 200 @alice:example.org` gets that
  string into the log of a deployment running that logger at DEBUG.

Neither is reachable without an HTTPS proxy configured for outbound requests.
The second is closed here, in `install_proxy_log_guard`, by filtering that one
record out before any handler sees it - a logging filter needs no change to
Synapse. The first is not: closing it means supplying this module's own CONNECT
endpoint and factory so the handshake Deferred has a canceller, which is a
re-implementation of Synapse's proxy client carrying its TLS verification and
IP policy with it. That is a real fix and it is available; it is out of
proportion to a transport change, and it is recorded here as a deliberate
scope decision rather than as an impossibility.
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from synapse.logging.context import PreserveLoggingContext, make_deferred_yieldable
from twisted.internet import defer
from twisted.internet.protocol import Protocol, connectionDone
from twisted.python.failure import Failure
from twisted.web.client import PotentialDataLoss, ResponseDone
from twisted.web.http_headers import Headers

from synapse_pangea_chat.moderation.compat import (
    _SecondsInterval,
    reraise_if_cancelled,
)
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
        #
        # `CancelledError` deliberately passes through without being caught,
        # and that does not weaken the guarantee this clause exists for: a
        # cancellation carries no response, no body and no decoded value - it
        # is raised by twisted with nothing of the service's in it - so there
        # is no payload for a chain to leak. What it does carry is the fact
        # that the worker running this check was asked to stop, which has to
        # reach that worker.
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
        response = await _with_deadline(
            clock,
            deadline,
            request,
            teardown=request.cancel,
            timed_out=timed_out,
            message="moderation request timed out",
        )
        # The status is read BEFORE the body, because the two answer different
        # questions and the body can fail first. A 401 whose body then stalls
        # was reported as a timeout, which opens the breaker - and a bad
        # service-account token is the one failure that must never open it,
        # because it repeats forever and opening would disable moderation
        # until somebody noticed.
        if response.code >= 400:
            _drain_unwanted_body(response)
            raise ModerationCheckError(
                f"moderation endpoint returned {response.code}",
                _status_kind(response.code),
            )
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
        raw = await _with_deadline(
            clock,
            deadline,
            body_deferred,
            teardown=body_protocol.abort,
            timed_out=timed_out,
            message="moderation response body timed out",
        )
    except ModerationCheckError:
        # Already classified at the raise site, and left alone. The body
        # reader is the only thing that raises this from inside the block, it
        # knows whether it timed out or lost the connection, and re-labelling
        # it here would flatten that back to one kind.
        raise
    except Exception as e:
        reraise_if_cancelled(e)
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


async def _with_deadline(
    clock: Any,
    deadline: float,
    source: "defer.Deferred[Any]",
    *,
    teardown: Callable[[], None],
    timed_out: List[bool],
    message: str,
) -> Any:
    """Await ``source``, but never past ``deadline``.

    This is Synapse's `timeout_deferred` pattern, hand-rolled for two reasons:
    that function is keyword-only on one supported pin and positional on the
    other, and it only ends the WAIT, where a stalled HTTP exchange also has to
    be torn down.

    **The wait happens on a Deferred we own, and the source is never fired by
    us.** That is the whole design, and both halves are load-bearing:

    - Forcing an errback onto the agent's own Deferred means its producer can
      fire it again later, which is an `AlreadyCalledError` out of the
      reactor - a response arriving after our deadline is exactly the case the
      deadline exists for, so that is not a rare path.
    - `source.called` is not "finished". A real `Agent` request Deferred is
      already `called` and merely paused while the response is awaited, so a
      guard on `called` would decline to end a wait that was genuinely stuck.

    Our own Deferred has neither problem: nothing else can fire it, and
    `called` on it means exactly what it says.

    The teardown is best-effort; ending the wait is not. A canceller that
    raises, or a transport that is already gone, must not leave the caller
    parked with its deadline spent - that is a check which neither completes
    nor fails, holding a worker and, when it is the half-open probe, the
    breaker's only permit, for the life of the process.
    """
    # Set while twisted is cancelling us, and read by `_forward`. This is the
    # ONE place the module keeps cancellation from being turned into an
    # ordinary moderation failure, and it is here because this is where the
    # conversion happened: tearing the exchange down makes the transport
    # errback with `ResponseNeverReceived` or with the body reader's own
    # timeout, `_forward` fired our Deferred with it, and twisted's
    # `CancelledError` was then suppressed as an already-called Deferred. The
    # caller saw a moderation failure, absorbed it by contract, and never
    # learned it had been asked to stop.
    cancelling = [False]
    # Set while OUR OWN deadline is expiring, and read by `_forward` for the
    # same reason as `cancelling` - but against the opposite mistake.
    # `_expire` tears the exchange down, and some teardowns errback the source
    # with a bare `CancelledError`: Synapse's proxy CONNECT waits on a
    # Deferred with no canceller, so `request.cancel()` there produces exactly
    # that. Forwarded, it reached `reraise_if_cancelled`, which treated our
    # own timeout as somebody stopping the worker - the worker died, the
    # breaker never saw the failure, and a stalled proxy looked like a healthy
    # endpoint with a shrinking pool. Suppressing the forward means only
    # `_expire`'s own errback can fire `own`, so a deadline is a timeout
    # whatever the teardown produced.
    expiring = [False]

    def _on_cancel(_own: "defer.Deferred[Any]") -> None:
        # Cancellation has to end the EXCHANGE, not just our wait. Without a
        # canceller, cancelling this Deferred fires it and leaves the
        # connection open with the body still arriving - the same defect the
        # deadline exists to fix, reached through a different door.
        cancelling[0] = True
        _from_reactor(teardown)

    own: "defer.Deferred[Any]" = defer.Deferred(_on_cancel)

    def _forward(result: Any) -> Any:
        if not own.called and not cancelling[0] and not expiring[0]:
            own.callback(result)
        # The source's result is consumed here; returning None stops twisted
        # reporting an unhandled failure on a source we have finished with -
        # including the one the teardown above provokes.
        return None

    source.addBoth(_forward)

    def _expire() -> None:
        timed_out[0] = True
        expiring[0] = True
        try:
            _from_reactor(teardown)
        finally:
            # In a `finally`, because `expiring` has already suppressed the
            # forward by this point: if the teardown gets out of this frame
            # without the errback running, the caller is parked with its
            # deadline spent AND every later result discarded - a check that
            # can no longer complete by any route. Ending the wait is the
            # guarantee; the teardown is best-effort under it.
            if not own.called:
                with PreserveLoggingContext():
                    own.errback(ModerationCheckError(message, KIND_TIMEOUT))

    try:
        timer: Any = clock.call_later(
            _SecondsInterval(max(deadline - clock.time(), 0)), _expire
        )
    except Exception:
        # `Clock.call_later` raises once the clock has been shut down, and the
        # exchange is already open by then. Reporting a failure and walking
        # away would leave it alive with nobody reading it and no timer to end
        # it, so the deadline is applied immediately instead.
        _expire()
        timer = None

    try:
        return await make_deferred_yieldable(own)
    finally:
        if timer is not None and timer.active():
            timer.cancel()


def _drain_unwanted_body(response: Any) -> None:
    """Consume and discard the body of a response we are refusing on status.

    A response whose body is never read holds its connection open and keeps it
    out of the pool, so the shared agent leaks one connection per error
    response - and an endpoint answering 401 to every request answers a lot of
    them.
    """
    _finished, protocol = _read_body(response)
    protocol.tear_down_only()


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
        # `reraise_if_cancelled` is deliberately NOT called here, and this is
        # the one place in the module where that is right. This runs from a
        # reactor callback: there is no coroutine in this frame for a
        # cancellation to stop, and letting one out would abandon the caller
        # mid-deadline rather than ending its wait. The rule's own criterion
        # says the same thing - it applies to handlers wrapping an `await`,
        # and there is none here.
        #
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
        except Exception as exc:
            reraise_if_cancelled(exc)
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


# The exact format string `HTTPConnectSetupClient.handleStatus` logs. Matching
# on it is deliberate and so is its fragility: if a Synapse upgrade changes the
# string, the guard stops matching and `test_the_proxy_log_guard_matches_the
# _installed_synapse` fails, which is a failure somebody sees. Matching more
# loosely - every record from that logger - would drop connection diagnostics
# an operator needs, to protect against one of them.
_PROXY_STATUS_LOG_FORMAT = "Got Status: %s %s %s"
_PROXY_LOGGER_NAME = "synapse.http.connectproxyclient"


class _ProxyStatusFilter(logging.Filter):
    """Keeps a proxy's own CONNECT reason phrase out of the log.

    `HTTPConnectSetupClient.handleStatus` logs the status line **verbatim** at
    DEBUG, and the reason phrase is a string the proxy chooses: an HTTPS proxy
    replying `HTTP/1.1 200 @alice:example.org` puts that Matrix ID into the log
    of any deployment running this logger at DEBUG. It is reached identically
    through every Synapse HTTP client, so the transport choice does not avoid
    it - but a logging filter is ours to install and needs no change to
    Synapse.

    The record is redacted rather than dropped, so an operator debugging a
    proxy still sees that a status arrived and what its code was. Only the
    phrase the proxy wrote is removed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg != _PROXY_STATUS_LOG_FORMAT:
            return True
        args = record.args
        if isinstance(args, tuple) and len(args) == 3:
            status, _message, version = args
            record.args = (status, b"<redacted>", version)
        else:
            record.args = ()
            record.msg = "Got Status: <redacted>"
        return True


def install_proxy_log_guard() -> None:
    """Attach `_ProxyStatusFilter`, once, to Synapse's proxy client logger.

    A filter on the LOGGER rather than on a handler: a handler's filters run
    only for that handler, and the deployment owns its handlers. Idempotent,
    because Tier 2 may be constructed more than once in a process - the test
    suite does exactly that.
    """
    proxy_logger = logging.getLogger(_PROXY_LOGGER_NAME)
    if any(isinstance(f, _ProxyStatusFilter) for f in proxy_logger.filters):
        return
    proxy_logger.addFilter(_ProxyStatusFilter())
