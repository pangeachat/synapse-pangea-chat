from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from synapse.api.errors import (
    AuthError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from twisted.internet import defer
from twisted.web.resource import Resource

from synapse_pangea_chat.activity_session_previews.get_activity_session_previews import (
    get_activity_session_previews,
)
from synapse_pangea_chat.room_preview.is_rate_limited import is_rate_limited

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.activity_session_previews"
)


class ActivitySessionPreviews(Resource):
    """GET /_synapse/client/pangea/v1/activity_session_previews

    Space-scoped session discovery: ?rooms is a comma-delimited list of course
    space ids, ?activity an optional activity id filter. Responds with the same
    shape as room_preview, for only the activity-session child rooms of the
    spaces the caller is joined to. Shares room_preview's per-user rate
    limiter. See activity-session-previews.instructions.md.
    """

    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()

    def render_GET(self, request: SynapseRequest):
        defer.ensureDeferred(self._async_render_GET(request))
        return server.NOT_DONE_YET

    async def _async_render_GET(self, request: SynapseRequest):
        try:
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()
            if is_rate_limited(requester_id, self._config):
                respond_with_json(
                    request,
                    429,
                    {"error": "Rate limited"},
                    send_cors=True,
                )
                return

            # Parse the space ids from the query string (same CSV shape as
            # room_preview's rooms param).
            rooms_param = request.args.get(b"rooms")
            space_ids = []
            if rooms_param:
                rooms_str = rooms_param[0].decode("utf-8")
                space_ids = [
                    space_id.strip()
                    for space_id in rooms_str.split(",")
                    if space_id.strip()
                ]

            if not space_ids:
                # No spaces to answer for — same graceful empty response as
                # room_preview with no rooms.
                respond_with_json(
                    request,
                    200,
                    {"rooms": {}},
                    send_cors=True,
                )
                return

            # Optional single-activity scope.
            activity_param = request.args.get(b"activity")
            activity_id = None
            if activity_param:
                activity_id = activity_param[0].decode("utf-8").strip() or None

            rooms_data = await get_activity_session_previews(
                space_ids,
                activity_id,
                requester_id,
                self._api,
                self._datastores.main,
                self._config,
            )

            respond_with_json(
                request,
                200,
                {"rooms": rooms_data},
                send_cors=True,
            )

        except (AuthError, InvalidClientTokenError, MissingClientTokenError) as e:
            logger.info(
                "Authentication failed for activity session previews request: %s", e
            )
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
        except Exception as e:
            logger.error("Error processing request: %s", e)
            respond_with_json(
                request,
                500,
                {"error": "Internal server error"},
                send_cors=True,
            )
