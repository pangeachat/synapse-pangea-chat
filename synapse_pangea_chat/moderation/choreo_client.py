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
  stalls the CONNECT leaks one socket per check, without bound: the breaker's
  half-open probe opens another every cooldown, for as long as the process
  runs. The deadline bounds the check and the drop is counted; what is not
  bounded is the file descriptor, and running out of those takes the
  homeserver down rather than merely moderation.

  **So Tier 2 refuses to run through a proxy at all** - see
  `assert_no_proxy_in_front_of`. Closing it inside the transport means
  supplying this module's own CONNECT endpoint and factory, carrying
  Synapse's TLS verification and IP policy with it, which is a
  re-implementation of Synapse's proxy client. Bounding it instead - a cap on
  outstanding connection attempts - bounds the descriptors and leaves Tier 2
  permanently wedged once the cap fills, which is the same outage arrived at
  slowly. Refusing costs a proxied deployment either a `no_proxy` entry for
  the moderation host or Tier 2, and it says so at startup rather than
  running out of sockets in a week.
- `HTTPConnectSetupClient.handleStatus` logs the CONNECT **reason phrase
  verbatim** at DEBUG on `synapse.http.connectproxyclient`, which is outside
  this module's logger namespace and so outside its scrubbing. A hostile or
  compromised HTTPS proxy replying `HTTP/1.1 200 @alice:example.org` gets that
  string into the log of a deployment running that logger at DEBUG.

Neither is reachable without a proxy configured for outbound requests. The
second is closed in `install_proxy_log_guard`, which drops the whole status
line before any handler sees it - a logging filter needs no change to Synapse.
The first is closed by not running there at all.
"""

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, cast

import attr
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
class ModerationProxyUnsupportedError(Exception):
    """Tier 2 would reach the moderation endpoint through an HTTP proxy.

    A startup failure rather than a runtime degradation, because what it
    prevents is silent and cumulative: see the two proxy limits in the module
    docstring. An operator fixes it by exempting the moderation host from the
    proxy (`no_proxy`) or by turning Tier 2 off; either way they find out now.
    """


def assert_no_proxy_in_front_of(agent: Any, base_url: str) -> None:
    """Raise unless `base_url` is reached without a proxy.

    Asked of the AGENT, so it reflects the configuration the requests will
    actually use rather than a second reading of the environment. The
    uncertain case raises: if the agent does not present a proxy
    configuration we recognise, we cannot establish that nothing is in the
    way, and the failure this guards against does not announce itself.
    """
    from urllib.parse import urlsplit

    proxied = _proxy_agent(agent)
    if proxied is None:
        raise ModerationProxyUnsupportedError(
            "tier 2 moderation cannot establish whether its HTTP agent uses a "
            "proxy, and a proxied CONNECT leaks a socket per check; refusing "
            "to start. Set moderation.tier2_enabled to false, or report the "
            f"agent type {type(agent).__name__}"
        )
    split = urlsplit(base_url)
    host = split.hostname or ""
    endpoint = (
        proxied.https_proxy_endpoint
        if split.scheme == "https"
        else proxied.http_proxy_endpoint
    )
    if endpoint is None:
        return
    if _bypasses_proxy(proxied, host):
        return
    raise ModerationProxyUnsupportedError(
        f'Config "moderation.choreo_base_url" ({base_url}) would be reached '
        "through an HTTP proxy, and Synapse's proxy CONNECT leaks one socket "
        "per stalled handshake with no way for this module to close it. Add "
        "the moderation host to no_proxy, or set "
        "moderation.tier2_enabled to false"
    )


def _proxy_agent(agent: Any) -> Any:
    """The `ProxyAgent` behind whatever wrappers the client put in front.

    `SimpleHttpClient` wraps its agent in `BlocklistingAgentWrapper` when an
    IP blocklist is configured, and a deployment may wrap it further. Bounded
    so a cyclic or self-referential wrapper cannot spin.
    """
    for _ in range(8):
        if agent is None:
            return None
        if hasattr(agent, "http_proxy_endpoint") and hasattr(
            agent, "https_proxy_endpoint"
        ):
            return agent
        agent = getattr(agent, "_agent", None)
    return None


def _bypasses_proxy(proxied: Any, host: str) -> bool:
    """Does the agent's own `no_proxy` exempt this host?

    Through Synapse's own helper, so `no_proxy` means here exactly what it
    means to the request that follows. An error deciding it is not a bypass:
    the safe answer to "we could not tell" is the one that refuses.
    """
    if not host:
        return False
    proxies = _saved_proxies(proxied)
    if proxies is None:
        # No configuration we can read means no bypass we can establish, and
        # an unestablished bypass is not one. Falling back to the environment
        # here was reading a DIFFERENT source from the one the request will
        # use, which can disagree in both directions.
        return False
    try:
        from synapse.http.proxyagent import proxy_bypass_environment

        return bool(proxy_bypass_environment(host, proxies=proxies))
    except Exception:
        return False


def _saved_proxies(proxied: Any) -> Optional[Dict[str, str]]:
    """The exclusion list the AGENT is holding, in either supported shape.

    Synapse 1.159 keeps a `proxy_config` object and passes
    `get_proxies_dictionary()` into `proxy_bypass_environment`; 1.124 keeps a
    bare `no_proxy` string and passes `{"no": self.no_proxy}`. Reading only
    the newer shape refused startup on a 1.124 deployment that had done
    exactly what the error message asks for - exempted the moderation host -
    and COMPAT.yml declares both pins supported.
    """
    config = getattr(proxied, "proxy_config", None)
    if config is not None and hasattr(config, "get_proxies_dictionary"):
        proxies = config.get_proxies_dictionary()
        return dict(proxies) if isinstance(proxies, dict) else None
    no_proxy = getattr(proxied, "no_proxy", None)
    if isinstance(no_proxy, str):
        return {"no": no_proxy}
    return None


KIND_TRANSPORT = "transport"
KIND_TIMEOUT = "timeout"
KIND_SERVER_ERROR = "server_error"
KIND_RATE_LIMITED = "rate_limited"
KIND_CONFIG_ERROR = "config_error"
KIND_DECODE = "decode"
KIND_SHAPE = "shape"
# The endpoint does not understand a batched request. Deliberately NOT a kind
# of `config_error`, even though the status that carries it is a 4xx: the two
# want opposite responses. A config error means stop asking and tell an
# operator; this one means ask again, one text at a time, right now - and
# never asking again is moderation silently switching itself off.
#
# It is also not a `shape` failure, which means the provider is misbehaving
# and the messages are left alone. This one is the ordinary state of an
# un-upgraded deployment and costs nothing but a round trip.
KIND_BATCH_UNSUPPORTED = "batch_unsupported"

FAILURE_KINDS = frozenset(
    {
        KIND_TRANSPORT,
        KIND_TIMEOUT,
        KIND_SERVER_ERROR,
        KIND_RATE_LIMITED,
        KIND_CONFIG_ERROR,
        KIND_DECODE,
        KIND_SHAPE,
        KIND_BATCH_UNSUPPORTED,
    }
)

# The statuses that mean "this request's SHAPE was refused", which is what an
# endpoint with a required `text` field answers to a body carrying `texts`.
# FastAPI validates the Pydantic model before the handler runs and returns 422;
# 400 is here for a gateway that rewrites it.
#
# Narrow on purpose. A 401 is an expired service-account token, a 404 is a
# misconfigured path, a 429 is rate limiting and a 5xx is the provider - none
# of them says anything about batching, and reading one as "batching is
# unsupported" would demote a healthy deployment to single-text calls for the
# life of the process on one transient error.
_BATCH_REFUSED_STATUSES = frozenset({400, 422})


# The "this endpoint does not do batches" signal, as a value rather than an
# exception. See `ChoreoChecker._batch_admitted` for why it is not a raise.
_BATCH_UNSUPPORTED = object()


@attr.s(auto_attribs=True, frozen=True, slots=True)
class BatchVerdicts:
    """What `ChoreoChecker.check_batch` answers, and whether it may be acted on.

    Two fields because they are two different facts, and collapsing them is
    the mistake this type exists to prevent. `results` is one entry per
    requested text, in request order, `None` where there is no verdict.
    `confirmed` says whether each entry came back from a request carrying
    exactly its own text - so a caller may act on it directly - or was mapped
    by position out of one batched response, in which case it is a screen and
    anything it flags has to be asked again on its own.
    """

    results: List[Optional[Dict[str, Any]]]
    confirmed: bool


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
    if "categories" in result and (
        not isinstance(result["categories"], list)
        or not all(isinstance(category, str) for category in result["categories"])
    ):
        # `in`, not `get(...) is not None`: `categories: null` is a PRESENT
        # key with an unusable value, and reading it as absent turned
        # `{"flagged": true, "categories": null}` into a flagged verdict with
        # no category - which redacts, including when the category the service
        # meant to send was self-harm.
        raise ModerationCheckError(
            "moderation endpoint returned categories that are not a list of strings",
            KIND_SHAPE,
        )
    if "evaluated" in result and not isinstance(result["evaluated"], bool):
        # The caller tests `evaluated is False`, so a non-bool - `0`, `"false"`
        # - read as "the provider evaluated this", which is the one thing the
        # field exists to tell us it did not do. A value we cannot read is no
        # verdict, not a clean one.
        raise ModerationCheckError(
            "moderation endpoint returned a non-boolean evaluated", KIND_SHAPE
        )
    return result


def _status_kind(code: int, *, batched: bool = False) -> str:
    """Which failure a non-2xx status is, for the breaker.

    The distinction is load-bearing, not tidiness. A 401 from an expired
    service-account token repeats forever and is our fault; opening the
    breaker on it would disable moderation indefinitely while the state gauge
    blamed the provider. A 5xx or a 429 is the provider, and is exactly what
    the breaker exists to stop hammering.

    `batched` splits one more case off the 4xx band. An endpoint whose
    `ModerationRequest.text` is a required field answers 422 to a body
    carrying `texts`, and that is not a configuration error - it is the
    ordinary state of a deployment that has not been upgraded yet, and the
    answer to it is to ask again one text at a time rather than to tell an
    operator to check their token.
    """
    if batched and code in _BATCH_REFUSED_STATUSES:
        return KIND_BATCH_UNSUPPORTED
    if code == 429:
        return KIND_RATE_LIMITED
    if 400 <= code < 500:
        return KIND_CONFIG_ERROR
    return KIND_SERVER_ERROR


def _validated_batch(result: Any, expected: int) -> List[Dict[str, Any]]:
    """The batch response, refused unless it can be mapped with certainty.

    The wire contract is POSITIONAL - one result per input, in input order -
    so the length is not a detail, it is the whole of the evidence that a
    result belongs to the message it will be applied to. A list one short
    does not mean "one message went unchecked": paired naively it shifts every
    verdict from the point of the omission onwards onto the wrong learner's
    message, redacting one who wrote nothing wrong and leaving the harmful one
    standing. So a length that does not match the request produces NO verdicts
    at all, and the caller's fail-open path leaves every message in the batch
    alone.

    A response that is not a `{"results": [...]}` object at all is read as an
    endpoint that did not understand `texts`, not as a misbehaving one - that
    is what an un-upgraded choreo looks like if it ever answers 200, and the
    caller's answer to it is to re-ask one text at a time.
    """
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        raise ModerationCheckError(
            "moderation endpoint returned no batch results",
            KIND_BATCH_UNSUPPORTED,
        )
    results = result["results"]
    if len(results) != expected:
        raise ModerationCheckError(
            f"moderation endpoint returned {len(results)} results for "
            f"{expected} texts",
            KIND_SHAPE,
        )
    # Every element, before any of them is used. One element we cannot read
    # means the endpoint is not doing what the contract says, and the element
    # we cannot read may be exactly the one whose position is wrong - so
    # reading the others positionally is the move that misattributes a
    # verdict. A batch is one answer to one question.
    return [_validated_result(item) for item in results]


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


async def moderate_texts(
    texts: Sequence[str],
    base_url: str,
    access_token: str,
    *,
    agent: Any,
    clock: Any,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
) -> List[Dict[str, Any]]:
    """Return one choreo ModerationResult per entry of ``texts``, in order.

    The batched half of the same endpoint: `{"texts": [...]}` answers
    `{"results": [...]}`, one result per input. One provider call therefore
    carries many messages, which is what turns a ~2-second provider latency
    into throughput instead of a queue that fills and drops.

    **The returned list is always exactly as long as ``texts``, or there is no
    list.** A caller pairs the two positionally, so a short or long answer is
    not a partial result to salvage - it is a pairing in which verdicts belong
    to the wrong messages. `_validated_batch` refuses it outright and this
    raises, which is the caller's fail-open path.

    Raises `ModerationCheckError` with kind `batch_unsupported` when the
    endpoint does not understand a batched request, which is what an
    un-upgraded choreo answers. That is a signal to ask again one text at a
    time, never a reason to stop moderating.
    """
    try:
        result = await _exchange(
            {"texts": list(texts), "mock": False},
            base_url,
            access_token,
            agent,
            clock,
            timeout_seconds,
            batched=True,
        )
        return _validated_batch(result, len(texts))
    except ModerationCheckError as error:
        # Severed one frame out from every raise site, for the reason given on
        # `moderate_text`.
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
    # `{"text": ...}` alone, deliberately: `mock` defaults to false on the
    # endpoint's own request model, and this is the shape that is already in
    # production. The batched call sends `mock` explicitly because that is the
    # contract the batched handler is being written to; there is no reason to
    # move a working request to match it.
    result = await _exchange(
        {"text": text},
        base_url,
        access_token,
        agent,
        clock,
        timeout_seconds,
        batched=False,
    )
    return _validated_result(result)


async def _exchange(
    payload: Dict[str, Any],
    base_url: str,
    access_token: str,
    agent: Any,
    clock: Any,
    timeout_seconds: float,
    *,
    batched: bool,
) -> Any:
    """One POST to `/choreo/moderate`, decoded but not yet interpreted.

    Shared by the single and batched callers so that the two deadlines, the
    bounded body read, the status-before-body ordering and the chain severing
    are written once. The only thing `batched` changes is how a 4xx status is
    classified - see `_status_kind`; everything else about the exchange is
    identical, and a second copy of it is how one of the two halves quietly
    loses its body deadline.
    """
    from io import BytesIO

    from twisted.web.client import FileBodyProducer

    body = json.dumps(payload).encode("utf-8")
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
                _status_kind(response.code, batched=batched),
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
            _status_kind(response.code, batched=batched),
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
    return result


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
        # The batch contract, negotiated once and then remembered. Starts
        # optimistic: an upgraded endpoint is the intended state, and the cost
        # of being wrong is one refused request per process.
        self._batch_supported = True

    @property
    def batch_supported(self) -> bool:
        """Whether the endpoint has been seen to refuse a batched request.

        Read by the caller after a batch so the DISPATCHER can stop forming
        batches this endpoint cannot take - the fallback is serial inside one
        worker, so a batch it cannot use is latency with nothing to show for
        it.
        """
        return self._batch_supported

    async def check_batch(self, texts: Sequence[str]) -> "BatchVerdicts":
        """Screen many messages with one provider call, in order.

        **`results` is always exactly as long as ``texts``, on every path** -
        a clean batch, a refused one, an open breaker, a fallback to
        single-text calls. The caller pairs the two positionally, so a short
        return is the one failure that could silently shift every verdict onto
        the next message; there is no route out of this function that produces
        one.

        An entry is `None` when that message got no verdict, which always
        means the same thing it means everywhere else here: leave the message
        alone.

        **`confirmed` is what says whether a verdict may be acted on.** It is
        true when each verdict came back from a request carrying exactly its
        own text - a batch of one, or the single-text fallback - and false
        when they were mapped by position out of one batched response. A
        positional verdict is a screen: the caller re-asks that one message
        before taking any irreversible action.

        It is returned rather than left for the caller to infer, because the
        two paths look identical from outside and getting it wrong is silent
        in both directions: treating a screen as confirmed redacts on a
        positional mapping, and treating a confirmed verdict as a screen
        asks the provider a second time for every flagged message.

        A flagged SCREEN entry is deliberately not counted by `record_check`
        here - the confirmation counts it, and counting both would report two
        checks for one message.
        """
        from synapse_pangea_chat.moderation import metrics

        if not texts:
            return BatchVerdicts([], confirmed=True)
        if len(texts) == 1 or not self._batch_supported:
            # A batch of one buys nothing and, against an un-upgraded
            # endpoint, costs a refused round trip before the single call it
            # was always going to make. `check` also already carries the
            # breaker, the latency observation and the per-message counter,
            # so the fallback is the ordinary path rather than a second
            # implementation of it - and each of its answers is confirmed by
            # construction, because each request carried one text.
            return BatchVerdicts(
                [await self.check(text) for text in texts], confirmed=True
            )

        refusal, ticket = self._breaker.check()
        if refusal is not None:
            # Counted once PER MESSAGE, not once per batch: each of them went
            # unmoderated, and a shed batch reported as one drop understates
            # the gap by the size of the batch.
            metrics.record_drop(refusal, len(texts))
            return BatchVerdicts([None] * len(texts), confirmed=False)

        try:
            results = await self._batch_admitted(texts, ticket)
        finally:
            # Same placement and the same reason as in `check`: after the
            # outcome has been reported, never before it.
            self._breaker.release(ticket)

        if results is None:
            return BatchVerdicts([None] * len(texts), confirmed=False)
        if results is _BATCH_UNSUPPORTED:
            self._demote_batching()
            # Re-asked immediately and one at a time. Never wedge and never
            # fail closed: an endpoint that does not understand `texts` is the
            # ordinary state of a deployment that has not been upgraded, and
            # the messages in this batch still have to be moderated.
            return BatchVerdicts(
                [await self.check(text) for text in texts], confirmed=True
            )

        verdicts = cast(List[Dict[str, Any]], results)
        clean = sum(1 for verdict in verdicts if not verdict.get("flagged"))
        flagged = len(verdicts) - clean
        if clean:
            metrics.record_screen("clean", clean)
            metrics.record_check("clean", clean)
        if flagged:
            metrics.record_screen("flagged", flagged)
        return BatchVerdicts(list(verdicts), confirmed=False)

    def _demote_batching(self) -> None:
        from synapse_pangea_chat.moderation import metrics

        if not self._batch_supported:
            return
        self._batch_supported = False
        metrics.TIER2_BATCH_UNSUPPORTED.set(1)
        # Once per process, at INFO rather than WARNING: this is the expected
        # state of a deployment whose choreo predates the batched handler, not
        # a fault. What it costs is throughput, and the gauge above is what an
        # operator alerts on.
        logger.info(
            "tier2 moderation endpoint does not accept batched requests; "
            "falling back to one call per message for the life of this "
            "process"
        )

    async def _batch_admitted(self, texts: Sequence[str], ticket: Optional[int]) -> Any:
        """The batch's verdicts, `None` for no verdict, or the demote sentinel.

        A SENTINEL and not an exception for the unsupported case, deliberately.
        Raising a new exception from inside an `except` re-attaches the
        original on `__context__`, and the original here is a
        `ModerationCheckError` whose chain this module goes to some length to
        sever. A return value carries the same information and carries nothing
        else.
        """
        from synapse_pangea_chat.moderation import metrics

        started = self._clock.time()
        try:
            try:
                results = await moderate_texts(
                    texts,
                    base_url=self._base_url,
                    access_token=self._access_token,
                    agent=self._agent,
                    clock=self._clock,
                    timeout_seconds=self._timeout_seconds,
                )
            finally:
                # Per CALL, which is what this histogram has always measured.
                # The per-message figure is this divided by the batch size,
                # and `TIER2_BATCH_SIZE` is the other half of that division.
                metrics.TIER2_LATENCY.observe(max(self._clock.time() - started, 0.0))
        except Exception as exc:
            reraise_if_cancelled(exc)
            if failure_kind(exc) == KIND_BATCH_UNSUPPORTED:
                # silent-ok: not a failure of the provider, and deliberately
                # not reported as one. The endpoint refused the SHAPE of the
                # request, the messages are about to be checked properly one
                # at a time, and nothing has gone unmoderated - so nothing is
                # told to the breaker and nothing is counted as an error. It
                # is not unobservable either: `_demote_batching` logs the
                # demotion once and raises the
                # `tier2_batch_unsupported` gauge, which is the signal an
                # operator actually wants - a per-batch line would say the
                # same thing on every batch for the life of the process.
                return _BATCH_UNSUPPORTED
            # `Exception` rather than `ModerationCheckError`, for the reason
            # given on `_check_admitted`: whatever escapes this frame reaches
            # `run_as_background_process`, which logs it in full.
            self._record_failure(exc, ticket)
            metrics.record_check("error", len(texts))
            metrics.record_screen("no_verdict", len(texts))
            return None

        metrics.TIER2_BATCH_SIZE.observe(len(texts))
        if any(result.get("evaluated") is False for result in results):
            # The documented shape of a provider outage, and it is read across
            # the WHOLE batch rather than per item. The choreo handler answers
            # `evaluated: false` when its own provider call failed, and a
            # provider that failed for one text in a single request failed for
            # all of them - treating the rest as evaluated would report a
            # verdict nobody produced.
            self._breaker.record_failure(ticket)
            metrics.record_check("unevaluated", len(texts))
            metrics.record_screen("no_verdict", len(texts))
            logger.warning(
                "tier2 moderation endpoint returned no evaluation for a batch "
                "of %d; those messages were left unchecked",
                len(texts),
            )
            return None

        self._breaker.record_success(ticket)
        return results

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


# What the record says instead. A fixed string with no arguments at all: the
# operator still learns that a CONNECT status came back, and learns nothing a
# proxy wrote.
_PROXY_STATUS_WITHHELD = "Got Status: <withheld, see moderation.choreo_client>"


class _ProxyStatusFilter(logging.Filter):
    """Keeps a proxy's CONNECT status line out of the log. All of it.

    `HTTPConnectSetupClient.handleStatus` logs the status line **verbatim** at
    DEBUG, so an HTTPS proxy replying `HTTP/1.1 200 @alice:example.org` puts
    that Matrix ID into the log of any deployment running this logger at
    DEBUG. It is reached identically through every Synapse HTTP client, so the
    transport choice does not avoid it - but a logging filter is ours to
    install and needs no change to Synapse.

    **The whole line, not one field of it.** An earlier version scrubbed the
    reason phrase and passed the status and the version through, on the
    reading that an operator debugging a proxy wants to see the code. But
    `HTTPClient.lineReceived` simply splits the line into three on spaces and
    validates none of them, so all three fields are strings the proxy chose:
    feeding that parser `@alice:example.org 200 OK` put a Matrix ID in the
    VERSION field, straight past a guard that was installed and working.

    Cleaning one field of an attacker-influenced line is not cleaning the
    line, and there is no field of it we can establish is ours. So the record
    keeps its level, its logger and its timestamp - an operator still sees
    that a CONNECT status arrived and when - and carries no bytes off the
    wire at all. The diagnostic that is lost is the status code, which is not
    worth a route into a plaintext log for anything a proxy cares to write.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg != _PROXY_STATUS_LOG_FORMAT:
            return True
        record.args = ()
        record.msg = _PROXY_STATUS_WITHHELD
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
