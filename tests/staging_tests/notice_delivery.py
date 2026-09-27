"""Opt-in live smoke; sends one email and three notices to an approved recipient.

Run with SYNAPSE_AUTH_TOKEN, NOTICE_TEST_USER_ID, and NOTICE_CMS_API_KEY.
The caller must have authorization to send to this recipient. Never use a real
learner batch as a smoke test. Tokens are supplied by the operator, not stored.
"""

import copy
import json
import os
import unittest
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

from synapse_pangea_chat.notice_delivery.common import TEMPLATES_DIR
from tests.test_notice_content import request_body


class NoticeDeliveryStaging(unittest.TestCase):
    def test_delivery_and_cms_log(self):
        base = "https://matrix.staging.pangea.chat"
        uid = os.environ["NOTICE_TEST_USER_ID"]
        self.assertTrue(uid.endswith(":staging.pangea.chat"))
        run = "notice-staging-" + uuid.uuid4().hex
        session = requests.Session()
        self.addCleanup(session.close)
        session.headers["Authorization"] = "Bearer " + os.environ["SYNAPSE_AUTH_TOKEN"]
        cms_headers = {
            "Authorization": "service-users API-Key " + os.environ["NOTICE_CMS_API_KEY"]
        }

        def call(method, path, body=None):
            response = session.request(method, base + path, json=body, timeout=60)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        sender = call("GET", "/_matrix/client/v3/account/whoami")["user_id"]
        call("POST", "/_synapse/client/pangea/v1/prepare_notice", {"user_id": uid})
        room = call(
            "POST",
            "/_synapse/client/pangea/v1/ensure_direct_message",
            {"user_ids": [sender, uid]},
        )["room_id"]
        env = Environment(
            loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape()
        )
        for index, method in enumerate(("email-only", "in-app-only", "push-only")):
            with self.subTest(method=method):
                text = "Your requested staging notice test. No activity is required."
                event = call(
                    "PUT",
                    f"/_matrix/client/v3/rooms/{quote(room, safe='')}/send/p.room.notice/{run}-{index}",
                    {
                        "notice_type": "check_in",
                        "check_in_type": "do_activity",
                        "body": text,
                        "body_sent": text,
                        "original_sent": {
                            "lang": "en",
                            "txt": text,
                            "snt": False,
                            "wrttn": False,
                        },
                        "tokens_sent": {"tkns": []},
                        "pangea.analytics.variant": "do_activity",
                    },
                )["event_id"]
                body = request_body(method)
                body.update(user_id=uid, notice_room_id=room, notice_event_id=event)
                body.pop("activity_id")
                body["log"]["run"].update(
                    run_id=f"{run}-{index}",
                    decided_at=datetime.now(timezone.utc).isoformat(),
                )
                body["push"] = {
                    "title": "Notice delivery staging test",
                    "body": text,
                    "content": {},
                }
                body["email"]["subject"] = "[Staging 228] Shared notice delivery " + run
                body["email"][
                    "receiving_reason"
                ] = "You're receiving this because you requested this staging delivery test."
                body["email"]["html"] = env.get_template("notice_email.html").render(
                    app_name="Pangea Chat",
                    title="A notice for you",
                    body=text,
                    cta_label="Open staging Pangea Chat",
                    cta_url="{{cta_url}}",
                    unsubscribe_url="{{unsubscribe_url}}",
                    receiving_reason="{{receiving_reason}}",
                    postal_address="{{postal_address}}",
                )
                result = call("POST", "/_synapse/client/pangea/v1/deliver_notice", body)
                self.assertEqual(result["log_status"], "complete")
                allowed = {
                    "email-only": {"email"},
                    "in-app-only": {"in_app"},
                    "push-only": {"push", "none"},
                }
                self.assertIn(result["channel"], allowed[method])
                row = requests.get(
                    "https://api.staging.pangea.chat/cms/api/notification-log/"
                    + result["notification_log_id"],
                    headers=cms_headers,
                    timeout=30,
                )
                self.assertEqual(row.status_code, 200)
                self.assertEqual(row.json()["decision"]["channel"], result["channel"])
                duplicate = call(
                    "POST", "/_synapse/client/pangea/v1/deliver_notice", body
                )
                self.assertTrue(duplicate["duplicate"])
                self.assertEqual(
                    duplicate["notification_log_id"], result["notification_log_id"]
                )
                print(
                    json.dumps(
                        {
                            "method": method,
                            "channel": result["channel"],
                            "reason": result["reason"],
                            "log_id": result["notification_log_id"],
                            "run": run,
                        }
                    ),
                    flush=True,
                )
                invalid = copy.deepcopy(body)
                invalid["email"]["text"] = "No unsubscribe slot"
                rejected = session.post(
                    base + "/_synapse/client/pangea/v1/deliver_notice",
                    json=invalid,
                    timeout=30,
                )
                self.assertEqual(rejected.status_code, 400)
