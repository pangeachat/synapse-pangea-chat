"""A local SMTP server that accepts every message and keeps it, for e2e tests.

The test homeserver is pointed at it through its ``email`` config, so a test
can read back what Synapse actually sent: the rendered templates, through
Synapse's own mail path, rather than what a handler meant to send.
"""

from __future__ import annotations

import email
import email.policy
import socketserver
import threading
import time
from email.message import Message
from typing import List, Optional


class _Handler(socketserver.StreamRequestHandler):
    def _reply(self, line: str) -> None:
        self.wfile.write(f"{line}\r\n".encode())
        self.wfile.flush()

    def handle(self) -> None:
        sink: SmtpSink = self.server.sink  # type: ignore[attr-defined]
        recipients: List[str] = []
        self._reply("220 sink ESMTP")
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            command = raw.decode(errors="replace").strip()
            verb = command.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                self._reply("250 sink")
            elif verb == "MAIL":
                recipients = []
                self._reply("250 OK")
            elif verb == "RCPT":
                address = command.split(":", 1)[1].strip().strip("<>")
                recipients.append(address)
                self._reply("250 OK")
            elif verb == "DATA":
                self._reply("354 End data with <CR><LF>.<CR><LF>")
                lines = []
                while True:
                    line = self.rfile.readline()
                    if line in (b".\r\n", b".\n", b""):
                        break
                    if line.startswith(b".."):
                        line = line[1:]
                    lines.append(line)
                message = email.message_from_bytes(
                    b"".join(lines), policy=email.policy.default
                )
                sink._add(recipients, message)
                self._reply("250 OK")
            elif verb == "QUIT":
                self._reply("221 Bye")
                return
            else:
                self._reply("250 OK")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class SmtpSink:
    def __init__(self) -> None:
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.sink = self  # type: ignore[attr-defined]
        self.port: int = self._server.server_address[1]
        self._lock = threading.Lock()
        self._messages: List[tuple[List[str], Message]] = []
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "SmtpSink":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _add(self, recipients: List[str], message: Message) -> None:
        with self._lock:
            self._messages.append((recipients, message))

    def synapse_email_config(self) -> dict:
        """The homeserver ``email`` block that sends through this sink."""
        return {
            "smtp_host": "127.0.0.1",
            "smtp_port": self.port,
            "notif_from": "Pangea Chat <support@example.com>",
            "app_name": "Pangea Chat",
            "enable_tls": False,
            "require_transport_security": False,
        }

    def wait_for(
        self, recipient: str, subject_contains: str, timeout: float = 10.0
    ) -> Optional[Message]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                for recipients, message in self._messages:
                    if recipient in recipients and subject_contains in str(
                        message["Subject"]
                    ):
                        return message
            time.sleep(0.1)
        return None

    def messages_to(self, recipient: str) -> List[Message]:
        with self._lock:
            return [m for r, m in self._messages if recipient in r]


def body_text(message: Message) -> str:
    """Every text part of a message, decoded and joined."""
    parts = []
    for part in message.walk():
        if part.get_content_maintype() == "text":
            parts.append(part.get_content())
    return "\n".join(parts)
