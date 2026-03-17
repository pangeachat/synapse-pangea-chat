from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

import logging
from typing import List

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

from synapse_pangea_chat.room_code.burn_admin_code import burn_admin_code
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.get_inviter_user import promote_user_to_admin
from synapse_pangea_chat.room_code.get_rooms_with_access_code import (
    get_rooms_with_access_code,
)
from synapse_pangea_chat.room_code.invite_user_to_room import invite_user_to_room
from synapse_pangea_chat.room_code.is_rate_limited import is_rate_limited
from synapse_pangea_chat.room_code.user_is_room_member import user_is_room_member

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.room_code.knock_with_code"
)


class KnockWithCode(Resource):
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

            # Check if the request body contains the access code
            if "access_code" not in body:
                logger.error("Missing 'access_code' in request body")
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing 'access_code' in request body"},
                    send_cors=True,
                )
                return
            access_code = body["access_code"]

            # Check if the access code is a string and has the correct format
            if not isinstance(access_code, str):
                logger.error("'access_code' must be a string")
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
                or not any(char.isdigit() for char in access_code)  # At least one digit
            ):
                logger.error(f"Invalid 'access_code': {access_code}")
                respond_with_json(
                    request,
                    400,
                    {"error": f"Invalid 'access_code': {access_code}"},
                    send_cors=True,
                )
                return

            # Get the rooms with the access code
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

            # Send knock with access code to the rooms as requester
            invited_rooms: List[str] = []
            already_joined_rooms: List[str] = []
            for match in matches:
                try:
                    is_member = await user_is_room_member(
                        api=self._api,
                        user_id=requester_id,
                        room_id=match.room_id,
                    )
                    if is_member:
                        already_joined_rooms.append(match.room_id)
                        continue
                    await invite_user_to_room(
                        api=self._api,
                        user_id=requester_id,
                        room_id=match.room_id,
                    )
                    invited_rooms.append(match.room_id)

                    # Admin code: promote to admin and burn the code
                    if match.is_admin_code:
                        await promote_user_to_admin(
                            api=self._api,
                            room_id=match.room_id,
                            user_to_promote=requester_id,
                            invite_power=100,
                        )
                        await burn_admin_code(
                            api=self._api,
                            room_id=match.room_id,
                            burner_user_id=requester_id,
                        )
                except Exception as e:
                    logger.error(
                        f"Error sending knock with code to {match.room_id}: {e}"
                    )
            respond_with_json(
                request,
                200,
                {
                    "message": f"Invited {requester_id}",
                    "rooms": invited_rooms,
                    "already_joined": already_joined_rooms,
                },
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
