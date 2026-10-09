"""Platform signing keys, fetched from the platform's JWKS and cached by kid.

A launch names its key by `kid`. A kid the cache does not hold triggers one
refetch (a platform may have rotated), but at most once per
`min_refetch_seconds` per JWKS URL, so a stream of launches naming a made-up
kid cannot turn the tool into a request amplifier against the platform. A
failed fetch counts as an attempt for the same reason. Lookups that arrive
while a fetch is in flight wait for that fetch instead of starting another.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

import attr
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.util.async_helpers import ObservableDeferred

from synapse_pangea_chat.lti.keys import MIN_RSA_BITS, SIGNING_ALGORITHM

logger = logging.getLogger("synapse.module.synapse_pangea_chat.lti.platform_jwks")

MAX_KEYS = 50


@attr.s(auto_attribs=True)
class _Entry:
    keys: Dict[str, jwt.PyJWK]
    fetched_at: float
    attempted_at: float


def parse_jwks(document: Any) -> Dict[str, jwt.PyJWK]:
    """The RS256 signing keys of a JWKS document, by kid; others are skipped."""
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        return {}
    keys: Dict[str, jwt.PyJWK] = {}
    for item in document["keys"][:MAX_KEYS]:
        if not isinstance(item, dict):
            continue
        kid = item.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        if item.get("kty") != "RSA":
            continue
        if item.get("alg", SIGNING_ALGORITHM) != SIGNING_ALGORITHM:
            continue
        if item.get("use", "sig") != "sig":
            continue
        try:
            key = jwt.PyJWK(item, SIGNING_ALGORITHM)
        # silent-ok: an unusable entry is skipped; a launch naming it is refused as unknown_key
        except (jwt.PyJWKError, jwt.InvalidKeyError, ValueError, TypeError):
            continue
        if not isinstance(key.key, RSAPublicKey) or key.key.key_size < MIN_RSA_BITS:
            continue
        keys[kid] = key
    return keys


class PlatformKeyCache:
    def __init__(
        self,
        fetch_json: Callable[[str], Awaitable[Any]],
        clock: Callable[[], float],
        *,
        max_age_seconds: float = 3600,
        min_refetch_seconds: float = 60,
    ):
        self._fetch_json = fetch_json
        self._clock = clock
        self._max_age = max_age_seconds
        self._min_refetch = min_refetch_seconds
        self._entries: Dict[str, _Entry] = {}
        self._inflight: Dict[str, ObservableDeferred] = {}

    async def get_key(self, jwks_uri: str, kid: str) -> Optional[jwt.PyJWK]:
        inflight = self._inflight.get(jwks_uri)
        if inflight is not None:
            # A fetch is already on its way: wait for it rather than treating
            # it as a recent attempt, so launches arriving together on a cold
            # or expired cache all get its answer.
            await make_deferred_yieldable(inflight.observe())
            return self._lookup(jwks_uri, kid)
        now = self._clock()
        entry = self._entries.get(jwks_uri)
        fresh = entry is not None and now - entry.fetched_at < self._max_age
        if entry is not None and fresh and kid in entry.keys:
            return entry.keys[kid]
        if entry is not None and now - entry.attempted_at < self._min_refetch:
            # Recently fetched (or tried): answer from what we have.
            return entry.keys.get(kid) if fresh else None

        refresh = run_in_background(self._refresh, jwks_uri, now)
        shared = ObservableDeferred(refresh, consumeErrors=True)
        self._inflight[jwks_uri] = shared
        try:
            await make_deferred_yieldable(shared.observe())
        finally:
            if self._inflight.get(jwks_uri) is shared:
                del self._inflight[jwks_uri]
        return self._lookup(jwks_uri, kid)

    def _lookup(self, jwks_uri: str, kid: str) -> Optional[jwt.PyJWK]:
        entry = self._entries.get(jwks_uri)
        if entry is None or self._clock() - entry.fetched_at >= self._max_age:
            return None
        return entry.keys.get(kid)

    async def _refresh(self, jwks_uri: str, now: float) -> None:
        """Fetch and store the key set. Never raises: a failure is logged and
        counted as an attempt, so the refetch limit also covers failures."""
        entry = self._entries.get(jwks_uri)
        if entry is None:
            entry = _Entry(keys={}, fetched_at=float("-inf"), attempted_at=now)
            self._entries[jwks_uri] = entry
        else:
            entry.attempted_at = now
        try:
            document = await self._fetch_json(jwks_uri)
        except Exception as e:
            # Logged by type only: the message of an HTTP failure can carry the URL.
            logger.warning("LTI platform JWKS fetch failed: %s", type(e).__name__)
            return
        keys = parse_jwks(document)
        if not keys:
            logger.warning("LTI platform JWKS held no usable RS256 signing key")
        entry.keys = keys
        entry.fetched_at = now
