"""Names and Role Provisioning Services (NRPS 2.0): a Canvas course's roster.

The tool asks the platform's token endpoint for an access token with the OAuth2
client-credentials grant, authenticating with a JWT signed by its own key
(1EdTech Security Framework §4.1), for the NRPS `contextmembership.readonly`
scope; then it reads the course's membership container, following the
`rel="next"` Link header. One token request per import and one request per
page: a refusal is reported, never retried here. The token goes only to the
NRPS URL's own host: a next link elsewhere is refused unfetched.

Only active learners are returned, as (Canvas user id, email or None). Nothing
here logs: a failure is an `UpstreamError` carrying a fixed reason code.
"""

from __future__ import annotations

import secrets
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, Tuple

import attr

from synapse_pangea_chat.lti.http import UpstreamError, host_of, https_url
from synapse_pangea_chat.lti.keys import ToolKey
from synapse_pangea_chat.lti.registration import NRPS_SCOPE
from synapse_pangea_chat.lti.store import Platform

NRPS_CLAIM = "https://purl.imsglobal.org/spec/lti-nrps/claim/namesroleservice"
MEMBERSHIP_MEDIA_TYPE = "application/vnd.ims.lti-nrps.v2.membershipcontainer+json"
CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ASSERTION_LIFETIME_SECONDS = 300
# 200 pages is far beyond any course (Canvas pages hold 10-50 members); the
# cap stops a platform that links pages forever.
MAX_PAGES = 200
MAX_TOKEN_LENGTH = 4096
MAX_ID_LENGTH = 255
LEARNER_ROLES = frozenset(
    {"http://purl.imsglobal.org/vocab/lis/v2/membership#Learner", "Learner"}
)


class Http(Protocol):
    async def post_form(self, url: str, fields: Dict[str, str]) -> Any:
        ...

    async def get_page(
        self, url: str, bearer: str, accept: str
    ) -> Tuple[Any, Optional[str]]:
        ...


@attr.s(frozen=True, auto_attribs=True)
class Learner:
    # Kept out of repr: both identify a student.
    user_id: str = attr.ib(repr=False)
    email: Optional[str] = attr.ib(repr=False)


def nrps_url_from_launch(claims: Dict[str, Any]) -> Optional[str]:
    """The launch's NRPS 2.0 membership URL, or None."""
    service = claims.get(NRPS_CLAIM)
    if not isinstance(service, dict):
        return None
    versions = service.get("service_versions")
    if not isinstance(versions, list) or "2.0" not in versions:
        return None
    return https_url(service.get("context_memberships_url"))


def _learner(member: Any) -> Optional[Learner]:
    if not isinstance(member, dict):
        return None
    user_id = member.get("user_id")
    if not isinstance(user_id, str) or not 0 < len(user_id) <= MAX_ID_LENGTH:
        return None
    roles = member.get("roles")
    if not isinstance(roles, list) or not LEARNER_ROLES.intersection(
        r for r in roles if isinstance(r, str)
    ):
        return None
    if member.get("status", "Active") != "Active":
        return None
    email = member.get("email")
    return Learner(user_id=user_id, email=email if isinstance(email, str) else None)


class NrpsClient:
    def __init__(self, http: Http, key: ToolKey, clock_seconds: Callable[[], float]):
        self._http = http
        self._key = key
        self._clock_seconds = clock_seconds

    async def learners(
        self, platform: Platform, nrps_url: str, context_id: str
    ) -> List[Learner]:
        """Every active learner of the linked Canvas course. Raises
        `UpstreamError` on any failure, having returned nothing."""
        token = await self._token(platform)
        host = host_of(nrps_url)
        learners: List[Learner] = []
        seen: Set[str] = set()
        pages = 0
        url: Optional[str] = nrps_url
        while url is not None:
            if pages >= MAX_PAGES or url in seen:
                # A page already read, or more pages than any course has.
                raise UpstreamError("paging_loop")
            pages += 1
            seen.add(url)
            document, following = await self._http.get_page(
                url, token, MEMBERSHIP_MEDIA_TYPE
            )
            if not isinstance(document, dict) or not isinstance(
                document.get("members"), list
            ):
                raise UpstreamError("bad_membership")
            context = document.get("context")
            if isinstance(context, dict) and context.get("id") != context_id:
                raise UpstreamError("wrong_context")
            learners.extend(
                learner
                for learner in (_learner(m) for m in document["members"])
                if learner is not None
            )
            if following is not None and (
                https_url(following) is None or host_of(following) != host
            ):
                # The bearer token never leaves the NRPS URL's own host.
                raise UpstreamError("bad_next_link")
            url = following
        return learners

    async def _token(self, platform: Platform) -> str:
        now = int(self._clock_seconds())
        assertion = self._key.sign(
            {
                "iss": platform.client_id,
                "sub": platform.client_id,
                "aud": platform.token_url,
                "iat": now,
                "exp": now + ASSERTION_LIFETIME_SECONDS,
                "jti": secrets.token_urlsafe(24),
            }
        )
        answer = await self._http.post_form(
            platform.token_url,
            {
                "grant_type": "client_credentials",
                "client_assertion_type": CLIENT_ASSERTION_TYPE,
                "client_assertion": assertion,
                "scope": NRPS_SCOPE,
            },
        )
        if not isinstance(answer, dict):
            raise UpstreamError("bad_token_response")
        token = answer.get("access_token")
        if not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN_LENGTH:
            raise UpstreamError("bad_token_response")
        token_type = answer.get("token_type")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise UpstreamError("bad_token_response")
        expires_in = answer.get("expires_in")
        if expires_in is not None and (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or expires_in <= 0
        ):
            raise UpstreamError("expired_token")
        return token
