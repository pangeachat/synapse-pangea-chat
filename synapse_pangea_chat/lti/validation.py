"""LTI 1.3 launch validation (LTI 1.3 Core + 1EdTech Security Framework).

`read_header` runs first, before any key lookup: the token must be RS256 and
name its key. `verify_launch` then checks, with the platform's key:

- the RS256 signature;
- `iss` equals the platform's issuer, `aud` contains its client_id, and `azp`
  (required when `aud` lists more than one party) equals the client_id;
- `exp`, `iat` (present, numeric, not in the future) and `nbf` if present;
- `nonce` equals the one issued with this launch's state (the state row is
  deleted on first use, so the nonce is single use);
- the platform is approved, the deployment id is one it registered;
- message type `LtiResourceLinkRequest`, version `1.3.0`, the target link is
  this tool's launch URL, and the resource link, roles and `sub` claims are
  well formed.

Every rejection is a `LaunchRejected` carrying a reason code. Nothing here logs,
and no message carries a claim value, so neither the token nor the student's
email can reach a log line or Sentry from this module.
"""

from __future__ import annotations

import hmac
from typing import Any, Dict, Iterable, Tuple

import attr
import jwt

from synapse_pangea_chat.lti.keys import SIGNING_ALGORITHM
from synapse_pangea_chat.lti.store import Platform

LTI_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/"
CLAIM_MESSAGE_TYPE = LTI_CLAIM + "message_type"
CLAIM_VERSION = LTI_CLAIM + "version"
CLAIM_DEPLOYMENT_ID = LTI_CLAIM + "deployment_id"
CLAIM_TARGET_LINK_URI = LTI_CLAIM + "target_link_uri"
CLAIM_RESOURCE_LINK = LTI_CLAIM + "resource_link"
CLAIM_ROLES = LTI_CLAIM + "roles"
CLAIM_CONTEXT = LTI_CLAIM + "context"

LTI_VERSION = "1.3.0"
MESSAGE_TYPE_RESOURCE_LINK = "LtiResourceLinkRequest"
# Clock skew tolerated on exp, iat and nbf.
LEEWAY_SECONDS = 60
MAX_TOKEN_LENGTH = 65536
MAX_ID_LENGTH = 255

# Only these full LIS role URIs may start or complete a course connect.
# Simple names ("Instructor") and sub-roles (".../Instructor#TeachingAssistant")
# take the student path.
CONNECT_ROLES = frozenset(
    {
        "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor",
        "http://purl.imsglobal.org/vocab/lis/v2/membership#Administrator",
        "http://purl.imsglobal.org/vocab/lis/v2/institution/person#Administrator",
        "http://purl.imsglobal.org/vocab/lis/v2/system/person#Administrator",
    }
)
PATH_INSTRUCTOR = "instructor"
PATH_LEARNER = "learner"


class LaunchRejected(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


@attr.s(frozen=True, auto_attribs=True)
class Launch:
    platform_id: str
    issuer: str
    sub: str
    deployment_id: str
    context_id: str | None
    roles: Tuple[str, ...]
    path: str
    # The full verified claim set, for the steps after the launch (link,
    # connect, claim). Kept out of repr: it holds the student's email.
    claims: Dict[str, Any] = attr.ib(repr=False, eq=False)


def role_path(roles: Iterable[Any]) -> str:
    """`instructor` for an Instructor or Administrator launch, else `learner`."""
    if any(isinstance(role, str) and role in CONNECT_ROLES for role in roles):
        return PATH_INSTRUCTOR
    return PATH_LEARNER


def require_connect_role(launch: Launch) -> Launch:
    """Gate for starting or completing a course connect (B2 uses it at both
    ends): a launch whose roles are not Instructor or Administrator is
    refused."""
    if role_path(launch.roles) != PATH_INSTRUCTOR:
        raise LaunchRejected("not_instructor", status=403)
    return launch


def read_header(id_token: Any) -> str:
    """Check the token's header and return its `kid`."""
    if not isinstance(id_token, str) or not id_token:
        raise LaunchRejected("malformed_token")
    if len(id_token) > MAX_TOKEN_LENGTH:
        raise LaunchRejected("malformed_token")
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.InvalidTokenError:
        raise LaunchRejected("malformed_token") from None
    if header.get("alg") != SIGNING_ALGORITHM:
        raise LaunchRejected("bad_algorithm")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid or len(kid) > MAX_ID_LENGTH:
        raise LaunchRejected("missing_kid")
    return kid


def _decode(id_token: str, platform: Platform, key: Any) -> Dict[str, Any]:
    try:
        return jwt.decode(
            id_token,
            key=key,
            algorithms=[SIGNING_ALGORITHM],
            audience=platform.client_id,
            issuer=platform.issuer,
            leeway=LEEWAY_SECONDS,
            options={
                "require": ["iss", "aud", "sub", "exp", "iat", "nonce"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_nbf": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
    except jwt.MissingRequiredClaimError:
        raise LaunchRejected("missing_claim") from None
    except jwt.InvalidSignatureError:
        raise LaunchRejected("bad_signature") from None
    except jwt.ExpiredSignatureError:
        raise LaunchRejected("expired") from None
    except (jwt.InvalidIssuedAtError, jwt.ImmatureSignatureError):
        raise LaunchRejected("bad_iat") from None
    except jwt.InvalidIssuerError:
        raise LaunchRejected("bad_issuer") from None
    except jwt.InvalidAudienceError:
        raise LaunchRejected("bad_audience") from None
    except jwt.InvalidAlgorithmError:
        raise LaunchRejected("bad_algorithm") from None
    except jwt.InvalidTokenError:
        raise LaunchRejected("malformed_token") from None


def _short_string(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_ID_LENGTH


def verify_launch(
    id_token: str,
    *,
    platform: Platform,
    key: jwt.PyJWK,
    expected_nonce: str,
    known_deployments: frozenset,
    launch_url: str,
) -> Launch:
    if not platform.approved:
        raise LaunchRejected("platform_not_approved", status=403)
    read_header(id_token)
    claims = _decode(id_token, platform, key.key)

    audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
    azp = claims.get("azp")
    if len(audiences) > 1 and azp is None:
        raise LaunchRejected("bad_azp")
    if azp is not None and azp != platform.client_id:
        raise LaunchRejected("bad_azp")

    nonce = claims["nonce"]
    if not isinstance(nonce, str) or not hmac.compare_digest(
        nonce.encode("utf-8"), expected_nonce.encode("utf-8")
    ):
        raise LaunchRejected("bad_nonce")

    if claims.get(CLAIM_VERSION) != LTI_VERSION:
        raise LaunchRejected("bad_version")
    if claims.get(CLAIM_MESSAGE_TYPE) != MESSAGE_TYPE_RESOURCE_LINK:
        raise LaunchRejected("bad_message_type")

    deployment_id = claims.get(CLAIM_DEPLOYMENT_ID)
    if (
        not isinstance(deployment_id, str)
        or not _short_string(deployment_id)
        or deployment_id not in known_deployments
    ):
        raise LaunchRejected("unknown_deployment", status=403)

    if claims.get(CLAIM_TARGET_LINK_URI) != launch_url:
        raise LaunchRejected("bad_target_link_uri")

    resource_link = claims.get(CLAIM_RESOURCE_LINK)
    if not isinstance(resource_link, dict) or not _short_string(
        resource_link.get("id")
    ):
        raise LaunchRejected("bad_resource_link")

    roles = claims.get(CLAIM_ROLES)
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise LaunchRejected("bad_roles")

    sub = claims["sub"]
    if not isinstance(sub, str) or not _short_string(sub):
        raise LaunchRejected("bad_sub")

    context_id = None
    context = claims.get(CLAIM_CONTEXT)
    if context is not None:
        if not isinstance(context, dict) or not _short_string(context.get("id")):
            raise LaunchRejected("bad_context")
        context_id = context["id"]

    return Launch(
        platform_id=platform.platform_id,
        issuer=platform.issuer,
        sub=sub,
        deployment_id=deployment_id,
        context_id=context_id,
        roles=tuple(roles),
        path=role_path(roles),
        claims=claims,
    )
