from __future__ import annotations

import json
import logging
import time
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

from synapse_pangea_chat.room_code.constants import (
    EVENT_TYPE_M_ROOM_MEMBER,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_JOIN,
)
from synapse_pangea_chat.room_code.invite_user_to_room import invite_user_to_room

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.request_auto_join")

# Module-level rate limiter state
_request_log: Dict[str, List[float]] = {}


def _is_rate_limited(user_id: str, config: PangeaChatConfig) -> bool:
    current_time = time.time()

    if user_id not in _request_log:
        _request_log[user_id] = []

    _request_log[user_id] = [
        timestamp
        for timestamp in _request_log[user_id]
        if current_time - timestamp <= config.request_auto_join_burst_duration_seconds
    ]

    if len(_request_log[user_id]) >= config.request_auto_join_requests_per_burst:
        return True

    _request_log[user_id].append(current_time)
    return False


async def _extract_body_json(request: SynapseRequest) -> Any:
    content_type = request.getHeader("Content-Type")
    if content_type is None:
        return None
    if not content_type.lower().strip().startswith("application/json"):
        return None
    try:
        body = request.content.read()
        body_str = body.decode("utf-8")
        return json.loads(body_str)
    except Exception as e:
        logger.error("Failed to parse request body: %s", e)
        return None


class RequestAutoJoin(Resource):
    """HTTP endpoint that invites a user to a room they were previously a member of.

    When a user leaves a room (e.g., leaves a course and all subchats) and later
    tries to rejoin via standard Matrix /join, it can fail if no remaining member
    has invite power. This endpoint finds (or promotes) a member with invite
    power and sends an invite on the user's behalf.

    The existing auto_accept_invite callback detects that the invite replaces a
    previous knock/leave and auto-joins the user.

    POST /_synapse/client/unstable/org.pangea/v1/request_auto_join
    Body: { "room_id": "!abc:example.com" }
    """

    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()

    def render_POST(self, request: SynapseRequest):
        defer.ensureDeferred(self._async_render_POST(request))
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest):
        try:
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()

            if _is_rate_limited(requester_id, self._config):
                respond_with_json(
                    request, 429, {"error": "Rate limited"}, send_cors=True
                )
                return

            body = await _extract_body_json(request)
            if not isinstance(body, dict):
                respond_with_json(
                    request,
                    400,
                    {"error": "Invalid JSON in request body"},
                    send_cors=True,
                )
                return

            room_id = body.get("room_id")
            if not isinstance(room_id, str) or not room_id:
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing or invalid 'room_id'"},
                    send_cors=True,
                )
                return

            # Verify requester was previously a member (leave or knock)
            was_previous_member = await self._was_previous_member(requester_id, room_id)
            if not was_previous_member:
                respond_with_json(
                    request,
                    403,
                    {"error": "User was not previously a member of this room"},
                    send_cors=True,
                )
                return

            # Verify requester is not currently joined
            is_currently_joined = await self._is_currently_joined(requester_id, room_id)
            if is_currently_joined:
                respond_with_json(
                    request,
                    400,
                    {"error": "User is already a member of this room"},
                    send_cors=True,
                )
                return

            logger.info(
                "Processing auto-join request for user %s in room %s",
                requester_id,
                room_id,
            )

            await invite_user_to_room(
                api=self._api,
                user_id=requester_id,
                room_id=room_id,
            )

            respond_with_json(
                request,
                200,
                {"message": "Invited user", "room_id": room_id},
                send_cors=True,
            )

        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ) as e:
            logger.error("Forbidden: %s", e)
            respond_with_json(request, 403, {"error": "Forbidden"}, send_cors=True)

        except Exception as e:
            logger.error("Error processing request_auto_join: %s", e)
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    async def _was_previous_member(self, user_id: str, room_id: str) -> bool:
        """Check if the user was ever a joined member or has knocked on the room."""
        try:
            member_state_events = await self._api.get_room_state(
                room_id=room_id,
                event_filter=[(EVENT_TYPE_M_ROOM_MEMBER, user_id)],
            )
            for state_event in member_state_events.values():
                if (
                    state_event.type != EVENT_TYPE_M_ROOM_MEMBER
                    or state_event.state_key != user_id
                ):
                    continue
                membership = state_event.content.get(MEMBERSHIP_CONTENT_KEY)
                # User has a membership record — they were involved with this room
                if membership is not None:
                    return True
            return False
        except Exception as e:
            logger.error(
                "Failed to check previous membership for %s in %s: %s",
                user_id,
                room_id,
                e,
            )
            return False

    async def _is_currently_joined(self, user_id: str, room_id: str) -> bool:
        """Check if the user is currently a joined member of the room."""
        try:
            member_state_events = await self._api.get_room_state(
                room_id=room_id,
                event_filter=[(EVENT_TYPE_M_ROOM_MEMBER, user_id)],
            )
            for state_event in member_state_events.values():
                if (
                    state_event.type != EVENT_TYPE_M_ROOM_MEMBER
                    or state_event.state_key != user_id
                ):
                    continue
                membership = state_event.content.get(MEMBERSHIP_CONTENT_KEY)
                if membership == MEMBERSHIP_JOIN:
                    return True
            return False
        except Exception as e:
            logger.error(
                "Failed to check current membership for %s in %s: %s",
                user_id,
                room_id,
                e,
            )
            return False
