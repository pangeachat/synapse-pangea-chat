"""HTTP for the Safety page's two endpoints. The rules are in `handlers.py`.

The caller checks run in the order `course_member_emails` runs them: the token
(401), the per-caller rate limit (429), the input (400), then the endpoint's
own admission. The request body is parsed here rather than through Synapse's
helper, because that helper logs the raw body when it is not valid JSON - and
a report's body carries the reporter's reason.
"""

import json
import time
from typing import Any, Dict, List, Optional

from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from twisted.web.resource import Resource

from synapse_pangea_chat.moderation.compat import reraise_if_cancelled
from synapse_pangea_chat.moderation.log_safety import error_site, scrubbing_logger
from synapse_pangea_chat.safety_incidents.handlers import ReadHandler, ReportHandler

logger = scrubbing_logger(
    "synapse.modules.synapse_pangea_chat.moderation.safety_endpoints"
)

_UNAUTHORIZED = {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"}
_RATE_LIMITED = {"error": "Rate limited", "errcode": "M_LIMIT_EXCEEDED"}
_INTERNAL = {"error": "Internal server error", "errcode": "M_UNKNOWN"}

#: Any body larger than this is not a report. Generous - a reason is a
#: sentence - and it bounds what one request can make this process hold.
_MAX_BODY_BYTES = 64 * 1024


class SlidingWindowLimit:
    """At most `limit` calls per caller in any `window_seconds`."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self._limit = max(1, limit)
        self._window = max(1, window_seconds)
        self._log: Dict[str, List[float]] = {}

    def is_limited(self, caller_id: str, now: Optional[float] = None) -> bool:
        current = time.time() if now is None else now
        recent = [
            t for t in self._log.get(caller_id, []) if current - t <= self._window
        ]
        if len(recent) >= self._limit:
            self._log[caller_id] = recent
            return True
        recent.append(current)
        self._log[caller_id] = recent
        return False


def _read_json(request: SynapseRequest) -> Any:
    """The body as JSON, or None. Never logs it."""
    try:
        raw = request.content.read(_MAX_BODY_BYTES + 1)
        if len(raw) > _MAX_BODY_BYTES:
            return None
        return json.loads(raw.decode("utf-8"))
    # silent-ok: an unreadable body is answered 400 by the caller, and its
    # content is the one thing that must not reach a log.
    except Exception:
        return None


class _Base(Resource):
    isLeaf = True

    def __init__(self, homeserver: Any, limit: SlidingWindowLimit) -> None:
        super().__init__()
        self._hs = homeserver
        self._auth = homeserver.get_auth()
        self._limit = limit

    async def _requester(self, request: SynapseRequest) -> Any:
        try:
            return await self._auth.get_user_by_req(request)
        # silent-ok: the caller's own auth failure, answered 401
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ):
            respond_with_json(request, 401, _UNAUTHORIZED, send_cors=True)
            return None

    def _fail(self, request: SynapseRequest, what: str, exc: BaseException) -> None:
        # Site and type only: an exception raised around a report routinely
        # quotes the row it was writing.
        logger.error("%s failed at %s (%s)", what, error_site(exc), type(exc).__name__)
        respond_with_json(request, 500, _INTERNAL, send_cors=True)


class SafetyReport(_Base):
    """`POST /_synapse/client/pangea/v1/report`."""

    def __init__(
        self, homeserver: Any, handler: ReportHandler, limit: SlidingWindowLimit
    ) -> None:
        super().__init__(homeserver, limit)
        self._handler = handler

    def render_POST(self, request: SynapseRequest) -> int:
        run_in_background(self._render, request)
        return server.NOT_DONE_YET

    async def _render(self, request: SynapseRequest) -> None:
        try:
            requester = await self._requester(request)
            if requester is None:
                return
            if self._limit.is_limited(requester.user.to_string()):
                respond_with_json(request, 429, _RATE_LIMITED, send_cors=True)
                return
            status, payload = await self._handler.report(
                requester.user, _read_json(request)
            )
            if status == 200:
                logger.info("safety report recorded as %s", payload["incident_id"])
            respond_with_json(request, status, payload, send_cors=True)
        except Exception as exc:
            reraise_if_cancelled(exc)
            self._fail(request, "safety report", exc)


class SafetyIncidents(_Base):
    """`GET /_synapse/client/pangea/v1/safety_incidents?space_id=`."""

    def __init__(
        self, homeserver: Any, handler: ReadHandler, limit: SlidingWindowLimit
    ) -> None:
        super().__init__(homeserver, limit)
        self._handler = handler

    def render_GET(self, request: SynapseRequest) -> int:
        run_in_background(self._render, request)
        return server.NOT_DONE_YET

    async def _render(self, request: SynapseRequest) -> None:
        try:
            requester = await self._requester(request)
            if requester is None:
                return
            caller_id = requester.user.to_string()
            if self._limit.is_limited(caller_id):
                respond_with_json(request, 429, _RATE_LIMITED, send_cors=True)
                return
            values = request.args.get(b"space_id") or []
            space_id: Optional[str] = None
            if len(values) == 1:
                try:
                    space_id = values[0].decode("utf-8")
                # silent-ok: an undecodable id is answered 400 below
                except UnicodeDecodeError:
                    space_id = None
            status, payload = await self._handler.read(caller_id, space_id)
            if status == 200:
                logger.info(
                    "safety incidents read for %s: %d rows",
                    space_id,
                    len(payload["incidents"]),
                )
            respond_with_json(request, status, payload, send_cors=True)
        except Exception as exc:
            reraise_if_cancelled(exc)
            self._fail(request, "safety incidents read", exc)
