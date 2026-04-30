from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

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
from synapse.module_api import ModuleApi
from synapse.types import RoomID
from twisted.internet import defer
from twisted.web.resource import Resource

from synapse_pangea_chat.room_code.extract_body_json import extract_body_json

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.grant_instructor_analytics_access.grant_instructor_analytics_access"
)

MEMBERSHIP_INVITE = "invite"
MEMBERSHIP_JOIN = "join"

COURSE_SETTINGS_STATE_EVENT_TYPE = "pangea.course_settings"
REQUIRE_ANALYTICS_ACCESS_KEY = "require_analytics_access"
ANALYTICS_ROOM_TYPE = "p.analytics"


def _is_probable_bot_user_id(user_id: str) -> bool:
    if not user_id.startswith("@") or ":" not in user_id:
        return False
    localpart = user_id[1:].split(":", 1)[0]
    return (
        localpart == "bot" or localpart.startswith("bot-") or localpart.endswith("-bot")
    )


class GrantInstructorAnalyticsAccess(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()
        self._storage_controllers = self._api._hs.get_storage_controllers()

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
                    {"error": "Request body must be a JSON object"},
                    send_cors=True,
                )
                return

            mx_course_id = body.get("mx_course_id")
            if not isinstance(mx_course_id, str) or not self._is_valid_room_id(
                mx_course_id
            ):
                respond_with_json(
                    request,
                    400,
                    {"error": "'mx_course_id' must be a valid Matrix room ID"},
                    send_cors=True,
                )
                return

            mx_analytics_room_id = body.get("mx_analytics_room_id")
            if not isinstance(mx_analytics_room_id, str) or not self._is_valid_room_id(
                mx_analytics_room_id
            ):
                respond_with_json(
                    request,
                    400,
                    {
                        "error": (
                            "'mx_analytics_room_id' must be a valid Matrix room ID"
                        )
                    },
                    send_cors=True,
                )
                return

            (
                caller_membership,
                _,
            ) = await self._datastores.main.get_local_current_membership_for_user_in_room(
                requester_id, mx_course_id
            )
            if caller_membership != MEMBERSHIP_JOIN:
                respond_with_json(
                    request,
                    403,
                    {"error": "Caller is not a joined member of mx_course_id"},
                    send_cors=True,
                )
                return

            settings_event = (
                await self._storage_controllers.state.get_current_state_event(
                    mx_course_id, COURSE_SETTINGS_STATE_EVENT_TYPE, ""
                )
            )
            if (
                settings_event is None
                or settings_event.content.get(REQUIRE_ANALYTICS_ACCESS_KEY) is not True
            ):
                respond_with_json(
                    request,
                    403,
                    {"error": "Course does not require analytics access"},
                    send_cors=True,
                )
                return

            target_create_event = (
                await self._storage_controllers.state.get_current_state_event(
                    mx_analytics_room_id, EventTypes.Create, ""
                )
            )
            if target_create_event is None:
                respond_with_json(
                    request,
                    404,
                    {"error": "Analytics room not found"},
                    send_cors=True,
                )
                return

            if target_create_event.content.get("type") != ANALYTICS_ROOM_TYPE:
                respond_with_json(
                    request,
                    403,
                    {"error": "Target room is not an analytics room"},
                    send_cors=True,
                )
                return

            if target_create_event.sender != requester_id:
                respond_with_json(
                    request,
                    403,
                    {"error": "Caller did not create the analytics room"},
                    send_cors=True,
                )
                return

            instructor_ids = await self._get_course_instructor_ids(
                mx_course_id=mx_course_id, caller_id=requester_id
            )

            instructors_joined: list[dict[str, Any]] = []
            errors: list[dict[str, str]] = []
            for instructor_id in instructor_ids:
                try:
                    action = await self._force_join_instructor(
                        mx_analytics_room_id=mx_analytics_room_id,
                        instructor_id=instructor_id,
                        inviter_id=requester_id,
                    )
                    instructors_joined.append(
                        {"user_id": instructor_id, "action": action}
                    )
                except Exception as e:
                    logger.info(
                        "Failed force-joining %s into %s: %s",
                        instructor_id,
                        mx_analytics_room_id,
                        e,
                    )
                    errors.append(
                        {
                            "user_id": instructor_id,
                            "error": str(e) or "Force-join failed",
                        }
                    )

            respond_with_json(
                request,
                200,
                {
                    "mx_course_id": mx_course_id,
                    "mx_analytics_room_id": mx_analytics_room_id,
                    "instructors_joined": instructors_joined,
                    "errors": errors,
                },
                send_cors=True,
            )
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
            logger.exception("Error granting instructor analytics access")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    def _is_valid_room_id(self, room_id: str) -> bool:
        try:
            RoomID.from_string(room_id)
        except Exception:
            return False
        return True

    async def _get_course_instructor_ids(
        self, mx_course_id: str, caller_id: str
    ) -> list[str]:
        room_state = await self._api.get_room_state(
            room_id=mx_course_id,
            event_filter=[
                (EventTypes.Create, ""),
                (EventTypes.PowerLevels, ""),
                (EventTypes.Member, None),
            ],
        )

        create_event = room_state.get((EventTypes.Create, ""))
        power_levels_event = room_state.get((EventTypes.PowerLevels, ""))

        joined_members: set[str] = set()
        for state_event in room_state.values():
            if state_event.type != EventTypes.Member:
                continue
            if state_event.content.get("membership") != MEMBERSHIP_JOIN:
                continue
            user_id = state_event.state_key
            if not isinstance(user_id, str):
                continue
            joined_members.add(user_id)

        if not joined_members:
            return []

        users_default = 0
        users_power_levels: dict[str, Any] = {}
        creator_power_users: set[str] = set()

        if power_levels_event is not None:
            users_default = self._coerce_int(
                power_levels_event.content.get("users_default"), 0
            )
            raw_users_power_levels = power_levels_event.content.get("users", {})
            if isinstance(raw_users_power_levels, dict):
                users_power_levels = dict(raw_users_power_levels)

        if (
            create_event is not None
            and create_event.room_version.msc4289_creator_power_enabled
        ):
            creators = create_event.content.get("additional_creators", []) + [
                create_event.sender
            ]
            for creator in creators:
                if isinstance(creator, str) and creator in joined_members:
                    creator_power_users.add(creator)

        member_powers: dict[str, int] = {}
        for member_id in joined_members:
            if member_id in creator_power_users:
                member_powers[member_id] = 100
            elif member_id in users_power_levels:
                member_powers[member_id] = self._coerce_int(
                    users_power_levels[member_id], users_default
                )
            else:
                member_powers[member_id] = users_default

        caller_power = member_powers.get(caller_id, users_default)

        candidates = [
            (user_id, power)
            for user_id, power in member_powers.items()
            if power > caller_power
            and user_id != caller_id
            and self._api.is_mine(user_id)
            and not _is_probable_bot_user_id(user_id)
        ]
        if not candidates:
            return []

        max_power = max(power for _, power in candidates)
        return sorted(user_id for user_id, power in candidates if power == max_power)

    async def _force_join_instructor(
        self, mx_analytics_room_id: str, instructor_id: str, inviter_id: str
    ) -> str:
        (
            current_membership,
            _,
        ) = await self._datastores.main.get_local_current_membership_for_user_in_room(
            instructor_id, mx_analytics_room_id
        )

        if current_membership == MEMBERSHIP_JOIN:
            return "already_joined"

        if current_membership != MEMBERSHIP_INVITE:
            await self._api.update_room_membership(
                sender=inviter_id,
                target=instructor_id,
                room_id=mx_analytics_room_id,
                new_membership=MEMBERSHIP_INVITE,
            )

        await self._api.update_room_membership(
            sender=instructor_id,
            target=instructor_id,
            room_id=mx_analytics_room_id,
            new_membership=MEMBERSHIP_JOIN,
        )
        return "joined"

    def _coerce_int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
