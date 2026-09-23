"""Signed, expiring tokens for links that act on a person while signed out.

The unsubscribe link and the click-through link both name a user and an action.
Both are HMAC-signed with a server secret so the URL cannot be forged or
edited, and both carry an expiry so an old email cannot act forever. The
payload is not encrypted: it holds only a user id, a category, and event ids,
none of which is secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Dict, Optional

MILLISECONDS_PER_DAY = 24 * 60 * 60 * 1000


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _signature(secret: bytes, payload_part: str) -> str:
    digest = hmac.new(secret, payload_part.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def sign_token(
    secret: bytes, payload: Dict[str, Any], *, now_ms: int, ttl_ms: int
) -> str:
    body = dict(payload)
    body["exp"] = now_ms + ttl_ms
    payload_part = _b64encode(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{payload_part}.{_signature(secret, payload_part)}"


def verify_token(
    secret: bytes, token: Optional[str], *, now_ms: int
) -> Optional[Dict[str, Any]]:
    """Return the payload for a valid, unexpired token, else None."""
    if not isinstance(token, str) or token.count(".") != 1:
        return None
    payload_part, signature = token.split(".", 1)
    if not payload_part or not signature or not token.isascii():
        # A non-ASCII token is not one we issued; refusing it here keeps the
        # signature step from raising on it and the caller answers "invalid link".
        return None
    expected = _signature(secret, payload_part)
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        payload = json.loads(_b64decode(payload_part).decode("utf-8"))
    # silent-ok: a token that verifies but does not parse is corrupt; the caller
    # answers "invalid link", which is the only honest response
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < now_ms:
        return None
    return payload
