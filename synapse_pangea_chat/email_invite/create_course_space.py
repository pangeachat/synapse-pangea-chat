"""POST /_synapse/client/pangea/v1/create_course_space

Creates a private Matrix space for a custom course request, generates student
and admin access codes, and sends a branded invite email to the teacher.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

from synapse.api.constants import EventTypes, RoomCreationPreset
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
from synapse.types import create_requester
from twisted.internet import defer
from twisted.web.resource import Resource

from synapse_pangea_chat.room_code.constants import (
    ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    EVENT_TYPE_M_ROOM_JOIN_RULES,
    KNOCK_JOIN_RULE_VALUE,
)
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.generate_room_code import generate_access_code
from synapse_pangea_chat.room_code.get_rooms_with_access_code import (
    get_rooms_with_access_code,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.email_invite.create_course_space"
)

# Matches client defaultSpacePowerLevels
DEFAULT_SPACE_POWER_LEVELS: Dict[str, Any] = {
    "ban": 50,
    "kick": 50,
    "invite": 50,
    "redact": 50,
    "events": {
        "m.room.power_levels": 100,
        "m.room.join_rules": 100,
        "m.space.child": 50,
    },
    "events_default": 0,
    "state_default": 50,
    "users_default": 0,
    "notifications": {"room": 50},
}

# Pangea state event type for course plan association
PANGEA_COURSE_PLAN_STATE_EVENT_TYPE = "pangea.course_plan"


class CreateCourseSpace(Resource):
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

            body = await extract_body_json(request)
            if not isinstance(body, dict):
                respond_with_json(
                    request,
                    400,
                    {"error": "Invalid JSON in request body"},
                    send_cors=True,
                )
                return

            # Validate required fields
            title = body.get("title")
            if not isinstance(title, str) or not title.strip():
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing or empty 'title'"},
                    send_cors=True,
                )
                return

            teacher_email = body.get("teacher_email")
            if not isinstance(teacher_email, str) or not teacher_email.strip():
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing or empty 'teacher_email'"},
                    send_cors=True,
                )
                return

            description = body.get("description", "")
            course_plan_id = body.get("course_plan_id", "")
            image_url = body.get("image_url")

            # Generate two unique access codes
            student_code = await self._generate_unique_code()
            if student_code is None:
                respond_with_json(
                    request,
                    500,
                    {"error": "Failed to generate student access code"},
                    send_cors=True,
                )
                return

            admin_code = await self._generate_unique_code()
            if admin_code is None:
                respond_with_json(
                    request,
                    500,
                    {"error": "Failed to generate admin access code"},
                    send_cors=True,
                )
                return

            # Build initial state events for the space
            initial_state = [
                # Join rules with knock + both access codes
                {
                    "type": EVENT_TYPE_M_ROOM_JOIN_RULES,
                    "state_key": "",
                    "content": {
                        "join_rule": KNOCK_JOIN_RULE_VALUE,
                        ACCESS_CODE_JOIN_RULE_CONTENT_KEY: student_code,
                        ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY: admin_code,
                    },
                },
                # Course plan association
                {
                    "type": PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
                    "state_key": "",
                    "content": {
                        "course_plan_id": course_plan_id,
                    },
                },
            ]

            # Power levels with creator as admin
            power_levels = dict(DEFAULT_SPACE_POWER_LEVELS)
            power_levels["users"] = {requester_id: 100}
            initial_state.append(
                {
                    "type": EventTypes.PowerLevels,
                    "state_key": "",
                    "content": power_levels,
                }
            )

            # Create the space
            room_config = {
                "preset": RoomCreationPreset.PRIVATE_CHAT,
                "name": title,
                "topic": description,
                "creation_content": {"type": "m.space"},
                "initial_state": initial_state,
                "visibility": "private",
            }

            room_creation_handler = self._api._hs.get_room_creation_handler()
            room_id, _, _ = await room_creation_handler.create_room(
                requester=create_requester(
                    requester_id,
                    authenticated_entity=self._api.server_name,
                ),
                config=room_config,
                ratelimit=False,
            )

            # Set room avatar if image URL provided
            if isinstance(image_url, str) and image_url.strip():
                try:
                    await self._api.create_and_send_event_into_room(
                        {
                            "type": EventTypes.RoomAvatar,
                            "room_id": room_id,
                            "sender": requester_id,
                            "state_key": "",
                            "content": {"url": image_url},
                        }
                    )
                except Exception as e:
                    logger.warning(f"Failed to set room avatar: {e}")

            # Build admin join URL (same format the client uses for class links)
            admin_join_url = (
                f"https://pangea.chat/#/join_with_link?classcode={admin_code}"
            )

            # TODO: Send invite email to teacher via invite_by_email
            # (placeholder — invite_by_email endpoint is a separate session)
            logger.info(
                f"Course space created: room_id={room_id}, "
                f"teacher_email={teacher_email}, admin_join_url={admin_join_url}"
            )

            respond_with_json(
                request,
                200,
                {
                    "room_id": room_id,
                    "student_access_code": student_code,
                    "admin_access_code": admin_code,
                    "admin_join_url": admin_join_url,
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
            logger.error(f"Error creating course space: {e}")
            respond_with_json(
                request,
                500,
                {"error": "Internal server error"},
                send_cors=True,
            )

    async def _generate_unique_code(self) -> str | None:
        """Generate an access code that doesn't conflict with existing ones."""
        for _ in range(10):
            code = generate_access_code()
            matches = await get_rooms_with_access_code(
                access_code=code, room_store=self._datastores.main
            )
            if len(matches) == 0:
                return code
        return None
