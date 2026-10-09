"""The JSON routes of the Canvas hand-offs: `lti/link` (L1), `lti/connect`
(L2), `lti/course_status` (T10) and `lti/import` (T11).

They are the student invitation routes (token, rate limit, JSON, one 500 with
ids only), plus two things: as every LTI path, the query is dropped from the
URI Synapse logs; and the link step's token is optional (kind `ticket`): a
bound ticket needs none, an unbound one is refused 401 without it. A token
that is sent must be valid.
"""

from __future__ import annotations

from typing import Any

from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http.site import SynapseRequest

from synapse_pangea_chat.lti.endpoints import logged_uri
from synapse_pangea_chat.student_invitations.api import (
    RATE_LIMITED,
    UNAUTHORIZED,
    Result,
    StudentInvitationRoute,
    read_json,
)

KIND_TICKET = "ticket"


class LtiRoute(StudentInvitationRoute):
    def _render(self, request: SynapseRequest, method: str) -> Any:
        if isinstance(request.uri, bytes):
            request.uri = logged_uri(request.uri)
        return super()._render(request, method)

    async def _dispatch(self, request: SynapseRequest) -> Result:
        if self._kind != KIND_TICKET:
            return await super()._dispatch(request)
        if self._limiter.is_rate_limited(request.getClientAddress().host):
            return 429, RATE_LIMITED
        caller = None
        if self._auth.has_access_token(request):
            try:
                requester = await self._auth.get_user_by_req(request)
            # silent-ok: the caller's own auth failure, answered 401
            except (
                MissingClientTokenError,
                InvalidClientTokenError,
                InvalidClientCredentialsError,
                AuthError,
            ):
                return 401, UNAUTHORIZED
            caller = requester.user.to_string()
        return await self._handler(caller, read_json(request))
