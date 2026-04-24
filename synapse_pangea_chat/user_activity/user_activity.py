from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from synapse.api.errors import (
    AuthError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from twisted.internet import defer
from twisted.web.resource import Resource

from synapse_pangea_chat.user_activity.get_course_activities import (
    get_course_activities,
)
from synapse_pangea_chat.user_activity.get_user_courses import get_user_courses
from synapse_pangea_chat.user_activity.get_users import get_users

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.user_activity")


class _AdminResourceBase(Resource):
    """Shared auth / rate-limit boilerplate for admin-only endpoints."""

    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()


class UserActivity(_AdminResourceBase):
    """GET /_synapse/client/pangea/v1/user_activity

    Paginated list of local users with activity metadata.

    Query params:
      page                   int  (default 1)
      limit                  int  (default 50, max 200)
      user_ids               str  comma-separated Matrix user IDs to include
      course_ids             str  comma-separated room IDs; include only
                                  members of these course rooms
      inactive_days          int  return only users whose
                                  max(last_login_ts, last_message_ts) is older
                                  than this many days (or who have no activity)
      notification_cooldown_ms int exclude users who have a p.room.notice from
                                  the bot in their bot DM room within the last
                                  N ms. Requires the
                                  user_activity_notification_bot_user_id module
                                  config field to be set.

    NOTE: notification_cooldown_ms performs O(N candidates) account-data
    lookups server-side. Use user_ids or course_ids to narrow the candidate
    set when possible.
    """

    def render_GET(self, request: SynapseRequest):
        defer.ensureDeferred(self._async_render_GET(request))
        return server.NOT_DONE_YET

    async def _async_render_GET(self, request: SynapseRequest):
        try:
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()

            is_admin = await self._api.is_user_admin(requester_id)
            if not is_admin:
                respond_with_json(
                    request,
                    403,
                    {"error": "Forbidden: server admin required"},
                    send_cors=True,
                )
                return

            page = _int_param(request, b"page", default=1, minimum=1)
            limit = _int_param(request, b"limit", default=50, minimum=1, maximum=200)
            user_ids = _list_param(request, b"user_ids")
            course_ids = _list_param(request, b"course_ids")
            inactive_days = _optional_int_param(request, b"inactive_days", minimum=1)
            notification_cooldown_ms = _optional_int_param(
                request, b"notification_cooldown_ms", minimum=1
            )

            if notification_cooldown_ms is not None and not (
                self._config.user_activity_notification_bot_user_id
            ):
                respond_with_json(
                    request,
                    400,
                    {
                        "error": "notification_cooldown_ms requires the "
                        "user_activity_notification_bot_user_id module "
                        "config field to be set"
                    },
                    send_cors=True,
                )
                return

            data = await get_users(
                self._datastores.main,
                page=page,
                limit=limit,
                user_ids=user_ids,
                course_ids=course_ids,
                inactive_days=inactive_days,
                notification_cooldown_ms=notification_cooldown_ms,
                bot_user_id=self._config.user_activity_notification_bot_user_id,
                api=self._api,
            )

            respond_with_json(request, 200, data, send_cors=True)

        except (AuthError, InvalidClientTokenError, MissingClientTokenError) as e:
            logger.info("Authentication failed: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Error processing user_activity request")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )


class UserCourses(_AdminResourceBase):
    """GET /_synapse/client/pangea/v1/user_courses

    Paginated list of courses/activity rooms a user is a member of.
    Required: user_id
    Query params: page (int, default 1), limit (int, default 50, max 200).
    """

    def render_GET(self, request: SynapseRequest):
        defer.ensureDeferred(self._async_render_GET(request))
        return server.NOT_DONE_YET

    async def _async_render_GET(self, request: SynapseRequest):
        try:
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()

            is_admin = await self._api.is_user_admin(requester_id)
            if not is_admin:
                respond_with_json(
                    request,
                    403,
                    {"error": "Forbidden: server admin required"},
                    send_cors=True,
                )
                return

            user_id = _str_param(request, b"user_id")
            if not user_id:
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing required parameter: user_id"},
                    send_cors=True,
                )
                return

            page = _int_param(request, b"page", default=1, minimum=1)
            limit = _int_param(request, b"limit", default=50, minimum=1, maximum=200)

            data = await get_user_courses(
                self._datastores.main, user_id, page=page, limit=limit
            )

            respond_with_json(request, 200, data, send_cors=True)

        except (AuthError, InvalidClientTokenError, MissingClientTokenError) as e:
            logger.info("Authentication failed: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Error processing user_courses request")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )


class CourseActivities(_AdminResourceBase):
    """GET /_synapse/client/pangea/v1/course_activities

    Activity rooms belonging to a course.
    Required: course_room_id
    Optional (mutually exclusive):
        include_user_id — only activities where user IS a member
        exclude_user_id — only activities where user is NOT a member
    """

    def render_GET(self, request: SynapseRequest):
        defer.ensureDeferred(self._async_render_GET(request))
        return server.NOT_DONE_YET

    async def _async_render_GET(self, request: SynapseRequest):
        try:
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()

            is_admin = await self._api.is_user_admin(requester_id)
            if not is_admin:
                respond_with_json(
                    request,
                    403,
                    {"error": "Forbidden: server admin required"},
                    send_cors=True,
                )
                return

            course_room_id = _str_param(request, b"course_room_id")
            if not course_room_id:
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing required parameter: course_room_id"},
                    send_cors=True,
                )
                return

            include_user_id = _str_param(request, b"include_user_id")
            exclude_user_id = _str_param(request, b"exclude_user_id")

            if include_user_id and exclude_user_id:
                respond_with_json(
                    request,
                    400,
                    {
                        "error": "include_user_id and exclude_user_id "
                        "are mutually exclusive"
                    },
                    send_cors=True,
                )
                return

            page = _int_param(request, b"page", default=1, minimum=1)
            limit = _int_param(request, b"limit", default=50, minimum=1, maximum=200)

            data = await get_course_activities(
                self._datastores.main,
                course_room_id,
                include_user_id=include_user_id,
                exclude_user_id=exclude_user_id,
                page=page,
                limit=limit,
            )

            if "error" in data:
                respond_with_json(request, 404, data, send_cors=True)
                return

            respond_with_json(request, 200, data, send_cors=True)

        except (AuthError, InvalidClientTokenError, MissingClientTokenError) as e:
            logger.info("Authentication failed: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Error processing course_activities request")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )


# ---------------------------------------------------------------------------
# Query param helpers
# ---------------------------------------------------------------------------


def _int_param(
    request: SynapseRequest,
    name: bytes,
    *,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    raw = request.args.get(name, [None])[0]  # type: ignore[arg-type]
    if raw is None:
        return default
    try:
        val = int(raw)
    except (ValueError, TypeError):
        return default
    val = max(minimum, val)
    if maximum is not None:
        val = min(maximum, val)
    return val


def _str_param(request: SynapseRequest, name: bytes) -> str | None:
    raw = request.args.get(name, [None])[0]  # type: ignore[arg-type]
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


def _list_param(request: SynapseRequest, name: bytes) -> list[str] | None:
    """Parse a comma-separated multi-value query param.

    Returns None if the param is absent, an empty list if the value is blank,
    or a list of non-empty stripped strings.
    """
    raw = _str_param(request, name)
    if raw is None:
        return None
    return [v.strip() for v in raw.split(",") if v.strip()]


def _optional_int_param(
    request: SynapseRequest,
    name: bytes,
    *,
    minimum: int = 1,
) -> int | None:
    """Parse an optional integer query param. Returns None if absent or unparseable."""
    raw = request.args.get(name, [None])[0]  # type: ignore[arg-type]
    if raw is None:
        return None
    try:
        val = int(raw)
    except (ValueError, TypeError):
        return None
    return max(minimum, val)
