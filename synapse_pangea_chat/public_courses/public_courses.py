from __future__ import annotations

import logging
from typing import Optional

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

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.public_courses.get_public_courses import get_public_courses
from synapse_pangea_chat.public_courses.is_rate_limited import (
    RateLimitError,
    is_rate_limited,
)

logger = logging.getLogger("synapse.module.synapse_pangea_chat.public_courses")


class PublicCourses(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()

    def render_GET(self, request: SynapseRequest):
        defer.ensureDeferred(self._async_render_GET(request))
        return server.NOT_DONE_YET

    async def _async_render_GET(self, request: SynapseRequest):
        try:
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()

            is_rate_limited(requester_id, self._config)

            # Parse query parameters from query string
            limit_param = request.args.get(b"limit", [b"10"])
            if isinstance(limit_param, list):
                limit_value = limit_param[0] if limit_param else b"10"
            else:
                limit_value = limit_param

            if isinstance(limit_value, bytes):
                limit_str = limit_value.decode("utf-8", errors="ignore")
            else:
                limit_str = str(limit_value)

            try:
                limit = int(limit_str)
            except (ValueError, TypeError):
                limit = 10

            since_param = request.args.get(b"since")
            since_value: Optional[str] = None
            if since_param:
                candidate = (
                    since_param[0] if isinstance(since_param, list) else since_param
                )
                if isinstance(candidate, bytes):
                    since_value = candidate.decode("utf-8", errors="ignore")
                else:
                    since_value = str(candidate)

            public_courses = await get_public_courses(
                self._datastores.main, self._config, limit, since_value
            )

            respond_with_json(
                request,
                200,
                public_courses,
                send_cors=True,
            )
        except RateLimitError:
            respond_with_json(
                request,
                429,
                {"error": "Rate limited"},
                send_cors=True,
            )
        except (AuthError, InvalidClientTokenError, MissingClientTokenError) as e:
            logger.info("Authentication failed for room preview request: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
        except Exception as e:
            logger.error("Error processing request: %s", e)
            respond_with_json(
                request,
                500,
                {"error": "Internal server error"},
                send_cors=True,
            )
