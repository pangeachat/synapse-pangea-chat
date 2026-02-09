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

from synapse_pangea_chat.room_preview.get_room_preview import get_room_preview
from synapse_pangea_chat.room_preview.is_rate_limited import is_rate_limited

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.room_preview")


class RoomPreview(Resource):
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

            # Parse rooms parameter from query string
            rooms_param = request.args.get(b"rooms")
            if not rooms_param:
                # No rooms parameter provided, return empty rooms dict
                respond_with_json(
                    request,
                    200,
                    {"rooms": {}},
                    send_cors=True,
                )
                return

            # Extract room IDs from comma-delimited string
            rooms_str = rooms_param[0].decode("utf-8")
            room_ids = [
                room_id.strip() for room_id in rooms_str.split(",") if room_id.strip()
            ]

            if not room_ids:
                # Empty or invalid rooms parameter, return empty rooms dict
                respond_with_json(
                    request,
                    200,
                    {"rooms": {}},
                    send_cors=True,
                )
                return

            rooms_data = await get_room_preview(
                room_ids, self._api, self._datastores.main, self._config
            )

            respond_with_json(
                request,
                200,
                {"rooms": rooms_data},
                send_cors=True,
            )

        except (AuthError, InvalidClientTokenError, MissingClientTokenError) as e:
            logger.info("Authentication failed for room preview request: %s", e)
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
