"""HTTP client for the choreographer's shared moderation handler.

POST {base_url}/choreo/moderate with a Matrix bearer token (the endpoint is
`has_matrix_account`-gated — any valid token on this homeserver; deployments
configure a dedicated moderation service account's token).

Mirrors the twisted-Agent pattern of public_courses.course_plan_l2_lookup so
the module adds no HTTP dependency.
"""

import json
from typing import Any, Dict

from synapse_pangea_chat.moderation.log_safety import scrubbing_logger

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


class ModerationCheckError(Exception):
    """The moderation service could not produce a verdict."""


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
    flagged = result.get("flagged", False)
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
    from twisted.web.client import Agent, readBody
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
        body_deferred = readBody(response)
        body_deferred.addTimeout(max(deadline - reactor.seconds(), 0), reactor)
        raw = await body_deferred
    except Exception as e:
        # `from None`: the caller and `run_as_background_process` both log what
        # reaches them, and an ordinary `raise X from Y` keeps the original on
        # `__cause__`, which `logger.exception` prints in full. A transport or
        # decode error routinely quotes the payload that produced it, so the
        # chain is dropped rather than relabelled (ADR-10).
        raise ModerationCheckError(
            f"moderation request failed: {type(e).__name__}"
        ) from None

    if response.code >= 400:
        raise ModerationCheckError(f"moderation endpoint returned {response.code}")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        raise ModerationCheckError("moderation endpoint returned non-JSON") from None
    return _validated_result(result)
