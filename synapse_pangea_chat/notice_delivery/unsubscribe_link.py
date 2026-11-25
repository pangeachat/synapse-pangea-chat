"""``POST /_synapse/client/pangea/v1/notice_unsubscribe_link`` — an account's unsubscribe link, minted for mail the
module does not send.

The engagement runner sends some account mail itself (a plain note, a custom From, a copy to a colleague) rather
than through ``deliver_notice``. That mail still needs the account's own refusal link and one-click headers, and the
only key that signs them is this module's. So an admin asks for the link: the same unsubscribe token
``deliver_notice`` signs, for one account and one refusable category, with the same lifetime, answered at the same
``/unsubscribe`` page. Minting records nothing and sends nothing; it only signs.
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

from synapse_pangea_chat.notice_delivery.categories import REFUSABLE_CATEGORIES
from synapse_pangea_chat.notice_delivery.common import (
    TOKEN_KIND_UNSUBSCRIBE,
    now_ms,
    public_baseurl,
    token_secret,
    unsubscribe_url,
)
from synapse_pangea_chat.notice_delivery.rate_limit import AdminRateLimiter
from synapse_pangea_chat.notice_delivery.tokens import MILLISECONDS_PER_DAY, sign_token

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.notice_delivery.unsubscribe_link"
)


class MintUnsubscribeLink(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: "PangeaChatConfig"):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = api._hs.get_auth()
        self._limiter = AdminRateLimiter(
            config.notice_admin_requests_per_minute, config.notice_admin_burst
        )

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
            if self._limiter.is_rate_limited(requester_id):
                respond_with_json(
                    request, 429, {"error": "Rate limited"}, send_cors=True
                )
                return
            body = self._parse_body(request)
            user_id = body.get("user_id") if body is not None else None
            category = body.get("category") if body is not None else None
            if (
                not isinstance(user_id, str)
                or not user_id.startswith("@")
                or ":" not in user_id
            ):
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing or invalid user_id"},
                    send_cors=True,
                )
                return
            # The unsubscribe page accepts only a refusable category; a link it would answer as invalid is never minted.
            if not isinstance(category, str) or category not in REFUSABLE_CATEGORIES:
                respond_with_json(
                    request,
                    400,
                    {"error": "category must be a refusable notice category"},
                    send_cors=True,
                )
                return
            # check_user_exists answers with the account's id as stored, which may differ in case from the one asked
            # for; the token names the stored id, because that is the id the unsubscribe page reads and writes.
            canonical = (
                await self._api.check_user_exists(user_id)
                if self._api.is_mine(user_id)
                else None
            )
            if not canonical:
                respond_with_json(
                    request, 404, {"error": "Unknown user"}, send_cors=True
                )
                return
            code, payload = self.mint(canonical, category)
            respond_with_json(request, code, payload, send_cors=True)
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
            logger.exception("Error in notice_unsubscribe_link endpoint")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    def mint(self, user_id: str, category: str) -> tuple[int, Dict[str, Any]]:
        """The signed link, as ``deliver_notice`` signs it for its own email: same token kind, claims and lifetime."""
        secret = token_secret(self._api, self._config)
        if secret is None:
            logger.error(
                "unsubscribe link not minted: no token secret (notice_token_secret or macaroon secret)"
            )
            return 500, {"error": "no token secret is configured"}
        base = public_baseurl(self._api)
        if base is None:
            logger.error(
                "unsubscribe link not minted: public_baseurl is not configured"
            )
            return 500, {"error": "public_baseurl is not configured"}
        now = now_ms(self._api)
        ttl_ms = self._config.notice_token_ttl_days * MILLISECONDS_PER_DAY
        token = sign_token(
            secret,
            {"k": TOKEN_KIND_UNSUBSCRIBE, "u": user_id, "c": category},
            now_ms=now,
            ttl_ms=ttl_ms,
        )
        return 200, {"url": unsubscribe_url(base, token), "expires_at": now + ttl_ms}

    @staticmethod
    def _parse_body(request: SynapseRequest) -> Optional[Dict[str, Any]]:
        try:
            content = request.content.read()
            parsed = json.loads(content) if content else {}
        # silent-ok: malformed body - the caller answers 400
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
