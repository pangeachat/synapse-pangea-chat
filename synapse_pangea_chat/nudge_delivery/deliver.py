"""``POST /_synapse/client/pangea/v1/deliver_nudge`` — deliver one bot nudge on
one channel, chosen by availability.

The bot has already recorded the nudge as a ``p.room.notice`` in the person's
DM; this endpoint carries it the rest of the way: refused → nothing; in the
app right now → nothing more (the notice is the delivery); a working push
device → push; otherwise email, if the person has an address and email is
enabled. It never sends on two channels for one nudge.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Dict, Optional, cast

from synapse.api.constants import PresenceState
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

from synapse_pangea_chat.direct_push.direct_push import DirectPush
from synapse_pangea_chat.direct_push.is_rate_limited import is_rate_limited
from synapse_pangea_chat.direct_push.types import SendPushRequest
from synapse_pangea_chat.nudge_delivery.categories import (
    COMMUNICATION_PREFERENCES_ACCOUNT_DATA_TYPE,
    DELIVERABLE_CATEGORIES,
    GLOBAL_OFF_CATEGORIES,
    is_refused,
    parse_preferences,
)
from synapse_pangea_chat.nudge_delivery.common import (
    TEMPLATES_DIR,
    TOKEN_KIND_CLICK,
    TOKEN_KIND_UNSUBSCRIBE,
    category_label,
    click_url,
    now_ms,
    public_baseurl,
    token_secret,
    unsubscribe_url,
)
from synapse_pangea_chat.nudge_delivery.push_rule import ensure_bot_notice_push_rule
from synapse_pangea_chat.nudge_delivery.tokens import MILLISECONDS_PER_DAY, sign_token

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.nudge_delivery.deliver")

BOT_NOTICE_EVENT_TYPE = "p.room.notice"
EMAIL_MEDIUM = "email"
MAX_SUBJECT_LENGTH = 120

CHANNEL_REFUSED = "refused"
CHANNEL_IN_APP = "in_app"
CHANNEL_PUSH = "push"
CHANNEL_EMAIL = "email"
CHANNEL_NONE = "none"


def _optional_str(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class DeliverNudge(Resource):
    isLeaf = True

    def __init__(
        self, api: ModuleApi, config: "PangeaChatConfig", direct_push: DirectPush
    ):
        super().__init__()
        self._api = api
        self._config = config
        self._hs = api._hs
        self._auth = self._hs.get_auth()
        self._store = self._hs.get_datastores().main
        self._direct_push = direct_push
        self._send_email_handler = self._hs.get_send_email_handler()
        self._app_name = self._hs.config.email.email_app_name
        [self._email_html, self._email_text] = api.read_templates(
            ["nudge_email.html", "nudge_email.txt"],
            custom_template_directory=TEMPLATES_DIR,
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
            if is_rate_limited(requester_id, self._config):
                respond_with_json(
                    request, 429, {"error": "Rate limited"}, send_cors=True
                )
                return

            body = self._parse_body(request)
            if body is None:
                respond_with_json(
                    request, 400, {"error": "Invalid JSON"}, send_cors=True
                )
                return
            error = self._validate(body)
            if error is not None:
                respond_with_json(request, 400, {"error": error}, send_cors=True)
                return

            response = await self.deliver(body)
            respond_with_json(request, 200, response, send_cors=True)
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
            logger.exception("Error in deliver_nudge endpoint")
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

    @staticmethod
    def _validate(body: Dict[str, Any]) -> Optional[str]:
        if not _optional_str(body.get("user_id")):
            return "Missing user_id"
        category = _optional_str(body.get("category"))
        if category not in DELIVERABLE_CATEGORIES:
            return "category must be a deliverable catalog category"
        if not _optional_str(body.get("body")):
            return "Missing body"
        content = body.get("content")
        if content is not None and not isinstance(content, dict):
            return "content must be an object"
        return None

    async def deliver(self, body: Dict[str, Any]) -> Dict[str, Any]:
        user_id = body["user_id"].strip()
        category = body["category"].strip()
        result: Dict[str, Any] = {
            "user_id": user_id,
            "category": category,
            "channel": CHANNEL_NONE,
            "reason": None,
            "push": None,
            "email": None,
            "push_rule_installed": False,
        }

        raw_preferences = await self._api.account_data_manager.get_global(
            user_id, COMMUNICATION_PREFERENCES_ACCOUNT_DATA_TYPE
        )
        preferences = parse_preferences(raw_preferences)
        if is_refused(preferences, category):
            result["channel"] = CHANNEL_REFUSED
            result["reason"] = (
                "all_off"
                if preferences.all_off and category in GLOBAL_OFF_CATEGORIES
                else "category_refused"
            )
            return result

        if self._config.nudge_suppress_notice_push_rules:
            result["push_rule_installed"] = await ensure_bot_notice_push_rule(
                self._api, user_id
            )

        if await self._is_in_app(user_id):
            result["channel"] = CHANNEL_IN_APP
            result["reason"] = "currently_active"
            return result

        push_request: Dict[str, Any] = {
            "user_id": user_id,
            "body": body["body"],
            "room_id": _optional_str(body.get("notice_room_id")),
            "event_id": _optional_str(body.get("notice_event_id")),
            "type": BOT_NOTICE_EVENT_TYPE,
            "content": body.get("content") or {},
            "prio": "high",
        }
        push = await self._direct_push._send_push(
            user_id, None, cast(SendPushRequest, push_request), pusher_kinds=("http",)
        )
        result["push"] = push
        if push["sent"] > 0:
            result["channel"] = CHANNEL_PUSH
            return result

        email = await self._send_email(body, user_id=user_id, category=category)
        result["email"] = email
        if email["sent"]:
            result["channel"] = CHANNEL_EMAIL
        else:
            result["reason"] = (
                email["reason"]
                if push["attempted"] == 0
                else f"push_failed_then_{email['reason']}"
            )
        return result

    async def _is_in_app(self, user_id: str) -> bool:
        server_config = self._hs.config.server
        if getattr(server_config, "presence_enabled", True) is False:
            return False
        if getattr(server_config, "track_presence", True) is False:
            return False
        try:
            state = await self._hs.get_presence_handler().current_state_for_user(
                user_id
            )
        except Exception as e:  # noqa: BLE001
            # A presence read that fails must not block a nudge the person is
            # otherwise due; the worst case is one push to someone in the app.
            logger.warning(
                "presence lookup failed for nudge delivery: %s", type(e).__name__
            )
            return False
        # `currently_active` can survive an offline transition, so the state
        # itself must be online too; otherwise an offline person would read
        # as in-app and get nothing.
        return getattr(state, "state", None) == PresenceState.ONLINE and bool(
            getattr(state, "currently_active", False)
        )

    async def _first_email_address(self, user_id: str) -> Optional[str]:
        threepids = await self._store.user_get_threepids(user_id)
        for threepid in threepids:
            medium = getattr(threepid, "medium", None)
            address = getattr(threepid, "address", None)
            if medium is None and isinstance(threepid, dict):
                medium = threepid.get("medium")
                address = threepid.get("address")
            if medium == EMAIL_MEDIUM and isinstance(address, str) and address.strip():
                return address.strip()
        return None

    async def _send_email(
        self, body: Dict[str, Any], *, user_id: str, category: str
    ) -> Dict[str, Any]:
        if not self._config.nudge_email_enabled:
            return {"sent": False, "reason": "email_disabled"}
        secret = token_secret(self._api, self._config)
        if secret is None:
            logger.error(
                "nudge email skipped: no token secret (nudge_token_secret or macaroon secret)"
            )
            return {"sent": False, "reason": "no_token_secret"}
        base = public_baseurl(self._api)
        if base is None:
            logger.error("nudge email skipped: public_baseurl is not configured")
            return {"sent": False, "reason": "no_public_baseurl"}
        address = await self._first_email_address(user_id)
        if address is None:
            return {"sent": False, "reason": "no_email_address"}

        now = now_ms(self._api)
        ttl_ms = self._config.nudge_token_ttl_days * MILLISECONDS_PER_DAY
        variant = _optional_str(body.get("variant"))
        click_token = sign_token(
            secret,
            {
                "k": TOKEN_KIND_CLICK,
                "u": user_id,
                "e": _optional_str(body.get("notice_event_id")),
                "r": _optional_str(body.get("notice_room_id")),
                "v": variant,
                "a": _optional_str(body.get("activity_id")),
                "s": _optional_str(body.get("session_room_id")),
            },
            now_ms=now,
            ttl_ms=ttl_ms,
        )
        unsubscribe_token = sign_token(
            secret,
            {"k": TOKEN_KIND_UNSUBSCRIBE, "u": user_id, "c": category},
            now_ms=now,
            ttl_ms=ttl_ms,
        )
        cta_url = click_url(base, click_token)
        unsub_url = unsubscribe_url(base, unsubscribe_token)

        subject = (
            _optional_str(body.get("email_subject"))
            or _optional_str(body.get("title"))
            or body["body"].strip()
        )
        # A Subject header cannot carry line breaks; a multi-line body used
        # as the fallback subject would make the mailer reject the message.
        subject = " ".join(subject.split())[:MAX_SUBJECT_LENGTH]
        template_vars = {
            "app_name": self._app_name,
            "title": _optional_str(body.get("title")),
            "body": body["body"].strip(),
            "cta_label": _optional_str(body.get("cta_label")) or "Open Pangea Chat",
            "cta_url": cta_url,
            "unsubscribe_url": unsub_url,
            "category_label": category_label(category),
            "postal_address": self._config.nudge_email_postal_address or "",
        }
        headers = {
            "List-Unsubscribe": f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
        try:
            await self._send_email_handler.send_email(
                email_address=address,
                subject=subject,
                app_name=self._app_name,
                html=self._email_html.render(**template_vars),
                text=self._email_text.render(**template_vars),
                additional_headers=headers,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "nudge email send failed for category=%s: %s",
                category,
                type(e).__name__,
            )
            return {"sent": False, "reason": "send_failed"}
        return {"sent": True, "reason": None}
