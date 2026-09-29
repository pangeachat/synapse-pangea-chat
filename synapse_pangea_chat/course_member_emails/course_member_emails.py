"""A course admin reads the sign-in email of each student in their course.

``POST /_synapse/client/pangea/v1/course_member_emails`` with ``{"room_id"}``
and the caller's own Matrix token.

Only a course admin (power level 100 in the course space) may call it, and it
answers only for members whose current membership is ``join``: a student who
leaves drops out on the next call. The caller, bots, and other course admins
are never listed. Members with no bound email are omitted from the response.

Every refusal a caller could use to learn something about a room it is not in
(unknown room, not a member, not a space, not an admin) is the same 403 with
the same body, so the endpoint is no oracle for which rooms exist. Email
addresses are never logged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List

from synapse.api.constants import EventTypes
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
from synapse.types import RoomID
from twisted.web.resource import Resource

from synapse_pangea_chat.course_member_emails.is_rate_limited import is_rate_limited
from synapse_pangea_chat.course_member_emails.members import (
    COURSE_ADMIN_POWER_LEVEL,
    effective_power_levels,
    pick_email_per_user,
    visible_member_ids,
)
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.course_member_emails.course_member_emails"
)

MEMBERSHIP_JOIN = "join"
SPACE_ROOM_TYPE = "m.space"
EMAIL_MEDIUM = "email"

# One body for every refusal, so a 403 never says which check failed.
_FORBIDDEN = {"error": "Forbidden: course admin required", "errcode": "M_FORBIDDEN"}


class CourseMemberEmails(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        try:
            requester = await self._auth.get_user_by_req(request)
            caller_id = requester.user.to_string()

            if is_rate_limited(caller_id, self._config):
                respond_with_json(
                    request,
                    429,
                    {"error": "Rate limited", "errcode": "M_LIMIT_EXCEEDED"},
                    send_cors=True,
                )
                return

            body = await extract_body_json(request)
            if not isinstance(body, dict):
                respond_with_json(
                    request,
                    400,
                    {"error": "Request body must be a JSON object"},
                    send_cors=True,
                )
                return

            room_id = body.get("room_id")
            if not isinstance(room_id, str) or not _is_valid_room_id(room_id):
                respond_with_json(
                    request,
                    400,
                    {"error": "'room_id' must be a valid Matrix room ID"},
                    send_cors=True,
                )
                return

            members = await self._visible_members(room_id, caller_id)
            if members is None:
                respond_with_json(request, 403, _FORBIDDEN, send_cors=True)
                return

            emails = await self._emails_for(members)
            payload = [
                {"user_id": user_id, "email": emails[user_id]}
                for user_id in members
                if user_id in emails
            ]
            # Counts only: the addresses are personal data and never logged.
            logger.info(
                "course_member_emails: room=%s caller=%s members=%d with_email=%d",
                room_id,
                caller_id,
                len(members),
                len(payload),
            )
            respond_with_json(request, 200, {"members": payload}, send_cors=True)

        # silent-ok: the caller's auth failure, answered 401 (logged at INFO)
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ) as e:
            logger.info("Authentication failed: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
        except Exception:
            # No request data in the message: nothing here may echo an address.
            logger.exception("Error reading course member emails")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    async def _visible_members(self, room_id: str, caller_id: str) -> List[str] | None:
        """The members whose email the caller may see, or ``None`` when the
        caller may not use this endpoint on this room."""
        (
            caller_membership,
            _,
        ) = await self._datastores.main.get_local_current_membership_for_user_in_room(
            caller_id, room_id
        )
        if caller_membership != MEMBERSHIP_JOIN:
            return None

        state = await self._api.get_room_state(
            room_id=room_id,
            event_filter=[
                (EventTypes.Create, ""),
                (EventTypes.PowerLevels, ""),
                (EventTypes.Member, None),
            ],
        )
        create_event = state.get((EventTypes.Create, ""))
        if create_event is None or create_event.content.get("type") != SPACE_ROOM_TYPE:
            return None

        joined: List[str] = [
            event.state_key
            for event in state.values()
            if event.type == EventTypes.Member
            and isinstance(event.state_key, str)
            and event.content.get("membership") == MEMBERSHIP_JOIN
        ]

        creators: set[str] = set()
        if getattr(create_event.room_version, "msc4289_creator_power_enabled", False):
            additional = create_event.content.get("additional_creators", [])
            if isinstance(additional, list):
                creators.update(c for c in additional if isinstance(c, str))
            creators.add(create_event.sender)

        power_event = state.get((EventTypes.PowerLevels, ""))
        levels = effective_power_levels(
            [*joined, caller_id],
            power_event.content if power_event is not None else None,
            creators,
        )
        if levels[caller_id] < COURSE_ADMIN_POWER_LEVEL:
            return None

        return visible_member_ids(
            joined_member_ids=joined,
            power_levels=levels,
            caller_id=caller_id,
            is_mine=self._api.is_mine,
        )

    async def _emails_for(self, user_ids: List[str]) -> Dict[str, str]:
        if not user_ids:
            return {}
        rows = await self._datastores.main.db_pool.simple_select_many_batch(
            table="user_threepids",
            column="user_id",
            iterable=user_ids,
            keyvalues={"medium": EMAIL_MEDIUM},
            retcols=("user_id", "address", "added_at"),
            desc="pangea_course_member_emails",
        )
        return pick_email_per_user(
            {"user_id": row[0], "address": row[1], "added_at": row[2]} for row in rows
        )


def _is_valid_room_id(room_id: str) -> bool:
    try:
        RoomID.from_string(room_id)
    # silent-ok: validation only - the caller answers 400 for an invalid id
    except Exception:
        return False
    return True
