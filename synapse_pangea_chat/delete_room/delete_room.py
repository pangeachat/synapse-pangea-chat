from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.handlers.pagination import PURGE_ROOM_ACTION_NAME
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from twisted.web.resource import Resource

from synapse_pangea_chat.delete_room.cleanup_space_relationships import (
    cleanup_space_relationships,
)
from synapse_pangea_chat.delete_room.constants import (
    MEMBERSHIP_LEAVE,
    ROOM_DELETED_CONTENT_KEY,
    ROOM_DELETED_REASON,
)
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
        self._task_scheduler = self._api._hs.get_task_scheduler()
        self._clock = self._api._hs.get_clock()

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
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
                    403,
                    {"error": "Forbidden. Not a member of the room"},
                    send_cors=True,
                )
                return

            # Ensure request has highest power level
            if not await user_has_highest_power_level(self._api, requester_id, room_id):
                respond_with_json(
                    request,
                    403,
                    {"error": "Forbidden. Not the highest power level"},
                    send_cors=True,
                )

                return

            # Clean up space relationships before purging the room
            await cleanup_space_relationships(self._api, room_id, requester_id)

            for user in room_members_ids:
                try:
                    # Only works for local users; remote members can't be
                    # removed from here (accepted: single-homeserver deployment)
                    await self._api.update_room_membership(
                        user,
                        user,
                        room_id,
                        MEMBERSHIP_LEAVE,
                        content={
                            "reason": ROOM_DELETED_REASON,
                            ROOM_DELETED_CONTENT_KEY: True,
                        },
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to remove membership for %s in %s: %s", user, room_id, e
                    )

            # Defer the purge so the leave events stay available for members'
            # clients to sync; without force, the purge fails safe if a local
            # user rejoins during the delay window
            await self._task_scheduler.schedule_task(
                PURGE_ROOM_ACTION_NAME,
                resource_id=room_id,
                timestamp=self._clock.time_msec()
                + self._config.delete_room_purge_delay_seconds * 1000,
            )

            logger.info(
                "Room %s deleted by %s (%d members removed, purge in %ds)",
                room_id,
                requester_id,
                len(room_members_ids),
                self._config.delete_room_purge_delay_seconds,
            )

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
