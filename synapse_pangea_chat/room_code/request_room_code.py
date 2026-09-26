from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

import logging

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
from synapse.module_api import ModuleApi
from twisted.web.resource import Resource

from synapse_pangea_chat.email_invite.course_claims import CourseClaimStore
from synapse_pangea_chat.room_code.code_lookup import new_unique_code

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.room_code.request_room_code"
)


class RequestRoomCode(Resource):
    isLeaf = True

    def __init__(
        self, api: ModuleApi, config: PangeaChatConfig, claim_store: CourseClaimStore
    ):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()
        self._claim_store = claim_store

    def render_GET(self, request: SynapseRequest):
        run_in_background(self._async_render_GET, request)
        return server.NOT_DONE_YET

    async def _async_render_GET(self, request: SynapseRequest):
        try:
            await self._auth.get_user_by_req(request)

            # Free in join rules and among claim codes, which are not in join
            # rules (code_lookup).
            access_code = await new_unique_code(
                self._datastores.main, self._claim_store
            )
            if access_code is None:
                respond_with_json(
                    request,
                    500,
                    {"error": "Failed to generate access code, please try again"},
                    send_cors=True,
                )
                return

            respond_with_json(
                request,
                200,
                {"access_code": access_code},
                send_cors=True,
            )
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ) as e:
            logger.error(f"Forbidden: {e}")
            respond_with_json(
                request,
                403,
                {"error": "Forbidden"},
                send_cors=True,
            )
        except Exception as e:
            logger.error(f"Error processing request: {e}")
            respond_with_json(
                request,
                500,
                {"error": "Internal server error"},
                send_cors=True,
            )
