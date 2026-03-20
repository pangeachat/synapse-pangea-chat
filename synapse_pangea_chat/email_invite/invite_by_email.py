"""POST /_synapse/client/pangea/v1/invite_by_email

Sends branded invite emails to a list of email addresses, inviting them
to join a Matrix room via its access code join link.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

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
    ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    EVENT_TYPE_M_ROOM_JOIN_RULES,
    EVENT_TYPE_M_ROOM_MEMBER,
    EVENT_TYPE_M_ROOM_POWER_LEVELS,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_JOIN,
    USERS_POWER_LEVEL_KEY,
)
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.email_invite.invite_by_email"
)

# Matrix state event types for room metadata
EVENT_TYPE_M_ROOM_NAME = "m.room.name"
EVENT_TYPE_M_ROOM_TOPIC = "m.room.topic"
EVENT_TYPE_M_ROOM_AVATAR = "m.room.avatar"

# Power level required to use this endpoint (moderator level, so the bot at PL 50 can call it)
REQUIRED_POWER_LEVEL = 50


class InviteByEmail(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._send_email_handler = self._api._hs.get_send_email_handler()
        self._app_name = self._api._hs.config.email.email_app_name

        # Load Jinja2 templates via ModuleApi (searches custom_template_directory first)
        [self._template_html, self._template_text] = self._api.read_templates(
            ["course_invite.html", "course_invite.txt"],
        )

    def render_POST(self, request: SynapseRequest):
        defer.ensureDeferred(self._async_render_POST(request))
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest):
        try:
            # Authenticate caller
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()

            # Parse request body
            body = await extract_body_json(request)
            if not isinstance(body, dict):
                respond_with_json(
                    request,
                    400,
                    {"error": "Invalid JSON in request body"},
                    send_cors=True,
                )
                return

            room_id = body.get("room_id")
            if not isinstance(room_id, str) or not room_id.strip():
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing or empty 'room_id'"},
                    send_cors=True,
                )
                return

            emails = body.get("emails")
            if not isinstance(emails, list) or not emails:
                respond_with_json(
                    request,
                    400,
                    {"error": "'emails' must be a non-empty list"},
                    send_cors=True,
                )
                return

            # Validate each email is a non-empty string
            for email in emails:
                if not isinstance(email, str) or not email.strip():
                    respond_with_json(
                        request,
                        400,
                        {"error": "Each email must be a non-empty string"},
                        send_cors=True,
                    )
                    return

            message: Optional[str] = body.get("message")
            if message is not None and not isinstance(message, str):
                message = None

            # Verify caller has PL100 in the room
            caller_power = await self._get_user_power_level(room_id, requester_id)
            if caller_power < REQUIRED_POWER_LEVEL:
                respond_with_json(
                    request,
                    403,
                    {
                        "error": f"Forbidden — power level {REQUIRED_POWER_LEVEL} required"
                    },
                    send_cors=True,
                )
                return

            # Read room state for email content
            room_name = await self._get_room_name(room_id)
            room_topic = await self._get_room_topic(room_id)
            room_avatar_url = await self._get_room_avatar(room_id)
            access_code = await self._get_access_code(room_id)
            inviter_names = await self._get_inviter_names(room_id)

            if not access_code:
                respond_with_json(
                    request,
                    400,
                    {"error": "Room has no access code in join rules"},
                    send_cors=True,
                )
                return

            base = self._config.app_base_url.rstrip("/")
            join_url = f"{base}/#/join_with_link?classcode={access_code}"

            # Send emails
            emailed: List[str] = []
            errors: List[Dict[str, str]] = []

            for email in emails:
                email = email.strip()
                try:
                    template_vars = {
                        "course_title": room_name or "a course",
                        "course_description": room_topic or "",
                        "course_avatar_url": room_avatar_url or "",
                        "join_url": join_url,
                        "inviter_names": inviter_names,
                        "message": message or "",
                    }

                    html = self._template_html.render(**template_vars)
                    text = self._template_text.render(**template_vars)

                    subject = f"You're invited to join {room_name or 'a course'} on Pangea Chat"

                    await self._send_email_handler.send_email(
                        email_address=email,
                        subject=subject,
                        app_name=self._app_name,
                        html=html,
                        text=text,
                    )
                    emailed.append(email)
                except Exception as e:
                    logger.warning("Failed to send invite to %s: %s", email, e)
                    errors.append({"email": email, "error": str(e)})

            respond_with_json(
                request,
                200,
                {"emailed": emailed, "errors": errors},
                send_cors=True,
            )

        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ):
            respond_with_json(
                request,
                403,
                {"error": "Forbidden"},
                send_cors=True,
            )
        except Exception as e:
            logger.exception("Error in invite_by_email: %s", e)
            respond_with_json(
                request,
                500,
                {"error": "Internal server error"},
                send_cors=True,
            )

    async def _get_user_power_level(self, room_id: str, user_id: str) -> int:
        """Get a user's power level in a room."""
        state_events = await self._api.get_room_state(
            room_id=room_id,
            event_filter=[(EVENT_TYPE_M_ROOM_POWER_LEVELS, None)],
        )
        for event in state_events.values():
            if event.type == EVENT_TYPE_M_ROOM_POWER_LEVELS:
                users = event.content.get(USERS_POWER_LEVEL_KEY, {})
                if user_id in users:
                    try:
                        return int(users[user_id])
                    except (ValueError, TypeError):
                        pass
                # Return users_default if user not explicitly listed
                try:
                    return int(event.content.get("users_default", 0))
                except (ValueError, TypeError):
                    return 0
        return 0

    async def _get_room_name(self, room_id: str) -> Optional[str]:
        state_events = await self._api.get_room_state(
            room_id=room_id,
            event_filter=[(EVENT_TYPE_M_ROOM_NAME, "")],
        )
        for event in state_events.values():
            if event.type == EVENT_TYPE_M_ROOM_NAME:
                return event.content.get("name")
        return None

    async def _get_room_topic(self, room_id: str) -> Optional[str]:
        state_events = await self._api.get_room_state(
            room_id=room_id,
            event_filter=[(EVENT_TYPE_M_ROOM_TOPIC, "")],
        )
        for event in state_events.values():
            if event.type == EVENT_TYPE_M_ROOM_TOPIC:
                return event.content.get("topic")
        return None

    async def _get_room_avatar(self, room_id: str) -> Optional[str]:
        state_events = await self._api.get_room_state(
            room_id=room_id,
            event_filter=[(EVENT_TYPE_M_ROOM_AVATAR, "")],
        )
        for event in state_events.values():
            if event.type == EVENT_TYPE_M_ROOM_AVATAR:
                return event.content.get("url")
        return None

    async def _get_access_code(self, room_id: str) -> Optional[str]:
        state_events = await self._api.get_room_state(
            room_id=room_id,
            event_filter=[(EVENT_TYPE_M_ROOM_JOIN_RULES, "")],
        )
        for event in state_events.values():
            if event.type == EVENT_TYPE_M_ROOM_JOIN_RULES:
                return event.content.get(ACCESS_CODE_JOIN_RULE_CONTENT_KEY)
        return None

    async def _get_inviter_names(self, room_id: str) -> List[str]:
        """Get display names of all human users at the highest power level."""
        # Get power levels
        pl_events = await self._api.get_room_state(
            room_id=room_id,
            event_filter=[(EVENT_TYPE_M_ROOM_POWER_LEVELS, None)],
        )
        users_power: Dict[str, int] = {}
        for event in pl_events.values():
            if event.type == EVENT_TYPE_M_ROOM_POWER_LEVELS:
                users_power = event.content.get(USERS_POWER_LEVEL_KEY, {})
                break

        if not users_power:
            return []

        # Get members to filter to joined users only
        member_events = await self._api.get_room_state(
            room_id=room_id,
            event_filter=[(EVENT_TYPE_M_ROOM_MEMBER, None)],
        )
        joined_members: set = set()
        member_display_names: Dict[str, str] = {}
        for event in member_events.values():
            if event.type != EVENT_TYPE_M_ROOM_MEMBER:
                continue
            if event.content.get(MEMBERSHIP_CONTENT_KEY) != MEMBERSHIP_JOIN:
                continue
            user_id = event.state_key
            joined_members.add(user_id)
            display_name = event.content.get("displayname")
            if display_name:
                member_display_names[user_id] = display_name

        # Find highest power level among joined human users (exclude bots by convention: @bot*)
        highest_pl = -1
        for user_id, pl in users_power.items():
            if user_id not in joined_members:
                continue
            # Skip bot users (localpart starts with "bot")
            localpart = user_id.split(":")[0].lstrip("@")
            if localpart.startswith("bot"):
                continue
            try:
                pl_int = int(pl)
            except (ValueError, TypeError):
                continue
            if pl_int > highest_pl:
                highest_pl = pl_int

        if highest_pl < 0:
            return []

        # Collect all human users at that power level
        names: List[str] = []
        for user_id, pl in users_power.items():
            if user_id not in joined_members:
                continue
            localpart = user_id.split(":")[0].lstrip("@")
            if localpart.startswith("bot"):
                continue
            try:
                pl_int = int(pl)
            except (ValueError, TypeError):
                continue
            if pl_int == highest_pl:
                name = member_display_names.get(user_id, localpart)
                names.append(name)

        return names
