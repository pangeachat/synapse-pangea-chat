"""The tool's signing key, read from module config, and its published JWKS.

The private key reaches the module only through its config (the `lti` block's
`private_key_pem`, which Ansible fills from Secrets Manager). Nothing here
generates a key, reads one from disk or falls back to a built-in one: a missing
or unusable key means the LTI endpoints do not start (see `register_lti`).

Error messages name the problem, never the value, so a rejected key cannot end
up in a log line or in Sentry.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Dict, List, Tuple

import attr
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.utils import to_base64url_uint

MIN_RSA_BITS = 2048
SIGNING_ALGORITHM = "RS256"
CONFIG_KEYS = frozenset({"private_key_pem", "previous_public_keys_pem"})
MAX_PREVIOUS_KEYS = 5


class LtiConfigError(ValueError):
    """The `lti` config block cannot be used; the message carries no secret."""


def _public_jwk_fields(public_key: RSAPublicKey) -> Dict[str, str]:
    numbers = public_key.public_numbers()
    return {
        "kty": "RSA",
        "n": to_base64url_uint(numbers.n).decode(),
        "e": to_base64url_uint(numbers.e).decode(),
    }


def thumbprint(jwk: Dict[str, Any]) -> str:
    """RFC 7638 JWK thumbprint of an RSA key, used as its `kid`.

    Derived from the key itself, so rotating the key changes the kid and no
    second config value can drift out of step with it.
    """
    canonical = json.dumps(
        {"e": jwk["e"], "kty": "RSA", "n": jwk["n"]},
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _published_jwk(public_key: RSAPublicKey) -> Dict[str, str]:
    fields = _public_jwk_fields(public_key)
    return {
        "kty": "RSA",
        "kid": thumbprint(fields),
        "alg": SIGNING_ALGORITHM,
        "use": "sig",
        "n": fields["n"],
        "e": fields["e"],
    }


@attr.s(frozen=True, auto_attribs=True)
class ToolKey:
    """The current signing key. `repr` never shows the private half."""

    kid: str
    public_jwk: Dict[str, str] = attr.ib(eq=False)
    _private_key: RSAPrivateKey = attr.ib(repr=False, eq=False)

    def sign(self, payload: Dict[str, Any]) -> str:
        """An RS256 JWT carrying this key's kid (for the NRPS token request)."""
        return jwt.encode(
            payload,
            self._private_key,
            algorithm=SIGNING_ALGORITHM,
            headers={"kid": self.kid},
        )


@attr.s(frozen=True, auto_attribs=True)
class LtiSettings:
    signing_key: ToolKey
    # Public halves of keys being rotated out, still published so a platform
    # holding a token signed before the rotation can verify it.
    previous_public_jwks: Tuple[Dict[str, str], ...] = ()

    def public_jwks(self) -> Dict[str, List[Dict[str, str]]]:
        """The JWKS document: public members only, current key first."""
        keys = [dict(self.signing_key.public_jwk)]
        keys.extend(dict(jwk) for jwk in self.previous_public_jwks)
        return {"keys": keys}


def _check_rsa_size(bits: int, what: str) -> None:
    if bits < MIN_RSA_BITS:
        raise LtiConfigError(
            f"{what} is a {bits}-bit RSA key; at least {MIN_RSA_BITS} bits are required"
        )


def load_signing_key(pem: Any) -> ToolKey:
    if pem is None or (isinstance(pem, str) and not pem.strip()):
        raise LtiConfigError('Config "lti.private_key_pem" is missing')
    if not isinstance(pem, str):
        raise LtiConfigError('Config "lti.private_key_pem" must be a PEM string')
    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except TypeError:
        raise LtiConfigError(
            'Config "lti.private_key_pem" is encrypted; supply an unencrypted '
            "PKCS#8 or PKCS#1 RSA private key"
        ) from None
    except (ValueError, UnicodeError):
        raise LtiConfigError(
            'Config "lti.private_key_pem" is not a readable PEM private key'
        ) from None
    if not isinstance(key, RSAPrivateKey):
        raise LtiConfigError('Config "lti.private_key_pem" must be an RSA key')
    _check_rsa_size(key.key_size, 'Config "lti.private_key_pem"')
    published = _published_jwk(key.public_key())
    return ToolKey(kid=published["kid"], public_jwk=published, private_key=key)


def _load_previous_public_key(pem: Any, index: int) -> Dict[str, str]:
    what = f'Config "lti.previous_public_keys_pem[{index}]"'
    if not isinstance(pem, str) or not pem.strip():
        raise LtiConfigError(f"{what} must be a PEM string")
    try:
        key = serialization.load_pem_public_key(pem.encode("utf-8"))
    except (ValueError, UnicodeError):
        raise LtiConfigError(f"{what} is not a readable PEM public key") from None
    if not isinstance(key, RSAPublicKey):
        raise LtiConfigError(f"{what} must be an RSA key")
    _check_rsa_size(key.key_size, what)
    return _published_jwk(key)


def parse_lti_config(raw: Any) -> LtiSettings:
    """Validate the module config's `lti` block.

    Accepts exactly `private_key_pem` (required) and `previous_public_keys_pem`
    (optional list). An unknown key is an error, so a misspelt key name cannot
    leave the endpoints running without the key the operator meant to give.
    """
    if not isinstance(raw, dict):
        raise LtiConfigError('Config "lti" must be a mapping')
    unknown = sorted(set(raw) - CONFIG_KEYS)
    if unknown:
        raise LtiConfigError(
            'Config "lti" has unknown keys: ' + ", ".join(repr(k) for k in unknown)
        )
    signing_key = load_signing_key(raw.get("private_key_pem"))

    previous = raw.get("previous_public_keys_pem", [])
    if not isinstance(previous, list):
        raise LtiConfigError('Config "lti.previous_public_keys_pem" must be a list')
    if len(previous) > MAX_PREVIOUS_KEYS:
        raise LtiConfigError(
            f'Config "lti.previous_public_keys_pem" holds more than '
            f"{MAX_PREVIOUS_KEYS} keys"
        )
    previous_jwks = []
    kids = {signing_key.kid}
    for index, pem in enumerate(previous):
        jwk = _load_previous_public_key(pem, index)
        if jwk["kid"] in kids:
            raise LtiConfigError(
                f'Config "lti.previous_public_keys_pem[{index}]" repeats a key '
                "already published"
            )
        kids.add(jwk["kid"])
        previous_jwks.append(jwk)
    return LtiSettings(
        signing_key=signing_key, previous_public_jwks=tuple(previous_jwks)
    )
