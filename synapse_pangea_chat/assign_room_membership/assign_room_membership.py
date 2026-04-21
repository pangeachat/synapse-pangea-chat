from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from synapse.api.constants import EventTypes, JoinRules
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
    "synapse.module.synapse_pangea_chat.assign_room_membership.assign_room_membership"
)

MEMBERSHIP_BAN = "ban"
MEMBERSHIP_INVITE = "invite"
MEMBERSHIP_JOIN = "join"


class AssignRoomMembership(Resource):
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

            if not await self._api.is_user_admin(requester_id):
                respond_with_json(
                    request, 403, {"error": "Admin access required"}, send_cors=True
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
            if not isinstance(room_id, str) or not self._is_valid_room_id(room_id):
                respond_with_json(
                    request,
                    400,
                    {"error": "'room_id' must be a valid room ID"},
                    send_cors=True,
                )
                return

            force_join = body.get("force_join")
            if not isinstance(force_join, bool):
                respond_with_json(
                    request,
                    400,
                    {"error": "'force_join' must be a boolean"},
                    send_cors=True,
                )
                return

            user_ids = self._extract_user_ids(body)
            if user_ids is None:
                respond_with_json(
                    request,
                    400,
                    {
                        "error": (
                            "'user_ids' must be a non-empty array of distinct local user IDs"
                        )
                    },
                    send_cors=True,
                )
                return

            canonical_user_ids = await self._validate_local_users(user_ids)
            if canonical_user_ids is None:
                respond_with_json(
                    request,
                    400,
                    {
                        "error": (
                            "'user_ids' must be a non-empty array of distinct local user IDs"
                        )
                    },
                    send_cors=True,
                )
                return

            if not await self._room_exists(room_id):
                respond_with_json(
                    request,
                    404,
                    {"error": "Room not found"},
                    send_cors=True,
                )
                return

            results = []
            for user_id in canonical_user_ids:
                results.append(
                    await self._assign_user(
                        requester=requester,
                        room_id=room_id,
                        user_id=user_id,
                        force_join=force_join,
                    )
                )

            respond_with_json(
                request,
                200,
                {
                    "room_id": room_id,
                    "force_join": force_join,
                    "results": results,
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
            logger.exception("Error assigning room membership")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    def _is_valid_room_id(self, room_id: str) -> bool:
        try:
            RoomID.from_string(room_id)
        except Exception:
            return False
        return True

    def _extract_user_ids(self, body: Any) -> list[str] | None:
        user_ids = body.get("user_ids")
        if not isinstance(user_ids, list) or not user_ids:
            return None

        if not all(isinstance(user_id, str) for user_id in user_ids):
            return None

        if len(user_ids) != len(set(user_ids)):
            return None

        return user_ids

    async def _validate_local_users(self, user_ids: list[str]) -> list[str] | None:
        canonical_user_ids: list[str] = []
        seen_user_ids: set[str] = set()

        for user_id in user_ids:
            canonical_user_id = await self._api.check_user_exists(user_id)
            if canonical_user_id is None or not self._api.is_mine(canonical_user_id):
                return None
            if canonical_user_id in seen_user_ids:
                return None
            seen_user_ids.add(canonical_user_id)
            canonical_user_ids.append(canonical_user_id)

        return canonical_user_ids

    async def _room_exists(self, room_id: str) -> bool:
        create_event = await self._storage_controllers.state.get_current_state_event(
            room_id, EventTypes.Create, ""
        )
        return create_event is not None

    async def _assign_user(
        self,
        requester: Any,
        room_id: str,
        user_id: str,
        force_join: bool,
    ) -> dict[str, Any]:
        (
            current_membership,
            _,
        ) = await self._datastores.main.get_local_current_membership_for_user_in_room(
            user_id, room_id
        )

        if current_membership == MEMBERSHIP_JOIN:
            return self._success_result(user_id, "already_joined")

        if current_membership == MEMBERSHIP_INVITE and not force_join:
            return self._success_result(user_id, "already_invited")

        if current_membership == MEMBERSHIP_BAN:
            return self._failure_result(user_id, "User is banned from room")

        try:
            if force_join:
                await self._join_user_as_admin(
                    user_id=user_id,
                    room_id=room_id,
                    current_membership=current_membership,
                )
                return self._success_result(user_id, "joined")

            await self._invite_user_as_admin(
                user_id=user_id,
                room_id=room_id,
            )
            return self._success_result(user_id, "invited")
        except Exception as e:
            logger.info(
                "Failed assigning room membership for %s in %s: %s",
                user_id,
                room_id,
                e,
            )
            return self._failure_result(user_id, str(e) or "Room assignment failed")

    async def _invite_user_as_admin(self, user_id: str, room_id: str) -> None:
        inviter_user_id = await self._get_local_inviter_user_id(room_id)
        if inviter_user_id is None:
            raise RuntimeError("No local joined inviter with sufficient power")

        await self._api.update_room_membership(
            sender=inviter_user_id,
            target=user_id,
            room_id=room_id,
            new_membership=MEMBERSHIP_INVITE,
        )

    async def _join_user_as_admin(
        self,
        user_id: str,
        room_id: str,
        current_membership: str | None,
    ) -> None:
        join_rules_event = (
            await self._storage_controllers.state.get_current_state_event(
                room_id, EventTypes.JoinRules, ""
            )
        )
        if (
            current_membership != MEMBERSHIP_INVITE
            and join_rules_event is not None
            and join_rules_event.content.get("join_rule") != JoinRules.PUBLIC
        ):
            await self._invite_user_as_admin(
                user_id=user_id,
                room_id=room_id,
            )

        await self._api.update_room_membership(
            sender=user_id,
            target=user_id,
            room_id=room_id,
            new_membership=MEMBERSHIP_JOIN,
        )

    async def _get_local_inviter_user_id(self, room_id: str) -> str | None:
        room_state = await self._api.get_room_state(
            room_id=room_id,
            event_filter=[
                (EventTypes.Create, ""),
                (EventTypes.PowerLevels, ""),
                (EventTypes.Member, None),
            ],
        )

        create_event = room_state.get((EventTypes.Create, ""))
        power_levels_event = room_state.get((EventTypes.PowerLevels, ""))

        local_joined_members: set[str] = set()
        for state_event in room_state.values():
            if state_event.type != EventTypes.Member:
                continue
            if state_event.content.get("membership") != MEMBERSHIP_JOIN:
                continue
            user_id = state_event.state_key
            if not isinstance(user_id, str) or not self._api.is_mine(user_id):
                continue
            local_joined_members.add(user_id)

        if not local_joined_members:
            return None

        invite_power = 0
        users_default = 0
        users_power_levels: dict[str, Any] = {}
        creator_power_users: set[str] = set()

        if power_levels_event is not None:
            invite_power = self._coerce_int(power_levels_event.content.get("invite"), 0)
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
                if isinstance(creator, str) and creator in local_joined_members:
                    creator_power_users.add(creator)

        candidate_users = sorted(local_joined_members)
        best_candidate: tuple[int, str] | None = None
        for candidate_user_id in candidate_users:
            candidate_power = users_default
            if candidate_user_id in creator_power_users:
                candidate_power = 100
            elif candidate_user_id in users_power_levels:
                candidate_power = self._coerce_int(
                    users_power_levels[candidate_user_id], users_default
                )

            candidate = (candidate_power, candidate_user_id)
            if best_candidate is None or candidate > best_candidate:
                best_candidate = candidate

        if best_candidate is None or best_candidate[0] < invite_power:
            return None

        return best_candidate[1]

    def _coerce_int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _success_result(self, user_id: str, action: str) -> dict[str, Any]:
        return {"user_id": user_id, "success": True, "action": action}

    def _failure_result(self, user_id: str, error: str) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "success": False,
            "action": "failed",
            "error": error,
        }
