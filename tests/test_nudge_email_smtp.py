"""Real local delivery, captured at SMTP. Optionally export the received MIME.

NUDGE_EMAIL_CAPTURE_DIR=/tmp/pangea-nudge-capture python -m unittest tests.test_nudge_email_smtp
"""

import os
import socket
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from jinja2 import Environment, FileSystemLoader

from synapse_pangea_chat.nudge_delivery.common import TEMPLATES_DIR
from synapse_pangea_chat.nudge_delivery.tokens import verify_token

from .base_e2e import BaseSynapseE2ETest
from .test_register_email_e2e import MockSMTPServer


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.urls.extend(value for key, value in attrs if key == "href")


class TestNudgeEmailSMTP(BaseSynapseE2ETest):
    async def test_real_delivery_has_brand_mime_and_working_unsubscribe(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.server_url = f"http://127.0.0.1:{port}"
        smtp = MockSMTPServer()
        smtp_port = smtp.start()
        self.addCleanup(smtp.stop)
        brand_address = (
            Environment(loader=FileSystemLoader(TEMPLATES_DIR))
            .get_template("brand_base.html")
            .module.brand_postal_address
        )
        started = await self.start_test_synapse(
            module_config={
                "nudge_email_enabled": True,
                "nudge_email_postal_address": brand_address,
                "nudge_token_secret": "local-test-secret",
                "app_base_url": "http://127.0.0.1/preview-only",
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
            await self.register_user(
                config_path, directory, "admin", "test-password", admin=True
            )
            _, token = await self.login_user("admin", "test-password")
            headers = {"Authorization": f"Bearer {token}"}
            user = "@preview:my.domain.name"
            response = requests.put(
                f"{self.server_url}/_synapse/admin/v2/users/{user}",
                headers=headers,
                json={
                    "password": "unused-test-password",
                    "threepids": [
                        {"medium": "email", "address": "preview@example.test"}
                    ],
                },
                timeout=20,
            )
            self.assertEqual(response.status_code, 201, response.text)
            response = requests.post(
                f"{self.server_url}/_synapse/client/pangea/v1/deliver_nudge",
                headers=headers,
                json={
                    "user_id": user,
                    "category": "suggestions",
                    "variant": "default",
                    "title": "Local brand email test",
                    "body": "A little practice goes a long way. Start a conversation and try something new today.",
                    "cta_label": "Open Pangea Chat",
                },
                timeout=30,
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["channel"], "email")
            self.assertEqual(len(smtp.received_emails), 1)
            raw = smtp.received_emails[0]["data"].removesuffix(".\r\n").encode()
            message = BytesParser(policy=policy.default).parsebytes(raw)
            html = message.get_body(preferencelist=("html",)).get_content()
            plain = message.get_body(preferencelist=("plain",)).get_content()
            self.assertIn("Medium-Dark-Horizontal-Logo.png", html)
            self.assertIn("NSF.png", html)
            self.assertIn(brand_address, html)
            self.assertIn(brand_address, plain)
            self.assertEqual(
                message["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click"
            )
            links = Links()
            links.feed(html)
            unsub = str(message["List-Unsubscribe"]).strip("<> ")
            self.assertIn(unsub, links.urls)
            self.assertIn(unsub, plain)
            cta = next(url for url in links.urls if "/pangea/v1/n?" in url)
            signed = parse_qs(urlparse(cta).query)["t"][0]
            payload = verify_token(
                b"local-test-secret",
                signed,
                now_ms=__import__("time").time_ns() // 1_000_000,
            )
            self.assertEqual(payload["u"], user)
            self.assertEqual(payload["k"], "click")
            clicked = requests.get(cta, allow_redirects=False, timeout=20)
            self.assertEqual(clicked.status_code, 302)
            self.assertEqual(
                clicked.headers["Location"], "http://127.0.0.1/preview-only/"
            )
            confirmation = requests.get(unsub, timeout=20)
            self.assertEqual(confirmation.status_code, 200)
            self.assertIn("conversation and course suggestions", confirmation.text)
            account_path = (
                f"{self.server_url}/_synapse/admin/v1/users/{user}/accountdata"
            )
            before = requests.get(account_path, headers=headers, timeout=20).json()
            self.assertNotIn(
                "pangea.communication_preferences", before["account_data"]["global"]
            )
            refused = requests.post(
                unsub, data={"List-Unsubscribe": "One-Click"}, timeout=20
            )
            self.assertEqual(refused.status_code, 200)
            after = requests.get(account_path, headers=headers, timeout=20).json()
            self.assertIn(
                "suggestions",
                after["account_data"]["global"]["pangea.communication_preferences"][
                    "refused"
                ],
            )
            if output := os.environ.get("NUDGE_EMAIL_CAPTURE_DIR"):
                destination = Path(output)
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "email.eml").write_bytes(raw)
                (destination / "email.html").write_text(html)
                (destination / "email.txt").write_text(plain)
                print(f"Captured local SMTP email: {destination.resolve()}")
        finally:
            self.stop_synapse(
                server_process=process,
                stdout_thread=stdout,
                stderr_thread=stderr,
                synapse_dir=directory,
                postgres=postgres,
            )
