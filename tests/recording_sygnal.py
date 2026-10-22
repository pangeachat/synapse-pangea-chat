"""A push gateway for e2e tests that records what it receives."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple


class RecordingSygnal:
    """A push gateway that records, per pushkey, every event push it gets."""

    def __init__(self) -> None:
        stub = self
        self.pushes: List[Tuple[str, Dict[str, Any]]] = []
        self._lock = threading.Lock()

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                notification = json.loads(body)["notification"]
                # Badge-only updates carry no event.
                if notification.get("event_id"):
                    with stub._lock:
                        for device in notification["devices"]:
                            stub.pushes.append((device["pushkey"], notification))
                payload = b'{"rejected": []}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/_matrix/push/v1/notify"

    def received(self, pushkey: str) -> List[str]:
        return [n["event_id"] for n in self.notifications(pushkey)]

    def notifications(self, pushkey: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [n for key, n in self.pushes if key == pushkey]

    def wait_for(self, pushkey: str, count: int, timeout: float) -> List[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(self.received(pushkey)) < count:
            time.sleep(0.1)
        return self.received(pushkey)

    def start(self) -> "RecordingSygnal":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
