from __future__ import annotations

import json
import logging
import secrets
import time
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, Optional

from synapse.api.errors import (
    AuthError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.module_api import ModuleApi
from twisted.internet import defer, reactor
from twisted.web.client import Agent, FileBodyProducer, readBody
from twisted.web.http_headers import Headers
from twisted.web.resource import Resource

from synapse_pangea_chat.direct_push.is_rate_limited import is_rate_limited
from synapse_pangea_chat.direct_push.types import (
    DeviceStatus,
    SendPushRequest,
    SendPushResponse,
)

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger("synapse.module.synapse_pangea_chat.direct_push")


class DirectPush(Resource):
    isLeaf = True

    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()

    def render_POST(self, request: SynapseRequest):
        defer.ensureDeferred(self._async_render_POST(request))
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest):
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

            body = await self._extract_body_json(request)
            if body is None:
                respond_with_json(
                    request, 400, {"error": "Invalid JSON"}, send_cors=True
                )
                return

            target_user_id = body.get("user_id")
            if not target_user_id:
                respond_with_json(
                    request, 400, {"error": "Missing user_id"}, send_cors=True
                )
                return

            device_id = body.get("device_id")
            room_id = body.get("room_id")
            if not room_id:
                respond_with_json(
                    request, 400, {"error": "Missing room_id"}, send_cors=True
                )
                return

            body_text = body.get("body")
            if not body_text:
                respond_with_json(
                    request, 400, {"error": "Missing body"}, send_cors=True
                )
                return

            response = await self._send_push(target_user_id, device_id, body)
            respond_with_json(request, 200, response, send_cors=True)

        except (AuthError, InvalidClientTokenError, MissingClientTokenError) as e:
            logger.info("Authentication failed: %s", e)
            respond_with_json(
                request,
                401,
                {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"},
                send_cors=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Error in direct_push endpoint")
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    async def _extract_body_json(
        self, request: SynapseRequest
    ) -> Optional[SendPushRequest]:
        try:
            content = request.content.read()
            if not content:
                return {}
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return None

        if not isinstance(parsed, dict):
            return None

        return parsed

    async def _send_push(
        self,
        target_user_id: str,
        device_id: Optional[str],
        req_body: SendPushRequest,
    ) -> SendPushResponse:
        pushers = await self._get_pushers(target_user_id, device_id)

        response: SendPushResponse = {
            "user_id": target_user_id,
            "attempted": len(pushers),
            "sent": 0,
            "failed": 0,
            "devices": {},
            "errors": [],
        }

        if not pushers:
            return response

        event_id = (
            req_body.get("event_id")
            or f"push-{int(time.time())}-{secrets.token_hex(6)}"
        )

        for pusher in pushers:
            device_id_key = pusher.get("device_id", "unknown")
            status: DeviceStatus = {
                "sent": False,
                "app_id": pusher.get("app_id", ""),
                "pushkey": pusher.get("pushkey", ""),
            }

            try:
                payload = self._build_payload(event_id, req_body, pusher)
                success = await self._post_to_sygnal(payload)

                if success:
                    status["sent"] = True
                    response["sent"] += 1
                else:
                    response["failed"] += 1
                    status["error"] = "Sygnal returned error"
                    response["errors"].append(f"{device_id_key}: {status['error']}")

            except Exception as e:  # noqa: BLE001
                response["failed"] += 1
                status["error"] = str(e)
                response["errors"].append(f"{device_id_key}: {status['error']}")
                logger.exception("Error posting to Sygnal for %s", device_id_key)

            response["devices"][device_id_key] = status

        return response

    async def _get_pushers(
        self, user_id: str, device_id: Optional[str]
    ) -> list[Dict[str, Any]]:
        pushers_iter = await self._datastores.main.get_pushers_by_user_id(user_id)
        pushers = []
        for pusher in pushers_iter:
            if not pusher.enabled:
                continue
            if device_id and pusher.device_id != device_id:
                continue
            pushers.append(
                {
                    "device_id": pusher.device_id,
                    "app_id": pusher.app_id,
                    "pushkey": pusher.pushkey,
                    "pushkey_ts": pusher.pushkey_ts,
                    "data": pusher.data,
                }
            )
        return pushers

    def _build_payload(
        self,
        event_id: str,
        req_body: SendPushRequest,
        pusher: Dict[str, Any],
    ) -> Dict[str, Any]:
        return {
            "notification": {
                "event_id": event_id,
                "room_id": req_body.get("room_id"),
                "type": req_body.get("type", "m.room.message"),
                "sender": "@bot:pangea.chat",
                "sender_display_name": "Pangea Bot",
                "room_name": "",
                "room_avatar_url": None,
                "prio": req_body.get("prio", "high"),
                "content": {
                    "msgtype": "m.text",
                    "body": req_body.get("body"),
                    **(req_body.get("content") or {}),
                },
                "counts": {"unread": 1, "missed_calls": 0},
                "devices": [
                    {
                        "app_id": pusher["app_id"],
                        "pushkey": pusher["pushkey"],
                        "pushkey_ts": pusher["pushkey_ts"],
                        "data": pusher["data"],
                        "tweaks": {},
                    }
                ],
            }
        }

    async def _post_to_sygnal(self, payload: Dict[str, Any]) -> bool:
        try:
            agent = Agent(reactor)
            url = b"https://sygnal.pangea.chat/_matrix/push/v1/notify"
            body_bytes = json.dumps(payload).encode("utf-8")

            producer = FileBodyProducer(BytesIO(body_bytes))

            response = await agent.request(
                b"POST",
                url,
                Headers({b"Content-Type": [b"application/json"]}),
                producer,
            )

            await readBody(response)

            if response.code >= 400:
                logger.warning("Sygnal returned %s", response.code)
                return False

            return True
        except Exception:  # noqa: BLE001
            logger.exception("Error posting to Sygnal")
            return False
