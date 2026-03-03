"""Custom user-directory search endpoint.

``POST /_synapse/client/pangea/v1/user_directory/search``

Replaces the stock Matrix ``/user_directory/search`` by pushing visibility
filtering (public attribute + shared rooms) into the SQL query itself, so the
LIMIT is applied *after* filtering rather than before.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

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

from synapse_pangea_chat.user_directory_search.is_rate_limited import is_rate_limited
from synapse_pangea_chat.user_directory_search.search_users import search_users_db

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.user_directory_search")


class UserDirectorySearch(Resource):
    """Twisted ``Resource`` that serves the Pangea user-directory search."""

    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()
        self._server_name: str = self._api.server_name

    # ── Twisted entry-point ──────────────────────────────────────────

    def render_POST(self, request: SynapseRequest):
        defer.ensureDeferred(self._async_render_POST(request))
        return server.NOT_DONE_YET

    # ── Async handler ────────────────────────────────────────────────

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        try:
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()

            if is_rate_limited(requester_id, self._config):
                respond_with_json(
                    request, 429, {"error": "Rate limited"}, send_cors=True
                )
                return

            body = self._extract_body(request)
            if body is None:
                respond_with_json(
                    request,
                    400,
                    {"error": "Invalid JSON in request body"},
                    send_cors=True,
                )
                return

            search_term = body.get("search_term")
            if not isinstance(search_term, str) or not search_term.strip():
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing or invalid search_term"},
                    send_cors=True,
                )
                return

            raw_limit = body.get("limit", 10)
            if not isinstance(raw_limit, int):
                raw_limit = 10
            limit = max(min(raw_limit, 50), 1)

            # Resolve config-derived fields once.
            path_str = self._config.limit_user_directory_public_attribute_search_path
            if path_str is None:
                respond_with_json(
                    request,
                    500,
                    {"error": "Server misconfiguration: missing public attribute path"},
                    send_cors=True,
                )
                return

            json_path = path_str.split(".")

            data = await search_users_db(
                self._datastores.main.db_pool,
                requester_id=requester_id,
                search_term=search_term,
                limit=limit,
                server_name=self._server_name,
                public_attribute_json_path=json_path,
                filter_if_missing_public_attribute=(
                    self._config.limit_user_directory_filter_search_if_missing_public_attribute
                ),
                whitelist_requester_id_patterns=(
                    self._config.limit_user_directory_whitelist_requester_id_patterns
                ),
                show_locked_users=self._api._hs.config.userdirectory.show_locked_users,
            )

            respond_with_json(request, 200, data, send_cors=True)

        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ) as e:
            logger.error("Forbidden: %s", e)
            respond_with_json(request, 403, {"error": "Forbidden"}, send_cors=True)
        except Exception as e:
            logger.error("Unexpected error in user_directory_search: %s", e)
            respond_with_json(
                request,
                500,
                {"error": "Internal server error"},
                send_cors=True,
            )

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_body(request: SynapseRequest) -> dict | None:
        content_type = request.getHeader("Content-Type")
        if content_type is None:
            return None
        if not content_type.lower().strip().startswith("application/json"):
            return None
        try:
            if request.content is None:
                return None
            raw = request.content.read()
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                return None
            return parsed
        except Exception:
            return None
