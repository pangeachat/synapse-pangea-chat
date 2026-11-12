"""One way to call the Payload CMS from the module: the service-users API key,
a bounded body, and an optional timeout. Callers keep their own error types
and decide what a status code means."""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any, Dict, Optional, Tuple

from synapse.http.client import read_body_with_max_size
from synapse.logging.context import make_deferred_yieldable
from twisted.web.http_headers import Headers

SERVICE_USERS_COLLECTION = "service-users"


def service_headers(api_key: str, *, json_body: bool = False) -> Headers:
    headers: Dict[str, Any] = {
        "Authorization": [f"{SERVICE_USERS_COLLECTION} API-Key {api_key}"]
    }
    if json_body:
        headers["Content-Type"] = ["application/json"]
    return Headers(headers)


def canonical_activity_where(activity_id: str) -> Dict[str, str]:
    """The where clause for an activity's canonical ``activities-v2`` row:
    its id, and not one of its localized variants."""
    return {
        "where[res.plan.activity_id][equals]": activity_id,
        "where[req.source_request_hash][exists]": "false",
    }


async def cms_request(
    http_client: Any,
    method: str,
    url: str,
    headers: Headers,
    *,
    max_bytes: int,
    body: Optional[Dict[str, Any]] = None,
    timeout_seconds: Optional[float] = None,
    reactor: Any = None,
) -> Tuple[int, bytes]:
    """The response's status and raw body, read up to ``max_bytes``. With a
    timeout, reading the body is bounded by it (``reactor`` required)."""
    if body is None:
        response = await http_client.request(method, url, headers=headers)
    else:
        response = await http_client.request(
            method, url, data=json.dumps(body).encode(), headers=headers
        )
    buffer = BytesIO()
    reading = read_body_with_max_size(response, buffer, max_bytes)
    if timeout_seconds is not None:
        reading = reading.addTimeout(timeout_seconds, reactor)
    await make_deferred_yieldable(reading)
    return response.code, buffer.getvalue()
