"""Mock choreo moderation server for E2E tests.

Implements POST /choreo/moderate with the ModerationResult shape. Flags any
text containing the marker string "FLAGME"; everything else is clean. A bare
Bearer token is required, mirroring the real endpoint's has_matrix_account
gate (the mock accepts any non-empty token).
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, List

FLAG_MARKER = "FLAGME"


class _MockModerationHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        server: "MockModerationServer" = self.server  # type: ignore[assignment]
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
        flagged = FLAG_MARKER in text
        self._send(
            200,
            {
                "flagged": flagged,
                "categories": ["harassment"] if flagged else [],
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
        self._httpd = HTTPServer(("127.0.0.1", 0), _MockModerationHandler)
        self._httpd.seen_texts = []  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def seen_texts(self) -> List[str]:
        return self._httpd.seen_texts  # type: ignore[attr-defined]

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "MockModerationServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
