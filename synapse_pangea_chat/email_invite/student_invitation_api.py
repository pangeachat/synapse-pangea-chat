"""``GET /_synapse/client/pangea/v1/student_invitations?room_id=``: a course's student invitations.

For the course's admins (power level 100) and server admins (the engagement runner): each
invitation's id, when it was sent and by whom, whether it has been used and by whom first, and
how many times. Never the address: the dashboard holds it, and a reminder to it goes through
Synapse by invitation id (invite-by-email.instructions.md).
"""

from __future__ import annotations

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

from synapse_pangea_chat.room_code.constants import (
    EVENT_TYPE_M_ROOM_POWER_LEVELS,
    USERS_POWER_LEVEL_KEY,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.email_invite.student_invitation_api"
)


class StudentInvitationAPI(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, claim_store):
        super().__init__()
        self._api = api
        self._auth = api._hs.get_auth()
        self._store = claim_store.students

    def render_GET(self, request: SynapseRequest):
        run_in_background(self._async_render_GET, request)
        return server.NOT_DONE_YET

    async def _is_course_admin(self, room_id: str, user_id: str) -> bool:
        state = await self._api.get_room_state(
            room_id=room_id, event_filter=[(EVENT_TYPE_M_ROOM_POWER_LEVELS, None)]
        )
        for event in state.values():
            users = event.content.get(USERS_POWER_LEVEL_KEY, {})
            try:
                return int(users.get(user_id, 0)) >= 100
            except (TypeError, ValueError):
                return False
        return False

    async def _async_render_GET(self, request: SynapseRequest):
        try:
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()
            room_ids = request.args.get(b"room_id") or []
            room_id = room_ids[0].decode("utf-8") if room_ids else ""
            if not room_id.startswith("!"):
                respond_with_json(
                    request, 400, {"error": "room_id is required"}, send_cors=True
                )
                return
            if not (
                await self._api.is_user_admin(requester_id)
                or await self._is_course_admin(room_id, requester_id)
            ):
                respond_with_json(request, 403, {"error": "Forbidden"}, send_cors=True)
                return
            rows = await self._store.for_room(room_id)
            respond_with_json(
                request,
                200,
                {"invitations": [self._store.public(r) for r in rows]},
                send_cors=True,
            )
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ):
            respond_with_json(request, 403, {"error": "Forbidden"}, send_cors=True)
        except Exception as e:
            logger.exception("Error in student_invitations: %s", e)
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )
