"""``/_synapse/client/pangea/v1/unsubscribe`` — the logged-out refusal surface.

GET shows a confirmation page; POST performs the refusal. The one-click
standard (RFC 8058) POSTs to the same URL, and a GET must never perform the
action because mail scanners fetch links automatically. Both write the same
account-data event the in-app preference screen reads and writes, so the two
surfaces cannot disagree.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional
from urllib.parse import parse_qs

from synapse.http import server
from synapse.http.server import respond_with_html
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from synapse.util.async_helpers import Linearizer
from twisted.web.resource import Resource

from synapse_pangea_chat.nudge_delivery.categories import (
    COMMUNICATION_PREFERENCES_ACCOUNT_DATA_TYPE,
    GLOBAL_OFF_CATEGORIES,
    REFUSABLE_CATEGORIES,
    SOURCE_UNSUBSCRIBE_LINK,
    parse_preferences,
    with_refusal,
)
from synapse_pangea_chat.nudge_delivery.common import (
    TEMPLATES_DIR,
    TOKEN_KIND_UNSUBSCRIBE,
    category_label,
    now_ms,
    preference_rows,
    token_secret,
)
from synapse_pangea_chat.nudge_delivery.rate_limit import SlidingWindowRateLimiter
from synapse_pangea_chat.nudge_delivery.tokens import verify_token

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.nudge_delivery.unsubscribe"
)

SCOPE_CATEGORY = "category"
SCOPE_ALL = "all"


def _first_arg(args: Dict[bytes, list], key: bytes) -> Optional[str]:
    values = args.get(key) or []
    if not values:
        return None
    try:
        return values[0].decode("utf-8")
    # silent-ok: an undecodable form value is answered as an invalid link
    except UnicodeDecodeError:
        return None


class NudgeUnsubscribe(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: "PangeaChatConfig"):
        super().__init__()
        self._api = api
        self._config = config
        self._app_name = api._hs.config.email.email_app_name
        # Read-merge-write per person, serialized: two unsubscribes racing
        # (a category refusal and the global off) must both survive. The
        # module runs on the main process, so a process-local lock suffices.
        self._write_lock = Linearizer(
            name="nudge_unsubscribe", clock=api._hs.get_clock()
        )
        [self._confirm_html, self._done_html, self._invalid_html] = api.read_templates(
            [
                "nudge_unsubscribe_confirm.html",
                "nudge_unsubscribe_done.html",
                "nudge_link_invalid.html",
            ],
            custom_template_directory=TEMPLATES_DIR,
        )
        self._rate_limiter = SlidingWindowRateLimiter(
            requests_per_burst=config.nudge_public_requests_per_burst,
            burst_duration_seconds=config.nudge_public_burst_duration_seconds,
        )

    def render_GET(self, request: SynapseRequest):
        run_in_background(self._async_render_GET, request)
        return server.NOT_DONE_YET

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    def _respond_invalid(self, request: SynapseRequest) -> None:
        respond_with_html(
            request, 400, self._invalid_html.render(app_name=self._app_name)
        )

    def _verify(
        self, request: SynapseRequest, token: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        secret = token_secret(self._api, self._config)
        if secret is None:
            logger.error(
                "unsubscribe link cannot be verified: no token secret configured"
            )
            return None
        payload = verify_token(secret, token, now_ms=now_ms(self._api))
        if payload is None or payload.get("k") != TOKEN_KIND_UNSUBSCRIBE:
            return None
        if (
            not isinstance(payload.get("u"), str)
            or payload.get("c") not in REFUSABLE_CATEGORIES
        ):
            return None
        return payload

    async def _async_render_GET(self, request: SynapseRequest) -> None:
        try:
            if self._rate_limiter.is_rate_limited(request.getClientAddress().host):
                respond_with_html(
                    request, 429, "<html><body>Too many requests</body></html>"
                )
                return
            token = _first_arg(dict(request.args or {}), b"t")
            payload = self._verify(request, token)
            if payload is None:
                self._respond_invalid(request)
                return
            raw_preferences = await self._api.account_data_manager.get_global(
                payload["u"], COMMUNICATION_PREFERENCES_ACCOUNT_DATA_TYPE
            )
            respond_with_html(
                request,
                200,
                self._confirm_html.render(
                    app_name=self._app_name,
                    token=token,
                    preference_rows=preference_rows(raw_preferences),
                    all_off=parse_preferences(raw_preferences).all_off,
                    category_label=category_label(payload["c"]),
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Error rendering unsubscribe page")
            respond_with_html(
                request, 500, "<html><body>Something went wrong</body></html>"
            )

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        try:
            if self._rate_limiter.is_rate_limited(request.getClientAddress().host):
                respond_with_html(
                    request, 429, "<html><body>Too many requests</body></html>"
                )
                return
            args: Dict[bytes, list] = dict(request.args or {})
            raw_body = request.content.read()
            if raw_body:
                for key, values in parse_qs(
                    raw_body.decode("utf-8", errors="replace")
                ).items():
                    args.setdefault(
                        key.encode("utf-8"), [v.encode("utf-8") for v in values]
                    )
            token = _first_arg(args, b"t")
            payload = self._verify(request, token)
            if payload is None:
                self._respond_invalid(request)
                return
            scope = _first_arg(args, b"scope") or SCOPE_CATEGORY
            if scope == "preferences":
                try:
                    enabled = {
                        value.decode("utf-8") for value in args.get(b"enabled", [])
                    }
                except UnicodeDecodeError:
                    respond_with_html(request, 400, "Invalid preference selection")
                    return
                if not enabled <= GLOBAL_OFF_CATEGORIES:
                    respond_with_html(request, 400, "Invalid preference selection")
                    return
                all_off = _first_arg(args, b"reminders_enabled") != "yes"
                await self.apply(
                    payload["u"],
                    payload["c"],
                    all_off=all_off,
                    categories=GLOBAL_OFF_CATEGORIES - enabled,
                )
            elif scope in (SCOPE_CATEGORY, SCOPE_ALL):
                all_off = scope == SCOPE_ALL
                await self.apply(payload["u"], payload["c"], all_off=all_off)
            else:
                respond_with_html(request, 400, "Invalid preference scope")
                return
            respond_with_html(
                request,
                200,
                self._done_html.render(
                    app_name=self._app_name,
                    category_label=category_label(payload["c"]),
                    all_off=all_off,
                    preferences_saved=scope == "preferences",
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Error applying unsubscribe")
            respond_with_html(
                request, 500, "<html><body>Something went wrong</body></html>"
            )

    async def apply(
        self,
        user_id: str,
        category: str,
        *,
        all_off: bool,
        categories: Optional[frozenset[str]] = None,
    ) -> Dict[str, Any]:
        manager = self._api.account_data_manager
        async with self._write_lock.queue(user_id):
            current = parse_preferences(
                await manager.get_global(
                    user_id, COMMUNICATION_PREFERENCES_ACCOUNT_DATA_TYPE
                )
            )
            updated = with_refusal(
                current,
                categories=categories
                if categories is not None
                else (() if all_off else (category,)),
                all_off=True if all_off else None,
                now_ms=now_ms(self._api),
                source=SOURCE_UNSUBSCRIBE_LINK,
            )
            await manager.put_global(
                user_id, COMMUNICATION_PREFERENCES_ACCOUNT_DATA_TYPE, updated
            )
        logger.info(
            "communication preference updated via unsubscribe link: category=%s all_off=%s",
            category,
            all_off,
        )
        return updated
