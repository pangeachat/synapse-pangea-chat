"""HTTP client for the choreographer's shared moderation handler.

POST {base_url}/choreo/moderate with a Matrix bearer token (the endpoint is
`has_matrix_account`-gated — any valid token on this homeserver; deployments
configure a dedicated moderation service account's token).

Mirrors the twisted-Agent pattern of public_courses.course_plan_l2_lookup so
the module adds no HTTP dependency.
"""

import json
import logging
from typing import Any, Dict

logger = logging.getLogger(
    "synapse.modules.synapse_pangea_chat.moderation.choreo_client"
)

# Bounded so a slow provider can never back up the moderation queue; the
# choreo handler itself fails open well inside this.
REQUEST_TIMEOUT_SECONDS = 15


class ModerationCheckError(Exception):
    """The moderation service could not produce a verdict."""


async def moderate_text(
    text: str,
    base_url: str,
    access_token: str,
) -> Dict[str, Any]:
    """Return the choreo ModerationResult dict for ``text``.

    Raises ModerationCheckError on transport/HTTP/decode failure — the caller
    owns the fail-open disposition.
    """
    from twisted.internet import reactor
    from twisted.web.client import Agent, readBody
    from twisted.web.http_headers import Headers

    agent = Agent(reactor)
    body = json.dumps({"text": text}).encode("utf-8")

    from io import BytesIO

    from twisted.web.client import FileBodyProducer

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
        raw = await readBody(response)
    except Exception as e:
        raise ModerationCheckError(
            f"moderation request failed: {type(e).__name__}"
        ) from e

    if response.code >= 400:
        raise ModerationCheckError(f"moderation endpoint returned {response.code}")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ModerationCheckError("moderation endpoint returned non-JSON") from e
    if not isinstance(result, dict):
        raise ModerationCheckError("moderation endpoint returned non-object")
    return result
