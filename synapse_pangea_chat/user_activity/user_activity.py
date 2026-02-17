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

from synapse_pangea_chat.user_activity.get_user_activity import get_user_activity
from synapse_pangea_chat.user_activity.is_rate_limited import is_rate_limited

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.user_activity")


class UserActivity(Resource):
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

            # Admin-only: check if requester is a server admin
            is_admin = await self._api.is_user_admin(requester_id)
            if not is_admin:
                respond_with_json(
                    request,
                    403,
                    {"error": "Forbidden: server admin required"},
                    send_cors=True,
                )
                return

            if is_rate_limited(requester_id, self._config):
                respond_with_json(
                    request,
                    429,
                    {"error": "Rate limited"},
                    send_cors=True,
                )
                return

            user_activity_data = await get_user_activity(self._datastores.main)

            respond_with_json(
                request,
                200,
                {"users": user_activity_data},
                send_cors=True,
            )

        except (AuthError, InvalidClientTokenError, MissingClientTokenError) as e:
            logger.info("Authentication failed for user activity request: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Error processing user activity request")
            respond_with_json(
                request,
                500,
                {"error": "Internal server error"},
                send_cors=True,
            )
