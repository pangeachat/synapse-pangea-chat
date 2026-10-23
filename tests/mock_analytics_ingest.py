"""A stand-in for admin-dash-api's ``POST /api/internal/matrix-events``.

Records every body it is sent, answers with a status the test sets, and
acknowledges a 200 by echoing ``delivered_through``, as the real ingest does
after its commit. ``on_post`` runs before the answer, so a test can change
something while a delivery is in flight.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, List, Optional, Tuple

INGEST_PATH = "/api/internal/matrix-events"


class _IngestServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], handler: Any, secret: str) -> None:
        super().__init__(address, handler)
        self.secret = secret
        self.status = 200
        self.lock = threading.Lock()
        # (status answered, body) per request, in arrival order.
        self.requests: List[Tuple[int, dict]] = []
        self.on_post: Optional[Callable[[dict], None]] = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        assert isinstance(self.server, _IngestServer)
        server = self.server
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path != INGEST_PATH:
            self._send(404, {"error": "not found"})
            return
        if self.headers.get("Authorization") != f"Bearer {server.secret}":
            self._send(401, {"error": "unauthorized"})
            return
        hook = server.on_post
        if hook is not None:
            hook(body)
        with server.lock:
            status = server.status
            server.requests.append((status, body))
        if status == 200:
            self._send(200, {"delivered_through": body["delivered_through"]})
        else:
            self._send(status, {"error": "unavailable"})

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MockIngest:
    def __init__(self, secret: str) -> None:
        self._server = _IngestServer(("127.0.0.1", 0), _Handler, secret)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}{INGEST_PATH}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def set_status(self, status: int) -> None:
        with self._server.lock:
            self._server.status = status

    def set_on_post(self, hook: Optional[Callable[[dict], None]]) -> None:
        self._server.on_post = hook

    def requests(self) -> List[Tuple[int, dict]]:
        with self._server.lock:
            return list(self._server.requests)

    def acknowledged(self) -> List[dict]:
        return [body for status, body in self.requests() if status == 200]
