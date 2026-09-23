"""Prepare a person for a bot notice before the notice is recorded.

Synapse evaluates push actions when an event is persisted, so the per-user
rule that keeps its own mailer and pushers off ``p.room.notice`` has to exist
before the bot records the notice; installing it at delivery time (which
``deliver_nudge`` still does, as a backstop) is one notice too late for a
first-time recipient. The bot calls this once per person per process.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from synapse.api.errors import (
    AuthError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from twisted.web.resource import Resource

from synapse_pangea_chat.direct_push.is_rate_limited import is_rate_limited
from synapse_pangea_chat.nudge_delivery.push_rule import ensure_bot_notice_push_rule

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.nudge_delivery.prepare")


class PrepareNudge(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: "PangeaChatConfig"):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = api._hs.get_auth()

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        try:
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()
            if not await self._api.is_user_admin(requester_id):
                respond_with_json(
                    request, 403, {"error": "Admin access required"}, send_cors=True
                )
                return
            if is_rate_limited(requester_id, self._config):
                respond_with_json(
                    request, 429, {"error": "Rate limited"}, send_cors=True
                )
                return
            body = self._parse_body(request)
            user_id = body.get("user_id") if body is not None else None
            if not isinstance(user_id, str) or not user_id.startswith("@"):
                respond_with_json(
                    request, 400, {"error": "Missing user_id"}, send_cors=True
                )
                return
            respond_with_json(request, 200, await self.prepare(user_id), send_cors=True)
        # silent-ok: the caller's auth failure, answered 401 (logged at INFO)
        except (AuthError, InvalidClientTokenError, MissingClientTokenError) as e:
            logger.info("Authentication failed: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Error in prepare_nudge endpoint")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    @staticmethod
    def _parse_body(request: SynapseRequest) -> Optional[Dict[str, Any]]:
        try:
            content = request.content.read()
            parsed = json.loads(content) if content else {}
        # silent-ok: malformed body - the caller answers 400
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    async def prepare(self, user_id: str) -> Dict[str, Any]:
        installed = False
        if self._config.nudge_suppress_notice_push_rules:
            installed = await ensure_bot_notice_push_rule(self._api, user_id)
        return {
            "user_id": user_id,
            "push_rule_installed": installed,
            "suppression_enabled": self._config.nudge_suppress_notice_push_rules,
        }
