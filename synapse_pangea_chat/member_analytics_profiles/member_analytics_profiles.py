"""Every joined member's public analytics profile for one room, in one request.

``GET /_synapse/client/pangea/v1/member_analytics_profiles?room_id=...`` with
the caller's own Matrix token. Design: member-analytics-profiles.instructions.md.

Any member currently joined to the room may call it. It answers for every
local member whose current membership is ``join``, each with the
``pangea.analytics_profile`` profile field as stored, or an empty object when
they have none. A remote member is absent: their profile is not stored here.

Every refusal (unknown room, caller not joined) is the same 403 with the same
body, so the endpoint is no oracle for which rooms exist.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Iterable, Mapping, Tuple, cast

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

from synapse_pangea_chat.member_analytics_profiles.is_rate_limited import (
    is_rate_limited,
)

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.member_analytics_profiles"
)

MEMBERSHIP_JOIN = "join"

# The custom profile field each client publishes its analytics mirror under.
ANALYTICS_PROFILE_FIELD = "pangea.analytics_profile"

# One body for every refusal, so a 403 never says which check failed.
_FORBIDDEN = {"error": "Forbidden: room member required", "errcode": "M_FORBIDDEN"}


class MemberAnalyticsProfiles(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()

    def render_GET(self, request: SynapseRequest):
        run_in_background(self._async_render_GET, request)
        return server.NOT_DONE_YET

    async def _async_render_GET(self, request: SynapseRequest) -> None:
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

            room_ids = request.args.get(b"room_id") or []
            try:
                room_id = room_ids[0].decode("utf-8") if len(room_ids) == 1 else ""
            # silent-ok: bytes that are not UTF-8 are no room id - answered 400
            except UnicodeDecodeError:
                room_id = ""
            if not _is_valid_room_id(room_id):
                respond_with_json(
                    request,
                    400,
                    {"error": "'room_id' must be one valid Matrix room ID"},
                    send_cors=True,
                )
                return

            store = self._datastores.main
            (
                caller_membership,
                _,
            ) = await store.get_local_current_membership_for_user_in_room(
                caller_id, room_id
            )
            if caller_membership != MEMBERSHIP_JOIN:
                respond_with_json(request, 403, _FORBIDDEN, send_cors=True)
                return

            # Cast: Synapse's @cached methods are opaque to mypy without
            # Synapse's own mypy plugin.
            member_ids = await cast(Any, store).get_local_users_in_room(room_id)
            rows = await store.db_pool.simple_select_many_batch(
                table="profiles",
                column="full_user_id",
                iterable=member_ids,
                retcols=("full_user_id", "fields"),
                desc="pangea_member_analytics_profiles",
                # One query for the whole room; the default reads it 100
                # members at a time.
                batch_size=max(1, len(member_ids)),
            )
            profiles = analytics_profiles_by_member(member_ids, rows)
            logger.info(
                "member_analytics_profiles: room=%s caller=%s members=%d with_profile=%d",
                room_id,
                caller_id,
                len(profiles),
                sum(1 for profile in profiles.values() if profile),
            )
            respond_with_json(request, 200, {"profiles": profiles}, send_cors=True)

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
            logger.exception("Error reading member analytics profiles")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )


def analytics_profiles_by_member(
    member_ids: Iterable[str], rows: Iterable[Tuple[Any, ...]]
) -> Dict[str, Dict[str, Any]]:
    """Each member's analytics profile, shaped like the profile API's answer
    (``{field: value}``) so the client parses both the same way. A member with
    no profile row or no analytics field gets ``{}``."""
    fields_by_user: Dict[str, Any] = {row[0]: row[1] for row in rows}
    profiles: Dict[str, Dict[str, Any]] = {}
    for user_id in member_ids:
        fields = fields_by_user.get(user_id)
        # SQLite has no JSON type and hands back the column as text.
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            # silent-ok: left as text, which is logged below as unexpected
            except ValueError:
                pass
        if fields is None:
            profiles[user_id] = {}
        elif isinstance(fields, Mapping):
            profiles[user_id] = (
                {ANALYTICS_PROFILE_FIELD: fields[ANALYTICS_PROFILE_FIELD]}
                if ANALYTICS_PROFILE_FIELD in fields
                else {}
            )
        else:
            # Synapse writes only an object or NULL to this column.
            logger.warning(
                "member_analytics_profiles: unexpected profile fields type %s "
                "for %s; answered as no profile",
                type(fields).__name__,
                user_id,
            )
            profiles[user_id] = {}
    return profiles


def _is_valid_room_id(room_id: str) -> bool:
    try:
        RoomID.from_string(room_id)
    # silent-ok: validation only - the caller answers 400 for an invalid id
    except Exception:
        return False
    return True
