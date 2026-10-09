"""Outbound HTTPS for the LTI tool: capped, time-limited, JSON only.

Every URL fetched here was supplied by a platform (an OpenID configuration URL
anyone can send to `/lti/register`, and the endpoints that document lists), so
the requests go through Synapse's proxied, IP-blocklisted client: the
homeserver's `ip_range_blocklist` stops them reaching private addresses.
Redirects are not followed. Bodies are capped and read under a timeout.
"""

from __future__ import annotations

import json
import logging
import re
from io import BytesIO
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from synapse.http.client import BodyExceededMaxSize, read_body_with_max_size
from synapse.logging.context import make_deferred_yieldable
from synapse.util.async_helpers import timeout_deferred
from twisted.internet import defer
from twisted.web.http_headers import Headers

logger = logging.getLogger("synapse.module.synapse_pangea_chat.lti.http")

MAX_BODY_BYTES = 256 * 1024
BODY_TIMEOUT_SECONDS = 15
MAX_URL_LENGTH = 2048
_NETLOC = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    r"|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$"
)


class UpstreamError(Exception):
    """A platform request failed; `reason` is a fixed code, never the URL."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def https_url(value: Any) -> Optional[str]:
    """`value` if it is a plain absolute https URL, else None.

    Plain means: ASCII, no whitespace or control characters, a host that is a
    DNS name or IP literal (so no userinfo), an optional numeric port and no
    fragment.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        return None
    if not value.isascii() or any(c.isspace() or ord(c) < 0x21 for c in value):
        return None
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError on a bad port
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname:
        return None
    if parts.fragment or not _NETLOC.match(parts.netloc):
        # Also excludes userinfo: a host is a DNS name or IP literal, nothing
        # else, because it is sent upstream and echoed into a CSP header.
        return None
    return value


def host_of(url: str) -> str:
    """Lower-cased host plus port (default 443) of an https URL."""
    parts = urlsplit(url)
    return f"{(parts.hostname or '').lower()}:{parts.port or 443}"


class PlatformHttp:
    def __init__(self, hs: Any):
        self._client = hs.get_proxied_blocklisted_http_client()
        self._clock = hs.get_clock()

    async def get_json(self, url: str) -> Any:
        return await self._request("GET", url, None, {})

    async def post_json(
        self, url: str, body: Dict[str, Any], bearer: Optional[str]
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return await self._request("POST", url, json.dumps(body).encode(), headers)

    async def _request(
        self, method: str, url: str, data: Optional[bytes], headers: Dict[str, str]
    ) -> Any:
        if https_url(url) is None:
            raise UpstreamError("not_https")
        raw_headers = Headers({b"Accept": [b"application/json"]})
        for name, value in headers.items():
            raw_headers.addRawHeader(name, value)
        try:
            response = await self._client.request(
                method, url, data=data, headers=raw_headers
            )
        except Exception as e:
            # The exception type only: its message can carry the URL.
            logger.warning(
                "LTI platform %s request failed: %s", method, type(e).__name__
            )
            raise UpstreamError("unreachable") from None
        stream = BytesIO()
        try:
            d = read_body_with_max_size(response, stream, MAX_BODY_BYTES)
            d = timeout_deferred(
                deferred=d, timeout=BODY_TIMEOUT_SECONDS, clock=self._clock
            )
            await make_deferred_yieldable(d)
        except BodyExceededMaxSize:
            raise UpstreamError("too_large") from None
        except defer.TimeoutError:
            raise UpstreamError("timeout") from None
        except Exception:
            raise UpstreamError("read_failed") from None
        if not 200 <= response.code < 300:
            raise UpstreamError(f"status_{response.code}")
        try:
            return json.loads(stream.getvalue().decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise UpstreamError("not_json") from None
