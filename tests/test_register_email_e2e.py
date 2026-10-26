import asyncio
import logging
import socket
import threading
from typing import Any, List, Optional, cast

import requests

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.register_email.is_rate_limited import (
    is_rate_limited,
    request_log,
)

from .base_e2e import BaseSynapseE2ETest

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    filename="synapse.log",
    filemode="a",
)

ENDPOINT = "/_synapse/client/pangea/v1/register/email/requestToken"
# Synapse's own registration endpoint, reachable independently of the Pangea
# route above. Covering it is the reason the address policy is a homeserver-wide
# callback rather than a check inside the Pangea endpoint.
BUILTIN_ENDPOINT = "/_matrix/client/v3/register/email/requestToken"
# The endpoint's per-IP burst limit (5 a minute by default) is kept in the
# server process, and every test talks from 127.0.0.1, so on a class's shared
# homeserver the later tests would be refused for the earlier tests' requests.
# Only TestRegisterEmailRateLimit, on a homeserver of its own, tests the limit.
NO_BURST_LIMIT = {"register_email_requests_per_burst": 1000}


class MockSMTPServer:
    """Minimal SMTP server that accepts all emails without sending them."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._host = host
        self._port = port
        self._server_socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.received_emails: List[dict] = []

    @property
    def port(self) -> int:
        if self._server_socket is None:
            raise RuntimeError("Server not started")
        return self._server_socket.getsockname()[1]

    def start(self) -> int:
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self._host, self._port))
        self._server_socket.listen(5)
        self._server_socket.settimeout(1.0)
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        self._running = False
        if self._server_socket:
            self._server_socket.close()
        if self._thread:
            self._thread.join(timeout=5)

    def emails_to(self, address: str) -> List[dict]:
        """The emails received for `address`, so that a test sharing this
        server with others reads only the mail it caused."""
        recipient = f"<{address.lower()}>"
        return [
            email
            for email in list(self.received_emails)
            if recipient in email["to"].lower()
        ]

    def _run(self):
        while self._running:
            try:
                conn, addr = self._server_socket.accept()
                threading.Thread(
                    target=self._handle_client, args=(conn,), daemon=True
                ).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle_client(self, conn: socket.socket):
        try:
            conn.settimeout(10)
            conn.sendall(b"220 localhost Mock SMTP\r\n")
            mail_from = ""
            rcpt_to = ""
            data = ""
            while True:
                raw = conn.recv(4096)
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                upper = line.upper()
                if upper.startswith(("EHLO", "HELO")):
                    conn.sendall(b"250 Hello\r\n")
                elif upper.startswith("MAIL FROM"):
                    mail_from = line
                    conn.sendall(b"250 OK\r\n")
                elif upper.startswith("RCPT TO"):
                    rcpt_to = line
                    conn.sendall(b"250 OK\r\n")
                elif upper.startswith("DATA"):
                    conn.sendall(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                    msg_data = b""
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        msg_data += chunk
                        if b"\r\n.\r\n" in msg_data:
                            break
                    data = msg_data.decode("utf-8", errors="replace")
                    self.received_emails.append(
                        {"from": mail_from, "to": rcpt_to, "data": data}
                    )
                    conn.sendall(b"250 OK\r\n")
                elif upper.startswith("QUIT"):
                    conn.sendall(b"221 Bye\r\n")
                    break
                elif upper.startswith("STARTTLS"):
                    conn.sendall(b"502 STARTTLS not supported\r\n")
                else:
                    conn.sendall(b"250 OK\r\n")
        except Exception as e:
            logger.debug("Mock SMTP client error: %s", e)
        finally:
            conn.close()


class TestRegisterEmailE2ENoEmailConfig(BaseSynapseE2ETest):
    """Tests for the register email endpoint WITHOUT email configured.

    These test username validation and parameter checking
    (email-send is never reached).
    """

    async def _homeserver_no_email(self):
        return await self.homeserver(module_config=NO_BURST_LIMIT)

    # --- Parameter validation tests ---

    async def test_missing_username(self) -> None:
        """POST without 'username' → 400 M_MISSING_PARAM."""
        await self._homeserver_no_email()

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "client_secret": "s3cr3t",
                "email": "user@example.com",
                "send_attempt": 1,
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["errcode"], "M_MISSING_PARAM")
        self.assertIn("username", body["error"])

    async def test_missing_email(self) -> None:
        """POST without 'email' → 400 M_MISSING_PARAM."""
        await self._homeserver_no_email()

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": "newuser",
                "client_secret": "s3cr3t",
                "send_attempt": 1,
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["errcode"], "M_MISSING_PARAM")
        self.assertIn("email", body["error"])

    async def test_missing_client_secret(self) -> None:
        """POST without 'client_secret' → 400 M_MISSING_PARAM."""
        await self._homeserver_no_email()

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": "newuser",
                "email": "user@example.com",
                "send_attempt": 1,
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["errcode"], "M_MISSING_PARAM")
        self.assertIn("client_secret", body["error"])

    async def test_missing_send_attempt(self) -> None:
        """POST without 'send_attempt' → 400 M_MISSING_PARAM."""
        await self._homeserver_no_email()

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": "newuser",
                "client_secret": "s3cr3t",
                "email": "user@example.com",
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["errcode"], "M_MISSING_PARAM")
        self.assertIn("send_attempt", body["error"])

    async def test_all_fields_missing(self) -> None:
        """POST with empty body → 400 M_MISSING_PARAM listing all fields."""
        await self._homeserver_no_email()

        response = requests.post(self.server_url + ENDPOINT, json={})
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["errcode"], "M_MISSING_PARAM")
        for field in ("username", "client_secret", "email", "send_attempt"):
            self.assertIn(field, body["error"])

    async def test_invalid_json_body(self) -> None:
        """POST with non-JSON body → 400 M_NOT_JSON."""
        await self._homeserver_no_email()

        response = requests.post(
            self.server_url + ENDPOINT,
            data="not json at all",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["errcode"], "M_NOT_JSON")

    # --- Username validation tests ---

    async def test_invalid_username_special_chars(self) -> None:
        """Username with invalid characters → 400 M_INVALID_USERNAME."""
        await self._homeserver_no_email()

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": "bad@user!name",
                "client_secret": "s3cr3t",
                "email": "user@example.com",
                "send_attempt": 1,
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["errcode"], "M_INVALID_USERNAME")

    async def test_empty_username(self) -> None:
        """Empty username → 400 M_INVALID_USERNAME."""
        await self._homeserver_no_email()

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": "",
                "client_secret": "s3cr3t",
                "email": "user@example.com",
                "send_attempt": 1,
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["errcode"], "M_INVALID_USERNAME")

    async def test_username_starts_with_underscore(self) -> None:
        """Username starting with '_' → 400 M_INVALID_USERNAME."""
        await self._homeserver_no_email()

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": "_reserved",
                "client_secret": "s3cr3t",
                "email": "user@example.com",
                "send_attempt": 1,
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["errcode"], "M_INVALID_USERNAME")

    async def test_username_too_long(self) -> None:
        """Username exceeding max length → 400 M_INVALID_USERNAME."""
        await self._homeserver_no_email()

        # Matrix user IDs max out at 255 chars for the full @localpart:server
        # The server name is "my.domain.name" (14 chars) + @ + : = 16 overhead
        # So localpart must be < 255 - 16 = 239 chars. Use 250 to be safe.
        long_username = "a" * 250
        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": long_username,
                "client_secret": "s3cr3t",
                "email": "user@example.com",
                "send_attempt": 1,
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["errcode"], "M_INVALID_USERNAME")

    async def test_username_already_taken(self) -> None:
        """Username already registered → 400 M_USER_IN_USE."""
        await self._homeserver_no_email()

        # Register a user first
        await self.new_user("existinguser", admin=True)

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": self.localpart("existinguser"),
                "client_secret": "s3cr3t",
                "email": "user@example.com",
                "send_attempt": 1,
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["errcode"], "M_USER_IN_USE")

    async def test_username_taken_case_insensitive(self) -> None:
        """Username check is case-insensitive: 'ExistingUser' taken if 'existinguser' exists."""
        await self._homeserver_no_email()

        await self.new_user("myuser", admin=True)

        # Try uppercase variant
        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": f"MyUser_{self.tag}",
                "client_secret": "s3cr3t",
                "email": "user@example.com",
                "send_attempt": 1,
            },
        )
        # Synapse stores usernames lowercase, but check_username does
        # case-insensitive lookup. Uppercase chars are also rejected
        # by the invalid characters check (only a-z allowed).
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn(body["errcode"], ["M_INVALID_USERNAME", "M_USER_IN_USE"])

    async def test_valid_username_no_email_config(self) -> None:
        """Valid username but no email config → 400 email registration disabled."""
        await self._homeserver_no_email()

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": "validnewuser",
                "client_secret": "s3cr3t",
                "email": "user@example.com",
                "send_attempt": 1,
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertIn("disabled", body["error"].lower())


class EmailConfiguredSynapseMixin:
    """Starts the test homeserver with a mock SMTP server attached.

    The SMTP server's port is in the homeserver config, so it lives as long as
    the class's shared homeserver, and each test reads only the mail sent to
    its own address.
    """

    smtp_server: MockSMTPServer

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()  # type: ignore[misc]
        cls.smtp_server = MockSMTPServer()
        smtp_port = cls.smtp_server.start()
        logger.info("Mock SMTP server started on port %d", smtp_port)
        cls.addClassCleanup(cls.smtp_server.stop)

    def _make_email_synapse_config(self, smtp_port: int) -> dict:
        return {
            "email": {
                "smtp_host": "127.0.0.1",
                "smtp_port": smtp_port,
                "notif_from": "Pangea Test <test@pangea.test>",
                "require_transport_security": False,
            },
        }

    async def _homeserver_with_email(self):
        return await self.homeserver(
            module_config=NO_BURST_LIMIT,
            synapse_config_overrides=self._make_email_synapse_config(
                self.smtp_server.port
            ),
        )


class TestRegisterEmailE2EWithEmailConfig(
    EmailConfiguredSynapseMixin, BaseSynapseE2ETest
):
    """Tests for the register email endpoint WITH email configured.

    Uses a mock SMTP server to capture outgoing emails.
    """

    async def test_happy_path_valid_username_and_email(self) -> None:
        """Valid username + valid email → 200 with sid."""
        await self._homeserver_with_email()
        address = f"newuser+{self.tag}@example.com"

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": "brandnewuser",
                "client_secret": "mysecret123",
                "email": address,
                "send_attempt": 1,
            },
        )
        self.assertEqual(response.status_code, 200, response.json())
        body = response.json()
        self.assertIn("sid", body)
        self.assertIsInstance(body["sid"], str)
        self.assertTrue(len(body["sid"]) > 0)

        # Verify email was actually sent via mock SMTP
        await asyncio.sleep(1)
        self.assertGreater(
            len(self.smtp_server.emails_to(address)),
            0,
            "Expected at least one email to be sent via SMTP",
        )

    async def test_happy_path_with_next_link(self) -> None:
        """Valid request with next_link → 200 with sid."""
        await self._homeserver_with_email()

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": "nextlinkuser",
                "client_secret": "mysecret456",
                "email": f"nextlink+{self.tag}@example.com",
                "send_attempt": 1,
                "next_link": "https://app.pangea.chat/register",
            },
        )
        self.assertEqual(response.status_code, 200, response.json())
        body = response.json()
        self.assertIn("sid", body)

    async def test_idempotent_send_attempt(self) -> None:
        """Same email/client_secret/send_attempt returns same sid without re-sending."""
        await self._homeserver_with_email()

        payload = {
            "username": "idempotentuser",
            "client_secret": "idempotentsecret",
            "email": f"idempotent+{self.tag}@example.com",
            "send_attempt": 1,
        }

        # First request
        resp1 = requests.post(self.server_url + ENDPOINT, json=cast(Any, payload))
        self.assertEqual(resp1.status_code, 200, resp1.json())
        sid1 = resp1.json()["sid"]

        # Same request again (same send_attempt) — should return same sid
        resp2 = requests.post(self.server_url + ENDPOINT, json=cast(Any, payload))
        self.assertEqual(resp2.status_code, 200, resp2.json())
        sid2 = resp2.json()["sid"]

        self.assertEqual(sid1, sid2)

    async def test_higher_send_attempt_re_sends(self) -> None:
        """Higher send_attempt with same email/client_secret re-sends email."""
        await self._homeserver_with_email()

        address = f"resend+{self.tag}@example.com"
        base_payload = {
            "username": "resenduser",
            "client_secret": "resendsecret",
            "email": address,
        }

        # First send_attempt
        resp1 = requests.post(
            self.server_url + ENDPOINT, json={**base_payload, "send_attempt": 1}
        )
        self.assertEqual(resp1.status_code, 200, resp1.json())
        sid1 = resp1.json()["sid"]

        await asyncio.sleep(0.5)
        emails_after_first = len(self.smtp_server.emails_to(address))

        # Higher send_attempt should re-send
        resp2 = requests.post(
            self.server_url + ENDPOINT, json={**base_payload, "send_attempt": 2}
        )
        self.assertEqual(resp2.status_code, 200, resp2.json())
        sid2 = resp2.json()["sid"]

        # Same session
        self.assertEqual(sid1, sid2)

        # Should have sent an additional email
        await asyncio.sleep(1)
        self.assertGreater(
            len(self.smtp_server.emails_to(address)),
            emails_after_first,
            "Expected a new email for higher send_attempt",
        )

    async def test_email_already_in_use(self) -> None:
        """Email already bound to an account → 400 M_THREEPID_IN_USE."""
        await self._homeserver_with_email()

        taken = f"taken+{self.tag}@example.com"

        # Register a user and bind an email via the Admin API
        user_id, access_token = await self.new_user("emailowner", admin=True)

        # Use Synapse Admin API to bind the 3PID
        admin_url = f"{self.server_url}/_synapse/admin/v2/users/{user_id}"
        resp = requests.put(
            admin_url,
            json={"threepids": [{"medium": "email", "address": taken}]},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(
            resp.status_code,
            200,
            f"Failed to bind 3PID: {resp.text}",
        )

        # Now try to register with that email
        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": "anothernewuser",
                "client_secret": "emailsecret",
                "email": taken,
                "send_attempt": 1,
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["errcode"], "M_THREEPID_IN_USE")

    async def test_username_taken_does_not_send_email(self) -> None:
        """If username is taken, no email should be sent."""
        await self._homeserver_with_email()

        address = f"innocent+{self.tag}@example.com"

        # Register a user
        await self.new_user("takenuser", admin=True)

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": self.localpart("takenuser"),
                "client_secret": "somesecret",
                "email": address,
                "send_attempt": 1,
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["errcode"], "M_USER_IN_USE")

        # Verify no email was sent
        await asyncio.sleep(1)
        self.assertEqual(
            self.smtp_server.emails_to(address),
            [],
            "No email should be sent when username is already taken",
        )

    async def test_invalid_email_format(self) -> None:
        """Invalid email format → 400."""
        await self._homeserver_with_email()

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": "validuser",
                "client_secret": "s3cr3t",
                "email": "not-an-email",
                "send_attempt": 1,
            },
        )
        self.assertEqual(response.status_code, 400)

    async def test_invalid_username_with_email_config(self) -> None:
        """Invalid username still rejected even with email configured."""
        await self._homeserver_with_email()
        address = f"user+{self.tag}@example.com"

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": "bad@user",
                "client_secret": "s3cr3t",
                "email": address,
                "send_attempt": 1,
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["errcode"], "M_INVALID_USERNAME")

        # Verify no email was sent for invalid usernames.
        await asyncio.sleep(1)
        self.assertEqual(
            self.smtp_server.emails_to(address),
            [],
            "No email should be sent when username is invalid",
        )


class TestRegisterEmailRateLimit(BaseSynapseE2ETest):
    """Rate limiting tests for the register email endpoint."""

    def setUp(self):
        super().setUp()
        # Clear rate limit state between tests
        request_log.clear()

    async def test_rate_limit_unit(self) -> None:
        """Unit test: IP-based rate limiter blocks after burst limit."""
        ip = "192.168.1.1"
        config = PangeaChatConfig(
            register_email_requests_per_burst=3,
            register_email_burst_duration_seconds=5,
        )
        for _ in range(config.register_email_requests_per_burst):
            self.assertFalse(is_rate_limited(ip, config))
        self.assertTrue(is_rate_limited(ip, config))

    async def test_rate_limit_window_expiry(self) -> None:
        """Unit test: Rate limit resets after burst duration."""
        ip = "192.168.1.2"
        config = PangeaChatConfig(
            register_email_requests_per_burst=2,
            register_email_burst_duration_seconds=2,
        )
        for _ in range(config.register_email_requests_per_burst):
            self.assertFalse(is_rate_limited(ip, config))
        self.assertTrue(is_rate_limited(ip, config))

        await asyncio.sleep(config.register_email_burst_duration_seconds + 1)
        self.assertFalse(is_rate_limited(ip, config))

    async def test_rate_limit_different_ips(self) -> None:
        """Unit test: Different IPs have independent rate limit buckets."""
        config = PangeaChatConfig(
            register_email_requests_per_burst=1,
            register_email_burst_duration_seconds=60,
        )
        self.assertFalse(is_rate_limited("10.0.0.1", config))
        self.assertTrue(is_rate_limited("10.0.0.1", config))
        # Different IP is not rate limited
        self.assertFalse(is_rate_limited("10.0.0.2", config))

    async def test_rate_limit_e2e(self) -> None:
        """E2E: Endpoint returns 429 after exceeding rate limit."""
        # The only test of this class that starts a homeserver, so the only
        # one whose requests count against this limit.
        await self.homeserver(
            module_config={
                "register_email_requests_per_burst": 2,
                "register_email_burst_duration_seconds": 60,
            },
        )

        payload = {
            "username": "ratelimituser",
            "client_secret": "ratesecret",
            "email": "rate@example.com",
            "send_attempt": 1,
        }

        # First requests should succeed (or fail with username/email errors,
        # but NOT 429)
        for i in range(2):
            response = requests.post(
                self.server_url + ENDPOINT, json=cast(Any, payload)
            )
            self.assertNotEqual(
                response.status_code,
                429,
                f"Request {i+1} should not be rate limited",
            )

        # Next request should be rate limited
        response = requests.post(self.server_url + ENDPOINT, json=cast(Any, payload))
        self.assertEqual(response.status_code, 429)
        body = response.json()
        self.assertEqual(body["errcode"], "M_LIMIT_EXCEEDED")


class TestRegisterEmailAddressPolicy(EmailConfiguredSynapseMixin, BaseSynapseE2ETest):
    """The address policy, on both endpoints that can send a verification email.

    Design: .github/instructions/email-address-policy.instructions.md
    """

    def _make_email_synapse_config(self, smtp_port: int) -> dict:
        config = super()._make_email_synapse_config(smtp_port)
        # The built-in endpoint answers 403 "registration is disabled" before it
        # ever reaches the policy, so registration has to be open to test it.
        config["enable_registration"] = True
        config["registrations_require_3pid"] = ["email"]
        return config

    async def test_pangea_endpoint_refuses_address_and_sends_nothing(self) -> None:
        """An address like `a@b` is what the old presence-and-"@" check let through."""
        await self._homeserver_with_email()
        address = f"a+{self.tag}@b"

        response = requests.post(
            self.server_url + ENDPOINT,
            json={
                "username": "policyuser",
                "client_secret": "s3cr3t",
                "email": address,
                "send_attempt": 1,
            },
        )

        self.assertEqual(response.status_code, 400, response.text)
        # Malformed, not "domain not authorized" — the Pangea endpoint runs
        # the policy itself so the app can say which it was.
        self.assertEqual(response.json()["errcode"], "M_INVALID_PARAM")

        await asyncio.sleep(1)
        self.assertEqual(
            self.smtp_server.emails_to(address),
            [],
            "No email should be sent for an address failing the policy",
        )

    async def test_builtin_endpoint_refuses_address_and_sends_nothing(self) -> None:
        """The path the app does not use, and the reason for the callback."""
        await self._homeserver_with_email()
        address = f"a+{self.tag}@b"

        response = requests.post(
            self.server_url + BUILTIN_ENDPOINT,
            json={
                "client_secret": "s3cr3t",
                "email": address,
                "send_attempt": 1,
            },
        )

        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["errcode"], "M_THREEPID_DENIED")

        await asyncio.sleep(1)
        self.assertEqual(
            self.smtp_server.emails_to(address),
            [],
            "No email should be sent for an address failing the policy",
        )

    async def test_builtin_endpoint_still_sends_for_a_good_address(self) -> None:
        """The control: the callback refuses junk, not everything."""
        await self._homeserver_with_email()
        address = f"builtin-learner+{self.tag}@example.com"

        response = requests.post(
            self.server_url + BUILTIN_ENDPOINT,
            json={
                "client_secret": "s3cr3t",
                "email": address,
                "send_attempt": 1,
            },
        )

        self.assertEqual(response.status_code, 200, response.text)

        await asyncio.sleep(1)
        self.assertGreater(len(self.smtp_server.emails_to(address)), 0)
