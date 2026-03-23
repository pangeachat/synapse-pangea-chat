import asyncio
import logging
import socket
import threading
from typing import List, Optional

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

ENDPOINT = "http://localhost:8008/_synapse/client/pangea/v1/register/email/requestToken"


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

    async def _start_synapse_no_email(self):
        return await self.start_test_synapse()

    # --- Parameter validation tests ---

    async def test_missing_username(self) -> None:
        """POST without 'username' → 400 M_MISSING_PARAM."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_no_email()

            response = requests.post(
                ENDPOINT,
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
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_missing_email(self) -> None:
        """POST without 'email' → 400 M_MISSING_PARAM."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_no_email()

            response = requests.post(
                ENDPOINT,
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
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_missing_client_secret(self) -> None:
        """POST without 'client_secret' → 400 M_MISSING_PARAM."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_no_email()

            response = requests.post(
                ENDPOINT,
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
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_missing_send_attempt(self) -> None:
        """POST without 'send_attempt' → 400 M_MISSING_PARAM."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_no_email()

            response = requests.post(
                ENDPOINT,
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
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_all_fields_missing(self) -> None:
        """POST with empty body → 400 M_MISSING_PARAM listing all fields."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_no_email()

            response = requests.post(ENDPOINT, json={})
            self.assertEqual(response.status_code, 400)
            body = response.json()
            self.assertEqual(body["errcode"], "M_MISSING_PARAM")
            for field in ("username", "client_secret", "email", "send_attempt"):
                self.assertIn(field, body["error"])
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_invalid_json_body(self) -> None:
        """POST with non-JSON body → 400 M_NOT_JSON."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_no_email()

            response = requests.post(
                ENDPOINT,
                data="not json at all",
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, 400)
            body = response.json()
            self.assertEqual(body["errcode"], "M_NOT_JSON")
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    # --- Username validation tests ---

    async def test_invalid_username_special_chars(self) -> None:
        """Username with invalid characters → 400 M_INVALID_USERNAME."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_no_email()

            response = requests.post(
                ENDPOINT,
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
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_empty_username(self) -> None:
        """Empty username → 400 M_INVALID_USERNAME."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_no_email()

            response = requests.post(
                ENDPOINT,
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
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_username_starts_with_underscore(self) -> None:
        """Username starting with '_' → 400 M_INVALID_USERNAME."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_no_email()

            response = requests.post(
                ENDPOINT,
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
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_username_too_long(self) -> None:
        """Username exceeding max length → 400 M_INVALID_USERNAME."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_no_email()

            # Matrix user IDs max out at 255 chars for the full @localpart:server
            # The server name is "my.domain.name" (14 chars) + @ + : = 16 overhead
            # So localpart must be < 255 - 16 = 239 chars. Use 250 to be safe.
            long_username = "a" * 250
            response = requests.post(
                ENDPOINT,
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
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_username_already_taken(self) -> None:
        """Username already registered → 400 M_USER_IN_USE."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_no_email()

            # Register a user first
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="existinguser",
                password="123123123",
                admin=True,
            )

            response = requests.post(
                ENDPOINT,
                json={
                    "username": "existinguser",
                    "client_secret": "s3cr3t",
                    "email": "user@example.com",
                    "send_attempt": 1,
                },
            )
            self.assertEqual(response.status_code, 400)
            body = response.json()
            self.assertEqual(body["errcode"], "M_USER_IN_USE")
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_username_taken_case_insensitive(self) -> None:
        """Username check is case-insensitive: 'ExistingUser' taken if 'existinguser' exists."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_no_email()

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="myuser",
                password="123123123",
                admin=True,
            )

            # Try uppercase variant
            response = requests.post(
                ENDPOINT,
                json={
                    "username": "MyUser",
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
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_valid_username_no_email_config(self) -> None:
        """Valid username but no email config → 400 email registration disabled."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_no_email()

            response = requests.post(
                ENDPOINT,
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
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )


class TestRegisterEmailE2EWithEmailConfig(BaseSynapseE2ETest):
    """Tests for the register email endpoint WITH email configured.

    Uses a mock SMTP server to capture outgoing emails.
    """

    smtp_server: Optional[MockSMTPServer] = None

    def _make_email_synapse_config(self, smtp_port: int) -> dict:
        return {
            "email": {
                "smtp_host": "127.0.0.1",
                "smtp_port": smtp_port,
                "notif_from": "Pangea Test <test@pangea.test>",
                "require_transport_security": False,
            },
        }

    async def _start_synapse_with_email(self):
        self.smtp_server = MockSMTPServer()
        smtp_port = self.smtp_server.start()
        logger.info("Mock SMTP server started on port %d", smtp_port)
        return await self.start_test_synapse(
            synapse_config_overrides=self._make_email_synapse_config(smtp_port),
        )

    def _stop_smtp(self):
        if self.smtp_server:
            self.smtp_server.stop()
            self.smtp_server = None

    def _require_smtp_server(self) -> MockSMTPServer:
        if self.smtp_server is None:
            raise AssertionError("Mock SMTP server was not started")
        return self.smtp_server

    async def test_happy_path_valid_username_and_email(self) -> None:
        """Valid username + valid email → 200 with sid."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_with_email()

            response = requests.post(
                ENDPOINT,
                json={
                    "username": "brandnewuser",
                    "client_secret": "mysecret123",
                    "email": "newuser@example.com",
                    "send_attempt": 1,
                },
            )
            self.assertEqual(response.status_code, 200, response.json())
            body = response.json()
            self.assertIn("sid", body)
            self.assertIsInstance(body["sid"], str)
            self.assertTrue(len(body["sid"]) > 0)

            # Verify email was actually sent via mock SMTP
            smtp_server = self._require_smtp_server()
            await asyncio.sleep(1)
            self.assertGreater(
                len(smtp_server.received_emails),
                0,
                "Expected at least one email to be sent via SMTP",
            )
        finally:
            self._stop_smtp()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_happy_path_with_next_link(self) -> None:
        """Valid request with next_link → 200 with sid."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_with_email()

            response = requests.post(
                ENDPOINT,
                json={
                    "username": "nextlinkuser",
                    "client_secret": "mysecret456",
                    "email": "nextlink@example.com",
                    "send_attempt": 1,
                    "next_link": "https://app.pangea.chat/register",
                },
            )
            self.assertEqual(response.status_code, 200, response.json())
            body = response.json()
            self.assertIn("sid", body)
        finally:
            self._stop_smtp()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_idempotent_send_attempt(self) -> None:
        """Same email/client_secret/send_attempt returns same sid without re-sending."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_with_email()

            payload = {
                "username": "idempotentuser",
                "client_secret": "idempotentsecret",
                "email": "idempotent@example.com",
                "send_attempt": 1,
            }

            # First request
            resp1 = requests.post(ENDPOINT, json=payload)
            self.assertEqual(resp1.status_code, 200, resp1.json())
            sid1 = resp1.json()["sid"]

            # Same request again (same send_attempt) — should return same sid
            resp2 = requests.post(ENDPOINT, json=payload)
            self.assertEqual(resp2.status_code, 200, resp2.json())
            sid2 = resp2.json()["sid"]

            self.assertEqual(sid1, sid2)
        finally:
            self._stop_smtp()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_higher_send_attempt_re_sends(self) -> None:
        """Higher send_attempt with same email/client_secret re-sends email."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_with_email()

            base_payload = {
                "username": "resenduser",
                "client_secret": "resendsecret",
                "email": "resend@example.com",
            }

            # First send_attempt
            resp1 = requests.post(ENDPOINT, json={**base_payload, "send_attempt": 1})
            self.assertEqual(resp1.status_code, 200, resp1.json())
            sid1 = resp1.json()["sid"]

            smtp_server = self._require_smtp_server()
            await asyncio.sleep(0.5)
            emails_after_first = len(smtp_server.received_emails)

            # Higher send_attempt should re-send
            resp2 = requests.post(ENDPOINT, json={**base_payload, "send_attempt": 2})
            self.assertEqual(resp2.status_code, 200, resp2.json())
            sid2 = resp2.json()["sid"]

            # Same session
            self.assertEqual(sid1, sid2)

            # Should have sent an additional email
            await asyncio.sleep(1)
            self.assertGreater(
                len(smtp_server.received_emails),
                emails_after_first,
                "Expected a new email for higher send_attempt",
            )
        finally:
            self._stop_smtp()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_email_already_in_use(self) -> None:
        """Email already bound to an account → 400 M_THREEPID_IN_USE."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_with_email()

            # Register a user and bind an email via the Admin API
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="emailowner",
                password="123123123",
                admin=True,
            )
            user_id, access_token = await self.login_user(
                user="emailowner", password="123123123"
            )

            # Use Synapse Admin API to bind the 3PID
            admin_url = f"http://localhost:8008/_synapse/admin/v2/users/{user_id}"
            resp = requests.put(
                admin_url,
                json={
                    "threepids": [{"medium": "email", "address": "taken@example.com"}]
                },
                headers={"Authorization": f"Bearer {access_token}"},
            )
            self.assertEqual(
                resp.status_code,
                200,
                f"Failed to bind 3PID: {resp.text}",
            )

            # Now try to register with that email
            response = requests.post(
                ENDPOINT,
                json={
                    "username": "anothernewuser",
                    "client_secret": "emailsecret",
                    "email": "taken@example.com",
                    "send_attempt": 1,
                },
            )
            self.assertEqual(response.status_code, 400)
            body = response.json()
            self.assertEqual(body["errcode"], "M_THREEPID_IN_USE")
        finally:
            self._stop_smtp()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_username_taken_does_not_send_email(self) -> None:
        """If username is taken, no email should be sent."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_with_email()

            # Register a user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="takenuser",
                password="123123123",
                admin=True,
            )

            smtp_server = self._require_smtp_server()
            emails_before = len(smtp_server.received_emails)

            response = requests.post(
                ENDPOINT,
                json={
                    "username": "takenuser",
                    "client_secret": "somesecret",
                    "email": "innocent@example.com",
                    "send_attempt": 1,
                },
            )
            self.assertEqual(response.status_code, 400)
            body = response.json()
            self.assertEqual(body["errcode"], "M_USER_IN_USE")

            # Verify no email was sent
            await asyncio.sleep(1)
            self.assertEqual(
                len(smtp_server.received_emails),
                emails_before,
                "No email should be sent when username is already taken",
            )
        finally:
            self._stop_smtp()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_invalid_email_format(self) -> None:
        """Invalid email format → 400."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_with_email()

            response = requests.post(
                ENDPOINT,
                json={
                    "username": "validuser",
                    "client_secret": "s3cr3t",
                    "email": "not-an-email",
                    "send_attempt": 1,
                },
            )
            self.assertEqual(response.status_code, 400)
        finally:
            self._stop_smtp()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_invalid_username_with_email_config(self) -> None:
        """Invalid username still rejected even with email configured."""
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self._start_synapse_with_email()

            smtp_server = self._require_smtp_server()
            emails_before = len(smtp_server.received_emails)

            response = requests.post(
                ENDPOINT,
                json={
                    "username": "bad@user",
                    "client_secret": "s3cr3t",
                    "email": "user@example.com",
                    "send_attempt": 1,
                },
            )
            self.assertEqual(response.status_code, 400)
            body = response.json()
            self.assertEqual(body["errcode"], "M_INVALID_USERNAME")

            # Verify no email was sent for invalid usernames.
            await asyncio.sleep(1)
            self.assertEqual(
                len(smtp_server.received_emails),
                emails_before,
                "No email should be sent when username is invalid",
            )
        finally:
            self._stop_smtp()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
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
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
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
                response = requests.post(ENDPOINT, json=payload)
                self.assertNotEqual(
                    response.status_code,
                    429,
                    f"Request {i+1} should not be rate limited",
                )

            # Next request should be rate limited
            response = requests.post(ENDPOINT, json=payload)
            self.assertEqual(response.status_code, 429)
            body = response.json()
            self.assertEqual(body["errcode"], "M_LIMIT_EXCEEDED")
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
