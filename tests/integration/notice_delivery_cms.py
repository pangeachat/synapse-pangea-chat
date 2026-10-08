"""Real Synapse/Postgres, CMS/Postgres and SMTP delivery.

Start tests/fixtures/notice_log_cms.mts from the sibling CMS checkout first.
NOTICE_CMS_FIXTURE=/tmp/notice-cms.json python -m unittest tests.integration.notice_delivery_cms
"""

import copy
import json
import os
import socket
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

from synapse_pangea_chat.notice_delivery.common import TEMPLATES_DIR
from tests.base_e2e import BaseSynapseE2ETest
from tests.test_notice_content import request_body
from tests.test_notice_email_smtp import Links
from tests.test_register_email_e2e import MockSMTPServer


class TestNoticeDeliveryCMS(BaseSynapseE2ETest):
    async def test_content_delivery_log_duplicate_and_unsubscribe(self):
        cms = json.loads(Path(os.environ["NOTICE_CMS_FIXTURE"]).read_text())
        if not cms["url"].startswith("http://127.0.0.1:"):
            raise ValueError("This integration test requires local CMS")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.server_url = f"http://127.0.0.1:{port}"
        smtp = MockSMTPServer()
        smtp_port = smtp.start()
        self.addCleanup(smtp.stop)
        started = await self.start_test_synapse(
            module_config={
                "cms_base_url": cms["url"],
                "cms_service_api_key": cms["api_key"],
                "notice_email_enabled": True,
                "notice_email_postal_address": "1 Integration St",
                "notice_token_secret": "integration-secret",
                "app_base_url": "http://127.0.0.1/app",
            },
            synapse_config_overrides={
                "public_baseurl": self.server_url,
                "listeners": [
                    {
                        "port": port,
                        "type": "http",
                        "tls": False,
                        "bind_addresses": ["127.0.0.1"],
                        "resources": [{"names": ["client"], "compress": False}],
                    }
                ],
                "email": {
                    "smtp_host": "127.0.0.1",
                    "smtp_port": smtp_port,
                    "notif_from": "Pangea Chat <test@pangea.test>",
                    "require_transport_security": False,
                },
            },
        )
        postgres, directory, config_path, process, stdout, stderr = started
        try:
            await self.register_user(config_path, directory, "admin", "pw", admin=True)
            _, token = await self.login_user("admin", "pw")
            headers = {"Authorization": f"Bearer {token}"}
            uid = "@alice:my.domain.name"
            created = requests.put(
                f"{self.server_url}/_synapse/admin/v2/users/{uid}",
                headers=headers,
                json={
                    "password": "pw",
                    "threepids": [{"medium": "email", "address": "alice@example.test"}],
                },
                timeout=20,
            )
            self.assertEqual(created.status_code, 201, created.text)
            _, alice_token = await self.login_user("alice", "pw")
            prepared = requests.post(
                f"{self.server_url}/_synapse/client/pangea/v1/prepare_notice",
                headers=headers,
                json={"user_id": uid},
                timeout=20,
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            room = requests.post(
                f"{self.server_url}/_matrix/client/v3/createRoom",
                headers=headers,
                json={"preset": "private_chat", "invite": [uid]},
                timeout=20,
            ).json()["room_id"]
            joined = requests.post(
                f"{self.server_url}/_matrix/client/v3/join/{room}",
                headers={"Authorization": f"Bearer {alice_token}"},
                json={},
                timeout=20,
            )
            self.assertEqual(joined.status_code, 200, joined.text)
            event = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{room}/send/p.room.notice/test-notice",
                headers=headers,
                json={"body": "An activity", "check_in_type": "do_activity"},
                timeout=20,
            ).json()["event_id"]
            body = request_body("email-only")
            body.update(user_id=uid, notice_room_id=room, notice_event_id=event)
            body["log"]["run"]["run_id"] = "notice-228-integration-" + uuid.uuid4().hex
            env = Environment(
                loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape()
            )
            body["email"]["html"] = env.get_template("notice_email.html").render(
                app_name="Pangea Chat",
                title="An activity",
                body="A caller-rendered activity card",
                cta_label="Open activity",
                cta_url="{{cta_url}}",
                unsubscribe_url="{{unsubscribe_url}}",
                postal_address="{{postal_address}}",
                receiving_reason="{{receiving_reason}}",
            )
            url = f"{self.server_url}/_synapse/client/pangea/v1/deliver_notice"
            with ThreadPoolExecutor(max_workers=5) as pool:
                concurrent = list(
                    pool.map(
                        lambda _: requests.post(
                            url, headers=headers, json=body, timeout=45
                        ),
                        range(5),
                    )
                )
            self.assertTrue(
                all(r.status_code in (200, 409) for r in concurrent),
                [r.text for r in concurrent],
            )
            sent = next(
                r
                for r in concurrent
                if r.status_code == 200 and not r.json()["duplicate"]
            )
            self.assertEqual(sent.status_code, 200, sent.text)
            self.assertEqual(sent.json()["channel"], "email", sent.text)
            self.assertEqual(sent.json()["log_status"], "complete", sent.text)
            self.assertEqual(len(smtp.received_emails), 1)
            cms_headers = {"Authorization": f'service-users API-Key {cms["api_key"]}'}
            row = requests.get(
                f'{cms["url"]}/api/notification-log/{sent.json()["notification_log_id"]}',
                headers=cms_headers,
                timeout=20,
            )
            self.assertEqual(row.status_code, 200, row.text)
            self.assertEqual(row.json()["decision"]["channel"], "email")
            self.assertNotIn("alice@example.test", row.text)
            self.assertNotIn("A caller-rendered", row.text)
            retry = requests.post(url, headers=headers, json=body, timeout=45)
            self.assertEqual(retry.status_code, 200, retry.text)
            self.assertTrue(retry.json()["duplicate"])
            self.assertEqual(len(smtp.received_emails), 1)
            message = BytesParser(policy=policy.default).parsebytes(
                smtp.received_emails[0]["data"].removesuffix(".\r\n").encode()
            )
            html = message.get_body(preferencelist=("html",)).get_content()
            plain = message.get_body(preferencelist=("plain",)).get_content()
            self.assertIn("NSF.png", html)
            self.assertIn("Spanish &lt;101&gt;", html)
            self.assertIn("Spanish <101>", plain)
            links = Links()
            links.feed(html)
            cta = next(link for link in links.urls if "/pangea/v1/n?" in link)
            clicked = requests.get(cta, allow_redirects=False, timeout=20)
            self.assertEqual(clicked.status_code, 302)
            self.assertTrue(clicked.headers["Location"].endswith("/app/activity-1"))
            timeline = requests.get(
                f"{self.server_url}/_matrix/client/v3/rooms/{room}/messages?dir=b&limit=10",
                headers=headers,
                timeout=20,
            ).json()
            self.assertTrue(
                any(
                    e["type"] == "p.room.notice.opened"
                    and e["content"]["notification_event_id"] == event
                    for e in timeline["chunk"]
                )
            )
            unsub = str(message["List-Unsubscribe"]).strip("<> ")
            self.assertEqual(requests.get(unsub, timeout=20).status_code, 200)
            self.assertEqual(
                requests.post(
                    unsub, data={"List-Unsubscribe": "One-Click"}, timeout=20
                ).status_code,
                200,
            )
            body["log"]["run"]["run_id"] += "-refused"
            refused = requests.post(url, headers=headers, json=body, timeout=45)
            self.assertEqual(refused.status_code, 200, refused.text)
            self.assertEqual(refused.json()["channel"], "refused")
            self.assertEqual(refused.json()["log_status"], "complete")
            self.assertEqual(len(smtp.received_emails), 1)
            body["email"]["text"] = "Missing required links"
            invalid = requests.post(url, headers=headers, json=body, timeout=20)
            self.assertEqual(invalid.status_code, 400, invalid.text)

            # A bounded 100-decision batch exercises real CMS writes without
            # sending 100 emails. Reuse this test's timeline notice, and restore
            # its test account's preferences before in-app-only delivery.
            reset = requests.put(
                f"{self.server_url}/_matrix/client/v3/user/{uid}/account_data/pangea.communication_preferences",
                headers={"Authorization": f"Bearer {alice_token}"},
                json={"refused": [], "all_off": False},
                timeout=20,
            )
            self.assertEqual(reset.status_code, 200, reset.text)
            batch_body = request_body("in-app-only")
            batch_body.update(user_id=uid, notice_room_id=room, notice_event_id=event)
            prefix = "notice-228-integration-" + uuid.uuid4().hex

            def one(index):
                payload = copy.deepcopy(batch_body)
                payload["log"]["run"]["run_id"] = f"{prefix}-{index}"
                for _ in range(20):
                    response = requests.post(
                        url, headers=headers, json=payload, timeout=45
                    )
                    if response.status_code != 429:
                        return response
                    time.sleep(0.2)
                return response

            begin = time.monotonic()
            with ThreadPoolExecutor(max_workers=5) as pool:
                batch = list(pool.map(one, range(100)))
            self.assertTrue(
                all(
                    r.status_code == 200
                    and r.json()["channel"] == "in_app"
                    and r.json()["log_status"] == "complete"
                    for r in batch
                ),
                [(r.status_code, r.text) for r in batch if r.status_code != 200],
            )
            self.assertEqual(len({r.json()["notification_log_id"] for r in batch}), 100)
            self.assertEqual(len(smtp.received_emails), 1)
            print(
                f"100 local in-app decisions and CMS records, concurrency 5: {time.monotonic() - begin:.2f}s"
            )

            # Refill the configured admin burst after the bounded batch above.
            time.sleep(2)
            scheduled = request_body("email-only")
            del scheduled["notice_event_id"]
            scheduled.update(
                user_id=uid,
                notice_room_id=room,
                sender_id="@admin:my.domain.name",
                notice_content={
                    "body": "Scheduled activity",
                    "check_in_type": "do_activity",
                },
                scheduled_at=datetime.fromtimestamp(
                    time.time() + 20, timezone.utc
                ).isoformat(),
            )
            scheduled["log"]["run"]["run_id"] = (
                "notice-228-integration-scheduled-" + uuid.uuid4().hex
            )
            image_url = "https://content.pangea.chat/media/example-activity.jpg"
            scheduled["email"]["html"] = env.get_template("notice_email.html").render(
                title="Scheduled activity",
                body="A course and activity for you",
                images=[{"url": image_url, "alt": "Activity cover"}],
                cta_label="Open activity",
                cta_url="{{cta_url}}",
                unsubscribe_url="{{unsubscribe_url}}",
                postal_address="{{postal_address}}",
                receiving_reason="{{receiving_reason}}",
            )
            with ThreadPoolExecutor(max_workers=5) as pool:
                accepted = list(
                    pool.map(
                        lambda _: requests.post(
                            url, headers=headers, json=scheduled, timeout=20
                        ),
                        range(5),
                    )
                )
            self.assertTrue(
                all(r.status_code == 202 for r in accepted), [r.text for r in accepted]
            )
            self.assertEqual(len({r.json()["schedule_id"] for r in accepted}), 1)
            schedule_id = accepted[0].json()["schedule_id"]

            def notice_events():
                response = requests.get(
                    f"{self.server_url}/_matrix/client/v3/rooms/{room}/messages?dir=b&limit=100",
                    headers=headers,
                    timeout=20,
                )
                self.assertEqual(response.status_code, 200, response.text)
                return [
                    e for e in response.json()["chunk"] if e["type"] == "p.room.notice"
                ]

            self.assertEqual(len(notice_events()), 1)
            self.assertEqual(len(smtp.received_emails), 1)
            status_url = url + "?schedule_id=" + schedule_id
            self.assertEqual(
                requests.get(
                    status_url,
                    headers={"Authorization": f"Bearer {alice_token}"},
                    timeout=20,
                ).status_code,
                403,
            )

            # Restart the actual Synapse process with the same PostgreSQL DB.
            args = process.args
            process.terminate()
            process.wait(timeout=20)
            stdout.join(timeout=5)
            stderr.join(timeout=5)
            restart_log = tempfile.TemporaryFile(mode="w+")
            self.addCleanup(restart_log.close)
            process = subprocess.Popen(
                args,
                cwd=directory,
                stdout=restart_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            deadline = time.monotonic() + 40
            final = None
            while time.monotonic() < deadline:
                try:
                    response = requests.get(status_url, headers=headers, timeout=2)
                # silent-ok: connection refusal is expected during restart;
                # the bounded poll fails its final assertion if startup fails.
                except requests.ConnectionError:
                    time.sleep(0.2)
                    continue
                self.assertEqual(response.status_code, 200, response.text)
                final = response.json()
                if final["status"] == "complete":
                    break
                time.sleep(0.2)
            self.assertIsNotNone(final)
            self.assertEqual(final["status"], "complete", final)
            self.assertEqual(final["result"]["channel"], "email", final)
            self.assertEqual(final["result"]["log_status"], "complete", final)
            self.assertEqual(len(notice_events()), 2)
            self.assertEqual(len(smtp.received_emails), 2)
            delivered_event = next(
                e
                for e in notice_events()
                if e["event_id"] == final["result"]["notice_event_id"]
            )
            self.assertGreaterEqual(
                delivered_event["origin_server_ts"],
                accepted[0].json()["scheduled_at_ms"],
            )
            self.assertEqual(
                delivered_event["content"]["pangea.schedule_id"], schedule_id
            )
            persisted = requests.get(
                f'{cms["url"]}/api/notification-log/{final["result"]["notification_log_id"]}',
                headers=cms_headers,
                timeout=20,
            )
            self.assertEqual(persisted.status_code, 200, persisted.text)
            self.assertEqual(
                persisted.json()["decision"]["notice_event_id"],
                final["result"]["notice_event_id"],
            )
            message = BytesParser(policy=policy.default).parsebytes(
                smtp.received_emails[1]["data"].removesuffix(".\r\n").encode()
            )
            html = message.get_body(preferencelist=("html",)).get_content()
            self.assertIn(image_url, html)
            self.assertIn('alt="Activity cover"', html)
            self.assertIn("NSF.png", html)
            links = Links()
            links.feed(html)
            scheduled_cta = next(link for link in links.urls if "/pangea/v1/n?" in link)
            clicked = requests.get(scheduled_cta, allow_redirects=False, timeout=20)
            self.assertEqual(clicked.status_code, 302)
            self.assertTrue(clicked.headers["Location"].endswith("/app/activity-1"))
            opened = requests.get(
                f"{self.server_url}/_matrix/client/v3/rooms/{room}/messages?dir=b&limit=100",
                headers=headers,
                timeout=20,
            ).json()["chunk"]
            self.assertTrue(
                any(
                    e["type"] == "p.room.notice.opened"
                    and e["content"]["notification_event_id"]
                    == final["result"]["notice_event_id"]
                    for e in opened
                )
            )
            self.assertIn(
                "Open http://",
                message.get_body(preferencelist=("plain",)).get_content(),
            )
            retry = requests.post(url, headers=headers, json=scheduled, timeout=20)
            self.assertEqual(retry.status_code, 202, retry.text)
            self.assertTrue(retry.json()["duplicate"])
            self.assertEqual(len(notice_events()), 2)
            self.assertEqual(len(smtp.received_emails), 2)

            # Change eligibility after enqueue against real account data and
            # room membership, rather than only mocking the final read.
            def scheduled_case(name, change, expected_channel, expected_reason=None):
                payload = copy.deepcopy(scheduled)
                payload["delivery_method"] = "use-available"
                payload["log"]["run"]["run_id"] = (
                    "notice-228-integration-" + name + "-" + uuid.uuid4().hex
                )
                payload["scheduled_at"] = datetime.fromtimestamp(
                    time.time() + 8, timezone.utc
                ).isoformat()
                before_events = len(notice_events())
                before_emails = len(smtp.received_emails)
                queued = requests.post(url, headers=headers, json=payload, timeout=20)
                self.assertEqual(queued.status_code, 202, queued.text)
                change()
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    checked = requests.get(
                        url + "?schedule_id=" + queued.json()["schedule_id"],
                        headers=headers,
                        timeout=20,
                    )
                    self.assertEqual(checked.status_code, 200, checked.text)
                    if checked.json()["status"] == "complete":
                        break
                    time.sleep(0.3)
                self.assertEqual(checked.json()["status"], "complete", checked.text)
                outcome = checked.json()["result"]
                self.assertEqual(outcome["channel"], expected_channel, outcome)
                if expected_reason:
                    self.assertEqual(outcome["reason"], expected_reason)
                self.assertEqual(len(smtp.received_emails), before_emails)
                self.assertEqual(
                    len(notice_events()),
                    before_events + (1 if expected_channel == "in_app" else 0),
                )
                print(f"Scheduled {name}: {expected_channel}; no email")

            alice_headers = {"Authorization": f"Bearer {alice_token}"}

            def put_alice(path, value):
                response = requests.put(
                    self.server_url + path,
                    headers=alice_headers,
                    json=value,
                    timeout=20,
                )
                self.assertEqual(response.status_code, 200, response.text)

            preferences_path = f"/_matrix/client/v3/user/{uid}/account_data/pangea.communication_preferences"
            scheduled_case(
                "preference-change",
                lambda: put_alice(preferences_path, {"all_off": True}),
                "refused",
            )
            put_alice(preferences_path, {})
            scheduled_case(
                "active-at-send",
                lambda: put_alice(
                    f"/_matrix/client/v3/presence/{uid}/status", {"presence": "online"}
                ),
                "in_app",
            )

            scheduled["eligibility"] = {"recipient_not_returned": True}
            scheduled_case(
                "returned-at-send",
                lambda: put_alice(
                    f"/_matrix/client/v3/presence/{uid}/status", {"presence": "online"}
                ),
                "none",
                "recipient_returned",
            )
            scheduled["eligibility"] = {"min_contact_spacing_ms": 86400000}
            scheduled_case("recent-contact", lambda: None, "none", "contact_spacing")

            def put_state(kind, value):
                response = requests.put(
                    f"{self.server_url}/_matrix/client/v3/rooms/{room}/state/{kind}",
                    headers=headers,
                    json=value,
                    timeout=20,
                )
                self.assertEqual(response.status_code, 200, response.text)

            put_state(
                "pangea.activity_plan",
                {"activity_id": "activity-1", "roles": [{"role_id": "one"}]},
            )
            scheduled["eligibility"] = {"activity_not_started": True}
            scheduled_case(
                "activity-started",
                lambda: put_state(
                    "pangea.activity_roles", {"roles": {"one": {"user_id": uid}}}
                ),
                "none",
                "activity_already_started",
            )
            scheduled["eligibility"] = {"session_available": True}
            scheduled["session_room_id"] = room
            scheduled_case("full-session", lambda: None, "none", "session_full")
            scheduled_case(
                "unknown-plan",
                lambda: put_state(
                    "pangea.activity_plan",
                    {
                        "activity_id": "activity-1",
                        "version_id": "00000000-0000-0000-0000-000000000000",
                    },
                ),
                "none",
                "eligibility_unavailable",
            )
            put_state(
                "pangea.activity_plan",
                {"activity_id": "activity-1", "roles": [{"role_id": "one"}]},
            )
            put_state("pangea.activity_roles", {"roles": {}})
            del scheduled["session_room_id"]
            saved_room = requests.post(
                f"{self.server_url}/_matrix/client/v3/createRoom",
                headers=alice_headers,
                json={
                    "preset": "private_chat",
                    "initial_state": [
                        {
                            "type": "pangea.activity_room_ids",
                            "state_key": "",
                            "content": {"room_ids": [room]},
                        }
                    ],
                },
                timeout=20,
            )
            self.assertEqual(saved_room.status_code, 200, saved_room.text)
            scheduled["eligibility"] = {"activity_not_started": True}
            scheduled_case(
                "saved-completion", lambda: None, "none", "activity_already_completed"
            )
            del scheduled["eligibility"]

            cancelled_body = copy.deepcopy(scheduled)
            cancelled_body["log"]["run"]["run_id"] += "-cancelled"
            cancelled_body["scheduled_at"] = datetime.fromtimestamp(
                time.time() + 8, timezone.utc
            ).isoformat()
            queued = requests.post(
                url, headers=headers, json=cancelled_body, timeout=20
            )
            self.assertEqual(queued.status_code, 202, queued.text)
            cancel_url = url + "?schedule_id=" + queued.json()["schedule_id"]
            self.assertEqual(
                requests.delete(
                    cancel_url, headers=alice_headers, timeout=20
                ).status_code,
                403,
            )
            for _ in range(2):
                cancelled = requests.delete(cancel_url, headers=headers, timeout=20)
                self.assertEqual(cancelled.status_code, 200, cancelled.text)
                self.assertEqual(cancelled.json()["status"], "cancelled")
            before_cancel_events = len(notice_events())

            def leave():
                response = requests.post(
                    f"{self.server_url}/_matrix/client/v3/rooms/{room}/leave",
                    headers=alice_headers,
                    json={},
                    timeout=20,
                )
                self.assertEqual(response.status_code, 200, response.text)

            scheduled_case(
                "membership-change", leave, "none", "scheduled_target_ineligible"
            )
            self.assertEqual(len(notice_events()), before_cancel_events)
            self.assertEqual(
                requests.get(cancel_url, headers=headers, timeout=20).json()["status"],
                "cancelled",
            )
        finally:
            self.stop_synapse(
                server_process=process,
                stdout_thread=stdout,
                stderr_thread=stderr,
                synapse_dir=directory,
                postgres=postgres,
            )
