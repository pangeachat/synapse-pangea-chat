"""Mock choreo moderation server for E2E tests.

Implements POST /choreo/moderate with the ModerationResult shape. Flags any
text containing the marker string "FLAGME" as harassment and any text
containing "PRESERVEME" as a self-harm disclosure - the one verdict that must
never be redacted; everything else is clean. A bare
Bearer token is required, mirroring the real endpoint's has_matrix_account
gate (the mock accepts any non-empty token).
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, List, Tuple

FLAG_MARKER = "FLAGME"
#: Flagged as a self-harm disclosure, which is the one verdict whose
#: disposition is to LEAVE THE MESSAGE STANDING.
PRESERVE_MARKER = "PRESERVEME"


class _RecordingHTTPServer(ThreadingHTTPServer):
    """An HTTP server that owns the list of texts the handler has seen.

    The handler reaches its server through `self.server`, which the stdlib
    types as `socketserver.BaseServer`, so bolting the attribute onto a plain
    `HTTPServer` needed a type suppression at every use. Declaring it on a
    subclass needs none.

    Threading, so `delay_seconds` holds ONE request rather than the whole
    server: a single-threaded server would serialise the concurrency the
    worker pool exists to produce, and a test of a queue that cannot be
    filled tests nothing.
    """

    daemon_threads = True

    def __init__(self, address: Tuple[str, int], handler: Any) -> None:
        super().__init__(address, handler)
        self.seen_texts: List[str] = []
        # Held responses, so a test can have work genuinely in flight when it
        # stops the homeserver. Without it a drain has nothing to drain and a
        # test that claims to exercise one proves nothing.
        self.delay_seconds = 0.0


class _MockModerationHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        # Runtime-checked narrowing rather than a suppression: `self.server`
        # is typed as the base server, and this handler is only ever attached
        # to the subclass above.
        assert isinstance(self.server, _RecordingHTTPServer)
        server = self.server
        if self.path != "/choreo/moderate":
            self._send(404, {"detail": "not found"})
            return
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not auth[len("Bearer ") :].strip():
            self._send(401, {"detail": "Could not validate Matrix token"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        text = body.get("text", "")
        server.seen_texts.append(text)
        if server.delay_seconds:
            time.sleep(server.delay_seconds)
        preserve = PRESERVE_MARKER in text
        flagged = preserve or FLAG_MARKER in text
        if preserve:
            categories = ["self-harm/intent"]
        elif flagged:
            categories = ["harassment"]
        else:
            categories = []
        self._send(
            200,
            {
                "flagged": flagged,
                "categories": categories,
                "evaluated": True,
            },
        )

    def _send(self, code: int, payload: Any) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class MockModerationServer:
    def __init__(self) -> None:
        self._httpd = _RecordingHTTPServer(("127.0.0.1", 0), _MockModerationHandler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def seen_texts(self) -> List[str]:
        return self._httpd.seen_texts

    def hold_responses_for(self, seconds: float) -> None:
        """Make every later response take ``seconds``, so a test can stop the
        homeserver with checks genuinely in flight."""
        self._httpd.delay_seconds = seconds

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        # `server_address` is only `str` for AF_INET/AF_INET6; the annotation
        # admits bytes, and formatting bytes would yield a b'...' host.
        hostname = (
            host.decode("ascii") if isinstance(host, (bytes, bytearray)) else str(host)
        )
        return f"http://{hostname}:{port}"

    def start(self) -> "MockModerationServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
