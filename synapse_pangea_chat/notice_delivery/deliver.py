"""Admin HTTP adapter and shared delivery service for persisted notices."""

from __future__ import annotations

import html
import json
import logging
from dataclasses import replace
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
from synapse_pangea_chat.direct_push.types import SendPushRequest
from synapse_pangea_chat.notice_delivery.categories import (
    COMMUNICATION_PREFERENCES_ACCOUNT_DATA_TYPE,
    DELIVERABLE_CATEGORIES,
    GLOBAL_OFF_CATEGORIES,
    is_refused,
    parse_preferences,
)
from synapse_pangea_chat.notice_delivery.common import (
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
from synapse_pangea_chat.notice_delivery.delivery_log import (
    DeliveryConflict,
    DeliveryLog,
    DeliveryLogError,
)
from synapse_pangea_chat.notice_delivery.eligibility import NoticeEligibility
from synapse_pangea_chat.notice_delivery.push_rule import ensure_bot_notice_push_rule
from synapse_pangea_chat.notice_delivery.rate_limit import AdminRateLimiter
from synapse_pangea_chat.notice_delivery.request import (
    EMAIL_ONLY,
    NoticeRequest,
    is_structured,
)
from synapse_pangea_chat.notice_delivery.schedule import NoticeSchedule
from synapse_pangea_chat.notice_delivery.tokens import MILLISECONDS_PER_DAY, sign_token

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.notice_delivery.deliver")

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


class DeliverNotice(Resource):
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
        self._limiter = AdminRateLimiter(
            config.notice_admin_requests_per_minute, config.notice_admin_burst
        )
        self._delivery_log = DeliveryLog(api, config)
        self.schedule = NoticeSchedule(api, self._deliver_scheduled)
        self._send_email_handler = self._hs.get_send_email_handler()
        self._app_name = self._hs.config.email.email_app_name
        [self._email_html, self._email_text] = api.read_templates(
            ["notice_email.html", "notice_email.txt"],
            custom_template_directory=TEMPLATES_DIR,
        )

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    def render_GET(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    def render_DELETE(self, request: SynapseRequest):
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

            if request.method in (b"GET", b"DELETE"):
                args = cast(Dict[bytes, list[bytes]], request.args)
                values = args.get(b"schedule_id", [])
                if len(values) != 1:
                    raise ValueError("schedule_id is required")
                operation = (
                    self.schedule.cancel
                    if request.method == b"DELETE"
                    else self.schedule.get
                )
                response = await operation(values[0].decode("utf-8"))
                respond_with_json(request, 200, response, send_cors=True)
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

            response = await self.deliver(body, requested_by=requester_id)
            respond_with_json(
                request,
                202 if "schedule_id" in response else 200,
                response,
                send_cors=True,
            )
        except ValueError as error:
            respond_with_json(request, 400, {"error": str(error)}, send_cors=True)
        except DeliveryConflict as error:
            respond_with_json(request, 409, {"error": str(error)}, send_cors=True)
        except DeliveryLogError:
            logger.exception("Notice delivery log unavailable; delivery not started")
            respond_with_json(
                request,
                503,
                {"error": "Delivery log unavailable; reconcile run before retry"},
                send_cors=True,
            )
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
            logger.exception("Error in deliver_notice endpoint")
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
        if is_structured(body):
            try:
                NoticeRequest.parse(body)
            except ValueError as error:
                return str(error)
            return None
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

    async def deliver(
        self, body: Dict[str, Any], *, requested_by: Optional[str] = None
    ) -> Dict[str, Any]:
        """Shared entry point for HTTP operators and trusted internal flows."""
        error = self._validate(body)
        if error:
            raise ValueError(error)
        if "scheduled_at" in body:
            body = {**body, "_requested_by": requested_by or body["sender_id"]}
            req = NoticeRequest.parse(body)
            await self._validate_scheduled_target(body, req)
            return await self.schedule.enqueue(body)
        return await self._deliver_now(body)

    async def _deliver_scheduled(self, body: Dict[str, Any]) -> Dict[str, Any]:
        # Revalidate persisted content and eligibility; never run through enqueue.
        NoticeRequest.parse(body)
        return await self._deliver_now(body)

    async def _validate_scheduled_target(
        self, body: Dict[str, Any], req: NoticeRequest
    ) -> None:
        sender = body["sender_id"]
        for admin in {sender, body.get("_requested_by", sender)}:
            if not self._api.is_mine(admin) or not await self._api.is_user_admin(admin):
                raise ValueError(
                    "Sender and requesting operator must be local server admins"
                )
        for user in (sender, req.user_id):
            (
                membership,
                _,
            ) = await self._store.get_local_current_membership_for_user_in_room(
                user, req.notice_room_id
            )
            if membership != "join":
                raise ValueError(
                    "Sender and recipient must be joined local room members"
                )
        if not self._config.notice_suppress_notice_push_rules:
            raise ValueError("Scheduled delivery requires native notice suppression")

    async def _deliver_now(self, body: Dict[str, Any]) -> Dict[str, Any]:
        body = dict(body)
        req = NoticeRequest.parse(body) if is_structured(body) else None
        record_id = None
        if req is not None:
            if req.notice_event_id is not None:
                await self._validate_notice(req)
            if req.caller_owns_record:
                # The caller wrote the Notification_Log row and will write the receipt onto
                # it; this module touches the record not at all (engagement-system doc).
                record_id = req.notification_log_id
            else:
                record_id, previous = await self._delivery_log.reserve(req)
                if previous is not None:
                    return {
                        **previous,
                        "notification_log_id": record_id,
                        "log_status": "complete",
                        "duplicate": True,
                    }
        result: Dict[str, Any]
        if req is not None and req.notice_event_id is None:
            reason: Optional[str]
            try:
                await self._validate_scheduled_target(body, req)
            # silent-ok: expected eligibility changes become a recorded no-send
            # result in Notification_Log below, not an unreported failure.
            except ValueError:
                reason = "scheduled_target_ineligible"
            else:
                assert record_id is not None  # Structured delivery reserved above.
                reason = await NoticeEligibility(self._api, self._delivery_log).check(
                    body, req, record_id
                )
            if reason:
                result = {
                    "user_id": req.user_id,
                    "category": req.category,
                    "channel": CHANNEL_NONE,
                    "reason": reason,
                    "push": None,
                    "email": None,
                    "push_rule_installed": False,
                }
            else:
                result = await self._deliver_once(body, req)
            req = replace(req, notice_event_id=body.get("notice_event_id"))
            result["notice_event_id"] = req.notice_event_id
            result["notice_room_id"] = req.notice_room_id
        else:
            result = await self._deliver_once(body, req)
        if req is not None and req.caller_owns_record:
            result.update(
                notification_log_id=record_id,
                log_status="caller",
                duplicate=False,
            )
        elif req is not None and record_id is not None:
            result.update(
                notification_log_id=record_id, log_status="complete", duplicate=False
            )
            if "scheduled_at" in body and (result.get("reason") or "").endswith(
                "send_failed"
            ):
                result["log_status"] = "pending_reconciliation"
            try:
                await self._delivery_log.finish(record_id, req, result)
            except Exception:
                # Delivery already happened. Report it, preserve the reservation,
                # and never turn a logging outage into a second transport send.
                logger.exception(
                    "Notice delivered but Notification_Log finalization failed: %s",
                    record_id,
                )
                result["log_status"] = "pending_reconciliation"
        return result

    async def _validate_notice(self, req: NoticeRequest) -> None:
        assert req.notice_event_id is not None
        event = await self._store.get_event(req.notice_event_id, allow_none=True)
        if (
            event is None
            or event.type != BOT_NOTICE_EVENT_TYPE
            or event.room_id != req.notice_room_id
        ):
            raise ValueError(
                "notice_event_id must reference p.room.notice in notice_room_id"
            )
        if not await self._api.is_user_admin(event.sender):
            raise ValueError("Referenced notice must have an admin sender")
        membership, _ = await self._store.get_local_current_membership_for_user_in_room(
            req.user_id, req.notice_room_id
        )
        if membership != "join":
            raise ValueError(
                "Recipient must be a joined local member of the notice room"
            )

    async def _deliver_once(
        self, body: Dict[str, Any], req: Optional[NoticeRequest]
    ) -> Dict[str, Any]:
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

        if self._config.notice_suppress_notice_push_rules:
            result["push_rule_installed"] = await ensure_bot_notice_push_rule(
                self._api, user_id
            )

        if req is not None and req.notice_event_id is None:
            event = await self._api.create_and_send_event_into_room(
                {
                    "type": BOT_NOTICE_EVENT_TYPE,
                    "sender": body["sender_id"],
                    "room_id": req.notice_room_id,
                    "content": {
                        **body["notice_content"],
                        **(
                            {"pangea.schedule_id": body["_schedule_id"]}
                            if "_schedule_id" in body
                            else {}
                        ),
                    },
                }
            )
            body["notice_event_id"] = event.event_id

        method = (
            req.method
            if req
            else ("email-only" if category in EMAIL_ONLY else "use-available")
        )
        if body.get("variant") == "allow_notifications":
            method = "in-app-only"
        if method == "in-app-only" or (
            method == "use-available" and await self._is_in_app(user_id)
        ):
            result["channel"] = CHANNEL_IN_APP
            result["reason"] = (
                "in_app_only" if method == "in-app-only" else "currently_active"
            )
            return result

        if method == "email-only":
            email = await self._send_email(
                body, user_id=user_id, category=category, req=req
            )
            result["email"] = email
            result["channel"] = CHANNEL_EMAIL if email["sent"] else CHANNEL_NONE
            result["reason"] = email["reason"]
            return result

        push_request: Dict[str, Any] = {
            "user_id": user_id,
            "body": req.push.body if req and req.push else body["body"],
            "title": req.push.title if req and req.push else body.get("title"),
            "room_id": _optional_str(body.get("notice_room_id")),
            "event_id": _optional_str(body.get("notice_event_id")),
            "type": BOT_NOTICE_EVENT_TYPE,
            "content": req.push.content
            if req and req.push
            else body.get("content") or {},
            "prio": "high",
        }
        push = await self._direct_push._send_push(
            user_id, None, cast(SendPushRequest, push_request), pusher_kinds=("http",)
        )
        result["push"] = push
        if push["sent"] > 0:
            result["channel"] = CHANNEL_PUSH
            return result

        if method == "push-only":
            result["reason"] = (
                "no_push_device" if push["attempted"] == 0 else "push_failed"
            )
            return result

        email = await self._send_email(
            body, user_id=user_id, category=category, req=req
        )
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
            # A presence read that fails must not block a notice the person is
            # otherwise due; the worst case is one push to someone in the app.
            logger.warning(
                "presence lookup failed for notice delivery: %s", type(e).__name__
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
        self,
        body: Dict[str, Any],
        *,
        user_id: str,
        category: str,
        req: Optional[NoticeRequest] = None,
    ) -> Dict[str, Any]:
        if not self._config.notice_email_enabled:
            return {"sent": False, "reason": "email_disabled"}
        secret = token_secret(self._api, self._config)
        if secret is None:
            logger.error(
                "notice email skipped: no token secret (notice_token_secret or macaroon secret)"
            )
            return {"sent": False, "reason": "no_token_secret"}
        base = public_baseurl(self._api)
        if base is None:
            logger.error("notice email skipped: public_baseurl is not configured")
            return {"sent": False, "reason": "no_public_baseurl"}
        address = await self._first_email_address(user_id)
        if address is None:
            return {"sent": False, "reason": "no_email_address"}

        now = now_ms(self._api)
        ttl_ms = self._config.notice_token_ttl_days * MILLISECONDS_PER_DAY
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

        email_content = req.email if req else None
        subject = (
            email_content.subject
            if email_content
            else (
                _optional_str(body.get("email_subject"))
                or _optional_str(body.get("title"))
                or body["body"].strip()
            )
        )
        # A Subject header cannot carry line breaks; a multi-line body used
        # as the fallback subject would make the mailer reject the message.
        subject = " ".join(subject.split())[:MAX_SUBJECT_LENGTH]
        template_vars = {
            "app_name": self._app_name,
            "title": _optional_str(body.get("title")),
            "body": body.get("body", "").strip(),
            "cta_label": _optional_str(body.get("cta_label")) or "Open Pangea Chat",
            "cta_url": cta_url,
            "unsubscribe_url": unsub_url,
            "category_label": category_label(category),
            "postal_address": self._config.notice_email_postal_address or "",
            "receiving_reason": body.get("receiving_reason", ""),
        }
        if email_content:
            # Literal replacement only: never execute caller HTML as Jinja.
            replacements = {
                "{{cta_url}}": cta_url,
                "{{unsubscribe_url}}": unsub_url,
                "{{receiving_reason}}": email_content.receiving_reason,
                "{{postal_address}}": self._config.notice_email_postal_address or "",
            }
            rendered_html, rendered_text = email_content.html, email_content.text
            for slot, value in replacements.items():
                rendered_html = rendered_html.replace(
                    slot, html.escape(value, quote=True)
                )
                rendered_text = rendered_text.replace(slot, value)
        else:
            rendered_html = self._email_html.render(**template_vars)
            rendered_text = self._email_text.render(**template_vars)
        headers = {
            "List-Unsubscribe": f"<{unsub_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }
        try:
            await self._send_email_handler.send_email(
                email_address=address,
                subject=subject,
                app_name=self._app_name,
                html=rendered_html,
                text=rendered_text,
                additional_headers=headers,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "notice email send failed for category=%s: %s",
                category,
                type(e).__name__,
            )
            return {"sent": False, "reason": "send_failed"}
        return {"sent": True, "reason": None}
