"""HTTP client for the choreographer's shared moderation handler.

POST {base_url}/choreo/moderate with a Matrix bearer token (the endpoint is
`has_matrix_account`-gated — any valid token on this homeserver; deployments
configure a dedicated moderation service account's token).

**The transport is Synapse's own shared client**, reached as
`ModuleApi.http_client`. That replaces a `twisted.web.client.Agent(reactor)`
built per request, which the repo copied from
`public_courses/course_plan_l2_lookup.py` - a convention that is itself the
bug. `Agent(reactor)` with no explicit pool constructs
`HTTPConnectionPool(reactor, False)`: **non-persistent**, whatever twisted's
own prose says, so every message paid a fresh TCP and TLS handshake. Synapse's
client owns a pool sized `max(100 * cache_factor, 5)` per host with a
two-minute cached-connection timeout, is a `@cache_in_self` singleton shared
with the rest of the process, carries proxy configuration and the IP
block/allow list, and increments Synapse's `outgoing_requests_counter`.

**`request()`, and deliberately not `post_json_get_json`.** The convenience
method cannot meet this module's two hard requirements. It reads the body with
`twisted.web.client.readBody`, which has no size cap and - as its own
docstring says - **no timeout at all on reading the response body**; a peer
that sends headers and then dribbles would leave a check pending forever, one
per message, with nothing logged because nothing has failed. And it logs the
request body: `logger.debug("HTTP POST %s -> %s", json_str, uri)` puts every
moderated message into `synapse.http.client` at DEBUG. `request()` does
neither - it logs only the method and a redacted URI, and because Synapse asks
treq for an unbuffered response it hands back the raw `IResponse`, so the
bounded, deadline-enforced, connection-tearing body reader below stays exactly
as it was.

**The one cost of the choice, stated rather than discovered later.**
`SimpleHttpClient.request` wraps its own request in `timeout_deferred`, whose
returned Deferred is created with no canceller, so cancelling it from outside
does not reach the socket. Our deadline therefore ends *our* wait during the
header phase without aborting the connection; that connection is reclaimed by
Synapse's own 60-second request timeout and the pool's 120-second cached
connection timeout instead of by us. It is bounded and it is one connection.
Once the response headers are in hand the body phase is ours again, and there
the deadline does tear the connection down - which is the phase that had no
bound of any kind and the reason this reader exists.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

from synapse.logging.context import make_deferred_yieldable, run_in_background
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

    def _fail(self, error: Exception) -> None:
        if self._done:
            return
        self._done = True
        self._chunks = []
        self._tear_down()
        self._finished.errback(error)

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
        protocol.abort()

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
    http_client: Any,
    clock: Any,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Return the choreo ModerationResult dict for ``text``.

    Raises ModerationCheckError on transport/HTTP/decode/shape failure - the
    caller owns the fail-open disposition.

    `http_client` is `ModuleApi.http_client`; `clock` is the homeserver's
    `Clock`. Both are passed rather than reached for so a test can drive a
    stalled peer against a clock it controls - with the global reactor the
    stall assertions would be "wait fifteen seconds and hope", and there would
    be no way at all to assert the ABSENCE of a scheduled timeout, which is
    the defect the body deadline exists for.
    """
    try:
        return await _moderate_text(
            text, base_url, access_token, http_client, clock, timeout_seconds
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
    http_client: Any,
    clock: Any,
    timeout_seconds: float,
) -> Dict[str, Any]:
    body = json.dumps({"text": text}).encode("utf-8")
    headers = Headers(
        {
            b"Authorization": [f"Bearer {access_token}".encode("utf-8")],
            b"Content-Type": [b"application/json"],
        }
    )
    uri = f"{base_url.rstrip('/')}/choreo/moderate"

    deadline = clock.time() + timeout_seconds
    response: Any = None
    try:
        # `run_in_background`, never a bare `defer.ensureDeferred`. The
        # difference is the whole of commit 33f7ead: a bare `ensureDeferred`
        # leaves this coroutine's logcontext set on the reactor, and since
        # 1.159 `clock.py` asserts the sentinel context at every timer fire and
        # permanently kills any Synapse timer that fires inside the leaked
        # window. A Deferred is needed at all - rather than awaiting the
        # coroutine directly - because there is nothing to attach a deadline to
        # otherwise.
        request = run_in_background(
            http_client.request, "POST", uri, data=body, headers=headers
        )
        header_timeout = clock.call_later(
            _SecondsInterval(max(deadline - clock.time(), 0)),
            _cancel_from_reactor,
            request,
        )
        try:
            response = await make_deferred_yieldable(request)
        finally:
            if header_timeout.active():
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
        # `call_later`, not a deferred timeout: ending the wait is not enough,
        # the connection has to be torn down, and cancelling a read only fires
        # the deferred. See `_BoundedBody`.
        timeout = clock.call_later(
            _SecondsInterval(max(deadline - clock.time(), 0)),
            body_protocol.abort,
        )
        try:
            raw = await make_deferred_yieldable(body_deferred)
        finally:
            if timeout.active():
                timeout.cancel()
    except ModerationCheckError:
        # Already classified at the raise site - the body reader knows whether
        # it timed out or lost the connection, and re-wrapping here would
        # flatten that back to `transport`.
        raise
    except Exception as e:
        # The chain is severed by `ModerationCheckError` itself, not by this
        # `from None` - see the class, and ADR-10. `from None` alone leaves
        # `__context__` holding the original, and a `json.JSONDecodeError`
        # carries the whole response body on `.doc`.
        raise ModerationCheckError(
            f"moderation request failed: {type(e).__name__}",
            _transport_kind(e),
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


def _cancel_from_reactor(deferred: "defer.Deferred[Any]") -> None:
    """Cancel an in-flight request from a reactor callback.

    `PreserveLoggingContext` is not decoration. Cancelling resumes the
    awaiting coroutine from inside this callback's frame, and that coroutine
    restores its own logcontext when it does - so without the wrapper the
    reactor is handed back whatever context the resumed coroutine left set,
    which is the leak class this module's whole handoff design is written
    around. Synapse's own `timeout_deferred` wraps its `cancel()` the same
    way, for the same reason.

    What this cancellation does NOT do is reach the socket.
    `SimpleHttpClient.request` wraps its request in its own `timeout_deferred`,
    whose returned Deferred is built with no canceller, so the cancel stops at
    that wrapper. See this module's docstring: the connection is reclaimed by
    Synapse's own 60-second request timeout rather than by us, and the wait -
    which is what a queued moderation job actually occupies - ends here.
    """
    from synapse.logging.context import PreserveLoggingContext

    if deferred.called:
        return
    with PreserveLoggingContext():
        deferred.cancel()


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
        http_client: Any,
        clock: Any,
        base_url: str,
        access_token: str,
        breaker: Any,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._http_client = http_client
        self._clock = clock
        self._base_url = base_url
        self._access_token = access_token
        self._breaker = breaker
        self._timeout_seconds = timeout_seconds

    async def check(self, text: str) -> Optional[Dict[str, Any]]:
        from synapse_pangea_chat.moderation import metrics

        refusal = self._breaker.check()
        if refusal is not None:
            # No HTTP call at all. Counted, because a shed check is a message
            # that went unmoderated and the count is the only thing that says
            # so.
            metrics.record_drop(refusal)
            return None

        started = self._clock.time()
        try:
            result = await moderate_text(
                text,
                base_url=self._base_url,
                access_token=self._access_token,
                http_client=self._http_client,
                clock=self._clock,
                timeout_seconds=self._timeout_seconds,
            )
        except Exception as exc:
            # `Exception`, not `ModerationCheckError`, and the widening is the
            # point: whatever escapes this frame reaches
            # `run_as_background_process`, which calls `logger.exception` on
            # it - so an unmapped exception would be logged in full, with
            # whatever it was carrying, by Synapse rather than by us.
            self._record_failure(exc)
            metrics.record_check("error")
            return None
        finally:
            metrics.TIER2_LATENCY.observe(max(self._clock.time() - started, 0.0))

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
            self._breaker.record_failure()
            metrics.record_check("unevaluated")
            logger.warning(
                "tier2 moderation endpoint returned no evaluation; "
                "message left unchecked"
            )
            return None

        self._breaker.record_success()
        metrics.record_check("flagged" if result.get("flagged") else "clean")
        return result

    def _record_failure(self, exc: BaseException) -> None:
        kind = failure_kind(exc)
        if kind == KIND_CONFIG_ERROR:
            # Never opens the breaker. A bad or expired service-account token
            # answers 401 to every request forever; opening on that would
            # disable moderation until a human noticed, with the breaker's own
            # gauge blaming the provider. Logged at most once per cooldown so
            # the signal is steady rather than one ERROR per message.
            if self._breaker.record_config_error():
                logger.error(
                    "tier2 moderation is rejected by the endpoint (%s); check "
                    "moderation.choreo_access_token and choreo_base_url",
                    kind,
                )
            return
        self._breaker.record_failure()
        logger.warning(
            "tier2 moderation check unavailable (%s/%s)",
            kind or "unmapped",
            type(exc).__name__,
        )
