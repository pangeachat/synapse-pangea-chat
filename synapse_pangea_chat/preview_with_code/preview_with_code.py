from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List

from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from twisted.internet import defer
from twisted.web.resource import Resource

from synapse_pangea_chat.preview_with_code.get_preview import get_room_preview_for_code
from synapse_pangea_chat.preview_with_code.is_rate_limited import is_rate_limited
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.get_rooms_with_access_code import (
    get_rooms_with_access_code,
)

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.preview_with_code.preview_with_code"
)


class PreviewWithCode(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()

    def render_POST(self, request: SynapseRequest):
        defer.ensureDeferred(self._async_render_POST(request))
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest):
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

            body = await extract_body_json(request)
            if not isinstance(body, dict):
                respond_with_json(
                    request,
                    400,
                    {"error": "Invalid JSON in request body"},
                    send_cors=True,
                )
                return

            if "access_code" not in body:
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing 'access_code' in request body"},
                    send_cors=True,
                )
                return
            access_code = body["access_code"]

            if not isinstance(access_code, str):
                respond_with_json(
                    request,
                    400,
                    {"error": "'access_code' must be a string"},
                    send_cors=True,
                )
                return

            if (
                len(access_code) != 7
                or not access_code.isalnum()
                or not any(char.isdigit() for char in access_code)
            ):
                respond_with_json(
                    request,
                    400,
                    {"error": f"Invalid 'access_code': {access_code}"},
                    send_cors=True,
                )
                return

            matches = await get_rooms_with_access_code(
                access_code=access_code, room_store=self._datastores.main
            )
            if matches is None:
                respond_with_json(
                    request,
                    500,
                    {"error": "Internal server error"},
                    send_cors=True,
                )
                return
            if len(matches) == 0:
                respond_with_json(
                    request,
                    400,
                    {"error": f"No rooms found with the access code: {access_code}"},
                    send_cors=True,
                )
                return

            previews: List[Dict[str, Any]] = []
            seen_room_ids: set[str] = set()
            for match in matches:
                if match.room_id in seen_room_ids:
                    continue
                seen_room_ids.add(match.room_id)
                try:
                    preview = await get_room_preview_for_code(
                        room_id=match.room_id,
                        api=self._api,
                        pangea_state_event_types=(
                            self._config.preview_with_code_state_event_types
                        ),
                    )
                except Exception as e:
                    logger.error("Error building preview for %s: %s", match.room_id, e)
                    continue
                if preview is not None:
                    previews.append(preview)

            respond_with_json(
                request,
                200,
                {"rooms": previews},
                send_cors=True,
            )
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ) as e:
            logger.info("Forbidden: %s", e)
            respond_with_json(
                request,
                403,
                {"error": "Forbidden"},
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
