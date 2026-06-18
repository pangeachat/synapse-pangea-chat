from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence, cast

from synapse.api.constants import (
    AccountDataTypes,
    EventTypes,
    JoinRules,
    RoomCreationPreset,
)
from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
    SynapseError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from synapse.types import UserID, create_requester
from twisted.internet import defer
from twisted.web.resource import Resource

from synapse_pangea_chat.room_code.extract_body_json import extract_body_json

if TYPE_CHECKING:
    from synapse.types import Requester

    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.direct_message.ensure_direct_message"
)

ACTION_VALID_EXISTING_NOOP = "valid_existing_noop"
ACTION_REPAIRED_ACCOUNT_DATA = "repaired_account_data"
ACTION_REPAIRED_MEMBERSHIP_OR_POWER = "repaired_membership_or_power"
ACTION_CREATED_ROOM = "created_room"


@dataclass(frozen=True)
class ExistingDirectRoom:
    room_id: str
    first_user_direct: dict[str, Sequence[str]]
    second_user_direct: dict[str, Sequence[str]]


class EnsureDirectMessage(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()
        self._storage_controllers = self._api._hs.get_storage_controllers()
        self._room_member_handler = self._api._hs.get_room_member_handler()

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
            user_ids = self._extract_user_ids(body)
            if user_ids is None:
                respond_with_json(
                    request,
                    400,
                    {
                        "error": (
                            "'user_ids' must be an array of exactly 2 distinct local user IDs"
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
                            "'user_ids' must be an array of exactly 2 distinct local user IDs"
                        )
                    },
                    send_cors=True,
                )
                return

            result = await self._ensure_direct_message_room(
                requester=requester,
                user_ids=canonical_user_ids,
            )
            logger.info(
                "ensure_direct_message action=%s created=%s reused=%s "
                "m_direct_updated_count=%d power_levels_updated=%s",
                result["action"],
                result["created"],
                result["reused"],
                len(result["m_direct_updated_for"]),
                result["power_levels_updated"],
            )

            respond_with_json(
                request,
                200,
                result,
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
            logger.exception("Error ensuring direct message room")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    async def _ensure_direct_message_room(
        self,
        requester: Requester,
        user_ids: Sequence[str],
    ) -> dict[str, Any]:
        existing_room = await self._find_existing_direct_room(user_ids)
        created = existing_room is None
        existing_power_content: dict[str, Any] | None = None
        current_power_users: dict[str, Any] | None = None

        if existing_room is None:
            room_id = await self._create_direct_room(
                requester=requester,
                user_ids=user_ids,
            )
        else:
            room_id = existing_room.room_id
            existing_power_content, current_power_users = await self._get_power_levels(
                room_id
            )
            if self._existing_room_is_valid_noop(
                user_ids=user_ids,
                room_id=room_id,
                existing_room=existing_room,
                current_power_users=current_power_users,
            ):
                return self._success_payload(
                    room_id=room_id,
                    created=False,
                    m_direct_updated_for=[],
                    power_levels_updated=False,
                    action=ACTION_VALID_EXISTING_NOOP,
                )

        power_levels_updated = await self._ensure_admin_power_levels(
            user_ids=user_ids,
            room_id=room_id,
            existing_content=existing_power_content,
            current_users=current_power_users,
        )

        first_direct_map = existing_room.first_user_direct if existing_room else None
        second_direct_map = existing_room.second_user_direct if existing_room else None
        m_direct_updated_for: list[str] = []
        if await self._ensure_direct_entry(
            user_ids[0], user_ids[1], room_id, direct_map=first_direct_map
        ):
            m_direct_updated_for.append(user_ids[0])
        if await self._ensure_direct_entry(
            user_ids[1], user_ids[0], room_id, direct_map=second_direct_map
        ):
            m_direct_updated_for.append(user_ids[1])

        return self._success_payload(
            room_id=room_id,
            created=created,
            m_direct_updated_for=m_direct_updated_for,
            power_levels_updated=power_levels_updated,
            action=self._classify_action(
                created=created,
                m_direct_updated_for=m_direct_updated_for,
                power_levels_updated=power_levels_updated,
            ),
        )

    def _success_payload(
        self,
        *,
        room_id: str,
        created: bool,
        m_direct_updated_for: Sequence[str],
        power_levels_updated: bool,
        action: str,
    ) -> dict[str, Any]:
        return {
            "room_id": room_id,
            "created": created,
            "reused": not created,
            "m_direct_updated_for": list(m_direct_updated_for),
            "power_levels_updated": power_levels_updated,
            "action": action,
        }

    def _classify_action(
        self,
        *,
        created: bool,
        m_direct_updated_for: Sequence[str],
        power_levels_updated: bool,
    ) -> str:
        if created:
            return ACTION_CREATED_ROOM
        if power_levels_updated:
            return ACTION_REPAIRED_MEMBERSHIP_OR_POWER
        if m_direct_updated_for:
            return ACTION_REPAIRED_ACCOUNT_DATA
        return ACTION_VALID_EXISTING_NOOP

    def _existing_room_is_valid_noop(
        self,
        *,
        user_ids: Sequence[str],
        room_id: str,
        existing_room: ExistingDirectRoom,
        current_power_users: dict[str, Any],
    ) -> bool:
        return (
            self._room_in_direct_map(
                existing_room.first_user_direct,
                user_ids[1],
                room_id,
            )
            and self._room_in_direct_map(
                existing_room.second_user_direct,
                user_ids[0],
                room_id,
            )
            and self._users_have_admin_power(current_power_users, user_ids)
        )

    def _extract_user_ids(self, body: Any) -> list[str] | None:
        if not isinstance(body, dict):
            return None

        user_ids = body.get("user_ids")
        if not isinstance(user_ids, list) or len(user_ids) != 2:
            return None

        if not all(isinstance(user_id, str) for user_id in user_ids):
            return None

        if user_ids[0] == user_ids[1]:
            return None

        return user_ids

    async def _validate_local_users(self, user_ids: list[str]) -> list[str] | None:
        canonical_user_ids: list[str] = []
        for user_id in user_ids:
            canonical_user_id = await self._api.check_user_exists(user_id)
            if canonical_user_id is None or not self._api.is_mine(canonical_user_id):
                return None
            canonical_user_ids.append(canonical_user_id)

        if canonical_user_ids[0] == canonical_user_ids[1]:
            return None

        return canonical_user_ids

    async def _find_existing_direct_room(
        self, user_ids: Sequence[str]
    ) -> ExistingDirectRoom | None:
        store = cast(Any, self._datastores.main)
        first_user_rooms = await store.get_rooms_for_user(user_ids[0])
        second_user_rooms = await store.get_rooms_for_user(user_ids[1])
        shared_room_ids = sorted(first_user_rooms.intersection(second_user_rooms))
        if not shared_room_ids:
            return None

        first_user_direct = await self._get_direct_map(user_ids[0])
        second_user_direct = await self._get_direct_map(user_ids[1])
        fallback_room_id = None

        for room_id in shared_room_ids:
            members = set(await store.get_users_in_room(room_id))
            if members != set(user_ids):
                continue

            if self._room_in_direct_map(first_user_direct, user_ids[1], room_id):
                return ExistingDirectRoom(
                    room_id=room_id,
                    first_user_direct=first_user_direct,
                    second_user_direct=second_user_direct,
                )
            if self._room_in_direct_map(second_user_direct, user_ids[0], room_id):
                return ExistingDirectRoom(
                    room_id=room_id,
                    first_user_direct=first_user_direct,
                    second_user_direct=second_user_direct,
                )

            if await self._looks_like_direct_room(room_id):
                fallback_room_id = room_id

        if fallback_room_id is None:
            return None
        return ExistingDirectRoom(
            room_id=fallback_room_id,
            first_user_direct=first_user_direct,
            second_user_direct=second_user_direct,
        )

    async def _get_direct_map(self, user_id: str) -> dict[str, Sequence[str]]:
        direct_map = await self._api.account_data_manager.get_global(
            user_id, AccountDataTypes.DIRECT
        )
        if isinstance(direct_map, dict):
            return dict(direct_map)
        return {}

    def _room_in_direct_map(
        self,
        direct_map: dict[str, Sequence[str]],
        counterpart_user_id: str,
        room_id: str,
    ) -> bool:
        room_ids = direct_map.get(counterpart_user_id, ())
        if not isinstance(room_ids, (list, tuple)):
            return False
        return room_id in room_ids

    async def _looks_like_direct_room(self, room_id: str) -> bool:
        state = await self._api.get_room_state(
            room_id,
            event_filter=[
                (EventTypes.Create, ""),
                (EventTypes.Name, ""),
                (EventTypes.Topic, ""),
                (EventTypes.RoomAvatar, ""),
                (EventTypes.CanonicalAlias, ""),
                (EventTypes.JoinRules, ""),
            ],
        )

        create_event = state.get((EventTypes.Create, ""))
        if create_event is None:
            return False

        if create_event.content.get("type"):
            return False

        if (EventTypes.Name, "") in state:
            return False
        if (EventTypes.Topic, "") in state:
            return False
        if (EventTypes.RoomAvatar, "") in state:
            return False
        if (EventTypes.CanonicalAlias, "") in state:
            return False

        join_rules_event = state.get((EventTypes.JoinRules, ""))
        if join_rules_event is not None:
            join_rule = join_rules_event.content.get("join_rule")
            if join_rule in {JoinRules.PUBLIC, JoinRules.KNOCK}:
                return False

        return True

    async def _create_direct_room(
        self, requester: Requester, user_ids: Sequence[str]
    ) -> str:
        room_id, _ = await self._api.create_room(
            user_ids[0],
            {
                "preset": RoomCreationPreset.PRIVATE_CHAT,
                "visibility": "private",
                "invite": [user_ids[1]],
                "is_direct": True,
                "power_level_content_override": {
                    "users": {
                        user_ids[0]: 100,
                        user_ids[1]: 100,
                    }
                },
            },
            ratelimit=False,
        )
        await self._join_user_as_admin(
            requester=requester,
            user_id=user_ids[1],
            room_id=room_id,
        )
        return room_id

    async def _join_user_as_admin(
        self, requester: Requester, user_id: str, room_id: str
    ) -> None:
        (
            current_membership,
            _,
        ) = await self._datastores.main.get_local_current_membership_for_user_in_room(
            user_id, room_id
        )
        if current_membership == "join":
            return

        fake_requester = create_requester(
            UserID.from_string(user_id),
            authenticated_entity=requester.authenticated_entity,
        )

        join_rules_event = (
            await self._storage_controllers.state.get_current_state_event(
                room_id, EventTypes.JoinRules, ""
            )
        )
        if (
            current_membership != "invite"
            and join_rules_event is not None
            and join_rules_event.content.get("join_rule") != JoinRules.PUBLIC
        ):
            await self._room_member_handler.update_membership(
                requester=requester,
                target=fake_requester.user,
                room_id=room_id,
                action="invite",
                ratelimit=False,
            )

        await self._room_member_handler.update_membership(
            requester=fake_requester,
            target=fake_requester.user,
            room_id=room_id,
            action="join",
            ratelimit=False,
        )

    async def _get_power_levels(
        self, room_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        state = await self._api.get_room_state(
            room_id, event_filter=[(EventTypes.PowerLevels, "")]
        )
        pl_event = state.get((EventTypes.PowerLevels, ""))
        existing_content: dict[str, Any] = {}
        current_users: dict[str, Any] = {}
        if pl_event is not None:
            existing_content = dict(pl_event.content)
            raw_users = existing_content.get("users", {})
            if isinstance(raw_users, dict):
                current_users = dict(raw_users)
        return existing_content, current_users

    def _users_have_admin_power(
        self, current_users: dict[str, Any], user_ids: Sequence[str]
    ) -> bool:
        return all(
            self._coerce_power(current_users.get(uid, 0)) >= 100 for uid in user_ids
        )

    def _coerce_power(self, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    async def _ensure_admin_power_levels(
        self,
        user_ids: Sequence[str],
        room_id: str,
        *,
        existing_content: dict[str, Any] | None = None,
        current_users: dict[str, Any] | None = None,
    ) -> bool:
        """Ensure both users have power level 100 in the room.

        Returns True if a power levels event was sent, False if no change was needed.
        """
        if existing_content is None or current_users is None:
            existing_content, current_users = await self._get_power_levels(room_id)

        if self._users_have_admin_power(current_users, user_ids):
            return False

        new_users = dict(current_users)
        for uid in user_ids:
            new_users[uid] = 100
        new_content = dict(existing_content)
        new_content["users"] = new_users

        for sender in user_ids:
            try:
                await self._api.create_and_send_event_into_room(
                    {
                        "type": EventTypes.PowerLevels,
                        "state_key": "",
                        "room_id": room_id,
                        "sender": sender,
                        "content": new_content,
                    }
                )
                return True
            except SynapseError as e:
                logger.warning(
                    "Could not send power levels event as %s in room %s: %s",
                    sender,
                    room_id,
                    e,
                )

        logger.warning(
            "Failed to update power levels in room %s — neither user had sufficient PL",
            room_id,
        )
        return False

    async def _ensure_direct_entry(
        self,
        user_id: str,
        counterpart_user_id: str,
        room_id: str,
        *,
        direct_map: dict[str, Sequence[str]] | None = None,
    ) -> bool:
        if direct_map is None:
            direct_map = await self._get_direct_map(user_id)
        else:
            direct_map = dict(direct_map)
        existing_room_ids = direct_map.get(counterpart_user_id, ())
        if not isinstance(existing_room_ids, (list, tuple)):
            existing_room_ids = ()

        if room_id in existing_room_ids:
            return False

        direct_map[counterpart_user_id] = tuple(existing_room_ids) + (room_id,)
        await self._api.account_data_manager.put_global(
            user_id, AccountDataTypes.DIRECT, direct_map
        )
        return True
