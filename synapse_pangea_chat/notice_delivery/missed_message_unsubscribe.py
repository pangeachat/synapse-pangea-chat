"""``/_synapse/client/unsubscribe`` — Synapse's own missed-message unsubscribe
link, served by this module instead.

Synapse puts this link, signed with its delete-pusher macaroon, in the body of
every missed-message email and in its ``List-Unsubscribe`` header. Synapse's
own page removes the email pusher as soon as the link is fetched, which a mail
scanner does on delivery, and records nothing a later sign-in can see. Here a
GET only shows the confirmation page; a POST (the page's button, or a mail
client's one-click unsubscribe) records the ``missed_message`` refusal, which
also removes the email pushers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Optional

from pymacaroons.exceptions import MacaroonException
from synapse.http import server
from synapse.http.server import respond_with_html
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from twisted.web.resource import Resource

from synapse_pangea_chat.notice_delivery.categories import (
    COMMUNICATION_PREFERENCES_ACCOUNT_DATA_TYPE,
    MISSED_MESSAGE_CATEGORY,
)
from synapse_pangea_chat.notice_delivery.common import TEMPLATES_DIR, category_label
from synapse_pangea_chat.notice_delivery.rate_limit import SlidingWindowRateLimiter
from synapse_pangea_chat.notice_delivery.refusal_store import RefusalStore
from synapse_pangea_chat.notice_delivery.unsubscribe import (
    CONFIRM_TEMPLATE,
    DONE_TEMPLATE,
    INVALID_TEMPLATE,
    SCOPE_PREFERENCES,
    PreferenceChoice,
    confirm_page_vars,
    first_arg,
    parse_preference_form,
    request_args,
)

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.notice_delivery.missed_message_unsubscribe"
)

MISSED_MESSAGE_UNSUBSCRIBE_PATH = "/_synapse/client/unsubscribe"
ONE_CLICK_FIELD = b"List-Unsubscribe"
ONE_CLICK_VALUE = "One-Click"


class MissedMessageUnsubscribe(Resource):
    isLeaf = True

    def __init__(
        self,
        api: ModuleApi,
        config: "PangeaChatConfig",
        refusal_store: RefusalStore,
    ):
        super().__init__()
        self._api = api
        self._refusal_store = refusal_store
        self._app_name = api._hs.config.email.email_app_name
        self._macaroon_generator = api._hs.get_macaroon_generator()
        [self._confirm_html, self._done_html, self._invalid_html] = api.read_templates(
            [CONFIRM_TEMPLATE, DONE_TEMPLATE, INVALID_TEMPLATE],
            custom_template_directory=TEMPLATES_DIR,
        )
        self._rate_limiter = SlidingWindowRateLimiter(
            requests_per_burst=config.notice_public_requests_per_burst,
            burst_duration_seconds=config.notice_public_burst_duration_seconds,
        )

    def render_GET(self, request: SynapseRequest):
        run_in_background(self._async_render_GET, request)
        return server.NOT_DONE_YET

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    def _verified_user(self, args: Dict[bytes, list]) -> Optional[str]:
        token = first_arg(args, b"access_token")
        app_id = first_arg(args, b"app_id")
        pushkey = first_arg(args, b"pushkey")
        if not (token and app_id and pushkey):
            return None
        try:
            return self._macaroon_generator.verify_delete_pusher_token(
                token, app_id, pushkey
            )
        # silent-ok: a forged, truncated or foreign link is answered as invalid
        except (MacaroonException, ValueError):
            return None

    def _rate_limited(self, request: SynapseRequest) -> bool:
        if not self._rate_limiter.is_rate_limited(request.getClientAddress().host):
            return False
        respond_with_html(request, 429, "<html><body>Too many requests</body></html>")
        return True

    def _respond_invalid(self, request: SynapseRequest) -> None:
        respond_with_html(
            request, 400, self._invalid_html.render(app_name=self._app_name)
        )

    async def _async_render_GET(self, request: SynapseRequest) -> None:
        try:
            if self._rate_limited(request):
                return
            user_id = self._verified_user(dict(request.args or {}))
            if user_id is None:
                self._respond_invalid(request)
                return
            raw_preferences = await self._api.account_data_manager.get_global(
                user_id, COMMUNICATION_PREFERENCES_ACCOUNT_DATA_TYPE
            )
            # No token field: the form posts back to this URL, whose query
            # string carries Synapse's signed link.
            respond_with_html(
                request,
                200,
                self._confirm_html.render(
                    app_name=self._app_name,
                    token=None,
                    category_label=category_label(MISSED_MESSAGE_CATEGORY),
                    **confirm_page_vars(raw_preferences),
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Error rendering missed-message unsubscribe page")
            respond_with_html(
                request, 500, "<html><body>Something went wrong</body></html>"
            )

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        try:
            if self._rate_limited(request):
                return
            args = request_args(request)
            user_id = self._verified_user(args)
            if user_id is None:
                self._respond_invalid(request)
                return
            choice = self._choice(args)
            if choice is None:
                respond_with_html(request, 400, "Invalid preference selection")
                return
            updated = await self._refusal_store.add_refusals(
                user_id,
                categories=choice.categories,
                all_off=True if choice.all_off else None,
            )
            respond_with_html(
                request,
                200,
                self._done_html.render(
                    app_name=self._app_name,
                    category_label=category_label(MISSED_MESSAGE_CATEGORY),
                    all_off=choice.all_off,
                    preferences_saved=first_arg(args, b"scope") == SCOPE_PREFERENCES,
                    missed_message_refused=MISSED_MESSAGE_CATEGORY
                    in updated["refused"],
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Error applying missed-message unsubscribe")
            respond_with_html(
                request, 500, "<html><body>Something went wrong</body></html>"
            )

    @staticmethod
    def _choice(args: Dict[bytes, list]) -> Optional[PreferenceChoice]:
        if first_arg(args, ONE_CLICK_FIELD) == ONE_CLICK_VALUE:
            return PreferenceChoice(
                categories=frozenset({MISSED_MESSAGE_CATEGORY}), all_off=False
            )
        if first_arg(args, b"scope") == SCOPE_PREFERENCES:
            return parse_preference_form(args)
        return None
