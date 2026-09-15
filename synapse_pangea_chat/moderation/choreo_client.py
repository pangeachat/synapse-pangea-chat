"""HTTP client for the choreographer's shared moderation handler.

POST {base_url}/choreo/moderate with a Matrix bearer token (the endpoint is
`has_matrix_account`-gated — any valid token on this homeserver; deployments
configure a dedicated moderation service account's token).

Mirrors the twisted-Agent pattern of public_courses.course_plan_l2_lookup so
the module adds no HTTP dependency.
"""

import json
from typing import Any, Dict, List, Tuple

from twisted.internet import defer
from twisted.internet.protocol import Protocol, connectionDone
from twisted.python.failure import Failure
from twisted.web.client import PotentialDataLoss, ResponseDone

from synapse_pangea_chat.moderation.log_safety import _severed, scrubbing_logger

logger = scrubbing_logger(
    "synapse.modules.synapse_pangea_chat.moderation.choreo_client"
)

# Bounded so a slow provider can never back up the moderation queue; the
# choreo handler itself fails open well inside this. The budget covers the
# WHOLE exchange - connect, headers and body - not just the part twisted's
# `addTimeout` reaches on its own.
REQUEST_TIMEOUT_SECONDS = 15

# NOTE: this client is scheduled for replacement by `ModuleApi.http_client`
# (ADR-4, chunk C3), which brings connection pooling, proxy support and
# Synapse's own metrics. It is corrected rather than left for that rewrite
# because every commit on this branch has to be right on its own: a branch
# whose intermediate states are broken cannot be bisected, reverted to, or
# shipped as far as it has got.


# A verdict is a small JSON object. The cap is three orders of magnitude above
# anything the endpoint can legitimately return, and it exists because
# `twisted.web.client.readBody` has no cap at all: a peer that streams forever
# fills the process's memory, and a deadline alone does not stop it - it fires
# the deferred and leaves the body arriving.
MAX_RESPONSE_BYTES = 1024 * 1024


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
    """


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
                    f"{MAX_RESPONSE_BYTES} bytes"
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
                    f"moderation response body failed: {reason.type.__name__}"
                )
            )

    def abort(self) -> None:
        """Stop the peer sending, which cancelling `readBody` does not do."""
        self._fail(ModerationCheckError("moderation response body timed out"))

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
        misbehaving peer can hold open, and this client is replaced wholesale
        in the chunk that moves to `ModuleApi.http_client`.
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
        raise ModerationCheckError("moderation endpoint returned a non-object")
    if "flagged" not in result:
        # Absent is not False. A response of `{}`, or an error object the
        # endpoint returns with a 200, was read as "this message is fine" -
        # which is a verdict we were never given, reached by a default.
        raise ModerationCheckError("moderation endpoint returned no verdict")
    flagged = result["flagged"]
    if not isinstance(flagged, bool):
        raise ModerationCheckError("moderation endpoint returned a non-boolean flagged")
    categories = result.get("categories")
    if categories is not None and (
        not isinstance(categories, list)
        or not all(isinstance(category, str) for category in categories)
    ):
        raise ModerationCheckError(
            "moderation endpoint returned categories that are not a list of strings"
        )
    return result


async def moderate_text(
    text: str,
    base_url: str,
    access_token: str,
    reactor: Any = None,
    agent: Any = None,
) -> Dict[str, Any]:
    """Return the choreo ModerationResult dict for ``text``.

    Raises ModerationCheckError on transport/HTTP/decode/shape failure - the
    caller owns the fail-open disposition. `reactor` and `agent` exist so a
    test can drive the clock and a stalled peer; production passes neither.
    """
    try:
        return await _moderate_text(text, base_url, access_token, reactor, agent)
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
    reactor: Any,
    agent: Any,
) -> Dict[str, Any]:
    from twisted.web.client import Agent
    from twisted.web.http_headers import Headers

    if reactor is None:
        from twisted.internet import reactor as _reactor

        reactor = _reactor
    if agent is None:
        agent = Agent(reactor)
    body = json.dumps({"text": text}).encode("utf-8")

    from io import BytesIO

    from twisted.web.client import FileBodyProducer

    deadline = reactor.seconds() + REQUEST_TIMEOUT_SECONDS
    try:
        d = agent.request(
            b"POST",
            f"{base_url.rstrip('/')}/choreo/moderate".encode("utf-8"),
            Headers(
                {
                    b"Authorization": [f"Bearer {access_token}".encode("utf-8")],
                    b"Content-Type": [b"application/json"],
                }
            ),
            FileBodyProducer(BytesIO(body)),
        )
        d.addTimeout(REQUEST_TIMEOUT_SECONDS, reactor)
        response = await d
        # The body read needs its own timeout, and this is not a belt-and-braces
        # second one. `agent.request`'s deferred fires as soon as the RESPONSE
        # HEADERS arrive, so its timeout is spent by then: a peer that sends
        # headers and then holds the body open leaves `readBody` pending with
        # no timeout scheduled anywhere and no way back. Those checks
        # accumulate, one per message, and nothing is logged, because nothing
        # has failed - the coroutine is simply never resumed.
        body_deferred, body_protocol = _read_body(response)
        # `callLater`, not `addTimeout`: a timeout has to tear the connection
        # down, and `addTimeout` only cancels the deferred. See `_BoundedBody`.
        timeout = reactor.callLater(
            max(deadline - reactor.seconds(), 0), body_protocol.abort
        )
        try:
            raw = await body_deferred
        finally:
            if timeout.active():
                timeout.cancel()
    except Exception as e:
        # The chain is severed by `ModerationCheckError` itself, not by this
        # `from None` - see the class, and ADR-10. `from None` alone leaves
        # `__context__` holding the original, and a `json.JSONDecodeError`
        # carries the whole response body on `.doc`.
        raise ModerationCheckError(
            f"moderation request failed: {type(e).__name__}"
        ) from None

    if response.code >= 400:
        raise ModerationCheckError(f"moderation endpoint returned {response.code}")
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
            f"{type(e).__name__}"
        ) from None
    return _validated_result(result)
