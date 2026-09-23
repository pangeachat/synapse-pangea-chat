"""``GET /_synapse/client/pangea/v1/n?t=…`` — the nudge email's click-through.

Records the open as the same first-party ``p.room.notice.opened`` event the
client writes on a push tap — so an email click feeds cooldown and backoff
exactly like a tap does — then redirects to the app. Recording never blocks the
redirect: a person who clicked gets where they were going even if the record
fails, and the failure is logged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, Optional

from synapse.http import server
from synapse.http.server import respond_with_redirect
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from twisted.web.resource import Resource

from synapse_pangea_chat.nudge_delivery.common import (
    TOKEN_KIND_CLICK,
    app_url,
    now_ms,
    token_secret,
)
from synapse_pangea_chat.nudge_delivery.rate_limit import SlidingWindowRateLimiter
from synapse_pangea_chat.nudge_delivery.tokens import verify_token

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.nudge_delivery.click")

BOT_NOTICE_OPENED_EVENT_TYPE = "p.room.notice.opened"


def _first_arg(request: SynapseRequest, key: bytes) -> Optional[str]:
    args: Dict[bytes, list] = dict(request.args or {})
    values = args.get(key) or []
    if not values:
        return None
    try:
        return values[0].decode("utf-8")
    # silent-ok: an undecodable query value is treated as no token
    except UnicodeDecodeError:
        return None


class NudgeClick(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: "PangeaChatConfig"):
        super().__init__()
        self._api = api
        self._config = config
        self._rate_limiter = SlidingWindowRateLimiter(
            requests_per_burst=config.nudge_public_requests_per_burst,
            burst_duration_seconds=config.nudge_public_burst_duration_seconds,
        )

    def render_GET(self, request: SynapseRequest):
        run_in_background(self._async_render_GET, request)
        return server.NOT_DONE_YET

    async def _async_render_GET(self, request: SynapseRequest) -> None:
        destination = app_url(
            self._config.app_base_url, activity_id=None, session_room_id=None
        )
        try:
            if self._rate_limiter.is_rate_limited(request.getClientAddress().host):
                respond_with_redirect(request, destination.encode("utf-8"))
                return
            payload = self._verify(_first_arg(request, b"t"))
            if payload is None:
                # An expired or forged link still lands the person in the app;
                # only the open goes unrecorded.
                logger.warning(
                    "nudge click link invalid or expired; redirecting without record"
                )
                respond_with_redirect(request, destination.encode("utf-8"))
                return
            destination = app_url(
                self._config.app_base_url,
                activity_id=payload.get("a"),
                session_room_id=payload.get("s"),
            )
            await self.record_open(payload)
            respond_with_redirect(request, destination.encode("utf-8"))
        except Exception:  # noqa: BLE001
            logger.exception("Error handling nudge click")
            respond_with_redirect(request, destination.encode("utf-8"))

    def _verify(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        secret = token_secret(self._api, self._config)
        if secret is None:
            logger.error(
                "nudge click link cannot be verified: no token secret configured"
            )
            return None
        payload = verify_token(secret, token, now_ms=now_ms(self._api))
        if payload is None or payload.get("k") != TOKEN_KIND_CLICK:
            return None
        if not isinstance(payload.get("u"), str):
            return None
        return payload

    async def record_open(self, payload: Dict[str, Any]) -> bool:
        """Persist the opened event; True when it was written."""
        room_id = payload.get("r")
        notice_event_id = payload.get("e")
        if not isinstance(room_id, str) or not isinstance(notice_event_id, str):
            logger.warning("nudge click carried no notice reference; open not recorded")
            return False
        try:
            await self._api.create_and_send_event_into_room(
                {
                    "type": BOT_NOTICE_OPENED_EVENT_TYPE,
                    "room_id": room_id,
                    "sender": payload["u"],
                    "content": {
                        "notification_event_id": notice_event_id,
                        "check_in_type": payload.get("v") or "default",
                        "opened_at_ts": now_ms(self._api),
                    },
                }
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("nudge open not recorded: %s", type(e).__name__)
            return False
        return True
