"""Reserve a CMS Notification_Log decision before touching a delivery transport."""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any, Dict, Optional
from urllib.parse import quote, urlencode

from synapse.http.client import read_body_with_max_size
from synapse.logging.context import make_deferred_yieldable
from synapse.module_api import ModuleApi
from twisted.web.http_headers import Headers

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.notice_delivery.request import NoticeRequest


class DeliveryLogError(Exception):
    """No send may follow an uncertain reservation."""


class DeliveryConflict(DeliveryLogError):
    """A run/person is already reserved or belongs to another notice."""


class DeliveryLog:
    def __init__(self, api: ModuleApi, config: PangeaChatConfig):
        self._http = api.http_client
        self._reactor = api._hs.get_reactor()
        self._url = config.cms_base_url.rstrip("/") + "/api/notification-log"
        self._headers = Headers(
            {
                "Authorization": [
                    f"service-users API-Key {config.cms_service_api_key}"
                ],
                "Content-Type": ["application/json"],
            }
        )

    async def _request(
        self, method: str, suffix: str = "", body: Optional[Dict[str, Any]] = None
    ):
        response = await self._http.request(
            method,
            self._url + suffix,
            data=json.dumps(body).encode() if body is not None else None,
            headers=self._headers,
        )
        buffer = BytesIO()
        await make_deferred_yieldable(
            read_body_with_max_size(response, buffer, 256_000).addTimeout(
                20, self._reactor
            )
        )
        raw = buffer.getvalue()
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise DeliveryLogError(
                f"CMS returned non-JSON ({response.code})"
            ) from error
        return response.code, payload

    @staticmethod
    def _decision(req: NoticeRequest) -> Dict[str, Any]:
        return {
            "outcome": "send",
            "category": req.category,
            "variant": req.variant,
            "copy_key": req.log.copy_key,
            "channel": "none",
            "notice_event_id": req.notice_event_id,
            "notice_room_id": req.notice_room_id,
            "delivery": {"status": "pending"},
        }

    async def reserve(self, req: NoticeRequest) -> tuple[str, Optional[Dict[str, Any]]]:
        row = {
            "run": req.log.run,
            "subject": {
                "kind": "account",
                "subject_id": req.user_id,
                "matrix_user_id": req.user_id,
            },
            "state": req.log.state,
            "decision": self._decision(req),
        }
        status, payload = await self._request("POST", body=row)
        if 200 <= status < 300:
            doc = payload.get("doc", payload)
            if not doc.get("id"):
                raise DeliveryLogError("CMS reservation returned no id")
            return str(doc["id"]), None
        if status not in (400, 409):
            raise DeliveryLogError(f"CMS reservation failed ({status})")
        # Read after any conflict-shaped response. A failed create must never be
        # retried here: a timeout could have committed the reservation already.
        key = json.dumps(
            [req.log.run["run_id"], req.user_id],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        code, found = await self._request(
            "GET", "?" + urlencode({"where[decision_key][equals]": key, "limit": 1})
        )
        docs = found.get("docs", [])
        if code != 200 or len(docs) != 1:
            raise DeliveryLogError(f"CMS reservation rejected ({status})")
        doc = docs[0]
        decision = doc["decision"]
        for field in (
            "category",
            "variant",
            "notice_event_id",
            "notice_room_id",
            "copy_key",
        ):
            if decision.get(field) != row["decision"][field]:
                raise DeliveryConflict(
                    "Run/person already belongs to a different notice"
                )
        delivery = decision.get("delivery") or {}
        if delivery.get("status") != "complete":
            raise DeliveryConflict(
                "Delivery is pending or uncertain; reconcile before any resend"
            )
        return str(doc["id"]), delivery["response"]

    async def finish(
        self, record_id: str, req: NoticeRequest, result: Dict[str, Any]
    ) -> None:
        # Device tokens, bodies and addresses must never enter Notification_Log.
        safe = {
            key: result[key]
            for key in (
                "user_id",
                "category",
                "channel",
                "reason",
                "email",
                "push_rule_installed",
            )
        }
        push = result.get("push")
        safe["push"] = (
            {key: push[key] for key in ("attempted", "sent", "failed")}
            if push
            else None
        )
        decision = self._decision(req)
        sent = result["channel"] in {"email", "push", "in_app"}
        decision.update(
            outcome="send" if sent else "skip",
            channel=result["channel"] if sent else "none",
            no_send_reason=None if sent else result["reason"],
            delivery={"status": "complete", "response": safe},
        )
        status, _ = await self._request(
            "PATCH", "/" + quote(record_id, safe=""), {"decision": decision}
        )
        if not 200 <= status < 300:
            raise DeliveryLogError(f"CMS delivery update failed ({status})")
