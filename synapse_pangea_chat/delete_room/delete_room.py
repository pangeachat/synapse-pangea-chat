from __future__ import annotations

import logging
from typing import TYPE_CHECKING

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

from synapse_pangea_chat.delete_room.cleanup_space_relationships import (
    cleanup_space_relationships,
)
from synapse_pangea_chat.delete_room.constants import MEMBERSHIP_LEAVE
from synapse_pangea_chat.delete_room.extract_body_json import extract_body_json
from synapse_pangea_chat.delete_room.get_room_members import get_room_members
from synapse_pangea_chat.delete_room.is_rate_limited import is_rate_limited
from synapse_pangea_chat.delete_room.user_has_highest_power_level import (
    user_has_highest_power_level,
)

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.delete_room")


class DeleteRoom(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()
        self._pagination_handler = self._api._hs.get_pagination_handler()

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
            # Extract body
            body = await extract_body_json(request)
            if not isinstance(body, dict):
                respond_with_json(
                    request,
                    400,
                    {"error": "Invalid JSON in request body"},
                    send_cors=True,
                )
                return

            # Validate body
            room_id = body.get("room_id", None)
            if not isinstance(room_id, str):
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing or invalid room_id"},
                    send_cors=True,
                )
                return

            # Ensure requester is member of the room
            room_members_ids = await get_room_members(self._api, room_id)
            is_member = requester_id in room_members_ids
            if not is_member:
                respond_with_json(
                    request,
                    400,
                    {"error": "Bad request. Not a member of the room"},
                    send_cors=True,
                )
                return

            # Ensure request has highest power level
            if not await user_has_highest_power_level(self._api, requester_id, room_id):
                respond_with_json(
                    request,
                    400,
                    {"error": "Bad request. Not the highest power level"},
                    send_cors=True,
                )

                return

            # Clean up space relationships before purging the room
            await cleanup_space_relationships(self._api, room_id, requester_id)

            for user in room_members_ids:
                try:
                    # Try to use the module API for all users, both local and remote
                    await self._api.update_room_membership(
                        user, user, room_id, MEMBERSHIP_LEAVE
                    )
                except Exception as e:
                    logger.error(
                        "Failed to remove membership for %s in %s: %s", user, room_id, e
                    )

            await self._pagination_handler.purge_room(room_id, force=True)

            respond_with_json(
                request,
                200,
                {"message": "Deleted"},
                send_cors=True,
            )
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ) as e:
            logger.error("Forbidden: %s", e)
            respond_with_json(
                request,
                403,
                {"error": "Forbidden"},
                send_cors=True,
            )

        except Exception as e:
            logger.error("Unexpected error: %s", e)
            respond_with_json(
                request,
                500,
                {"error": "Internal server error"},
                send_cors=True,
            )
