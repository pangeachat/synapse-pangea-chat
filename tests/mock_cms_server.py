"""Mock CMS HTTP server for E2E tests.

Implements Payload-compatible routes for process-token-feedback-logs
and user-exports, backed by in-memory storage.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse


class _MockCmsHandler(BaseHTTPRequestHandler):
    """Request handler backed by the server's in-memory state."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # suppress request logging in test output

    def _send_json(self, code: int, body: Any) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # --- routing ---

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/process-token-feedback-logs":
            self._handle_get_feedback_logs(parsed.query)
        else:
            self._send_json(404, {"error": "Not found"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/process-token-feedback-logs":
            self._handle_delete_feedback_logs(parsed.query)
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/user-exports":
            self._handle_create_export()
        else:
            self._send_json(404, {"error": "Not found"})

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/user-exports/"):
            self._handle_update_export(parsed.path)
        else:
            self._send_json(404, {"error": "Not found"})

    # --- feedback-logs handlers ---

    def _handle_get_feedback_logs(self, query_string: str) -> None:
        qs = parse_qs(query_string)
        user_id = qs.get("where[req.user_id][equals]", [None])[0]
        page = int(qs.get("page", ["1"])[0])
        limit = int(qs.get("limit", ["100"])[0])

        state: "_MockCmsState" = self.server._state  # type: ignore[attr-defined]
        all_logs = state.get_logs(user_id) if user_id else []

        start = (page - 1) * limit
        end = start + limit
        page_docs = all_logs[start:end]
        has_next = end < len(all_logs)

        self._send_json(
            200,
            {
                "docs": page_docs,
                "totalDocs": len(all_logs),
                "hasNextPage": has_next,
                "nextPage": page + 1 if has_next else None,
                "page": page,
                "limit": limit,
            },
        )

    def _handle_delete_feedback_logs(self, query_string: str) -> None:
        qs = parse_qs(query_string)
        user_id = qs.get("where[req.user_id][equals]", [None])[0]

        state: "_MockCmsState" = self.server._state  # type: ignore[attr-defined]
        deleted = state.delete_logs(user_id) if user_id else []

        self._send_json(200, {"docs": deleted})

    # --- user-exports handlers ---

    def _handle_create_export(self) -> None:
        self._send_json(200, {"doc": {"id": "mock-export-id"}})

    def _handle_update_export(self, path: str) -> None:
        self._send_json(200, {"doc": {"id": path.split("/")[-1]}})


class _MockCmsState:
    """Thread-safe in-memory store for feedback logs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._logs: Dict[str, List[Dict[str, Any]]] = {}

    def seed(self, user_id: str, logs: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._logs.setdefault(user_id, []).extend(logs)

    def get_logs(self, user_id: Optional[str]) -> List[Dict[str, Any]]:
        with self._lock:
            if user_id is None:
                return []
            return list(self._logs.get(user_id, []))

    def delete_logs(self, user_id: Optional[str]) -> List[Dict[str, Any]]:
        with self._lock:
            if user_id is None:
                return []
            return self._logs.pop(user_id, [])

    def get_remaining_logs(self, user_id: str) -> List[Dict[str, Any]]:
        return self.get_logs(user_id)


class MockCmsServer:
    """Start/stop a mock CMS HTTP server on a random port."""

    def __init__(self) -> None:
        self._state = _MockCmsState()
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def seed_feedback_logs(self, user_id: str, logs: List[Dict[str, Any]]) -> None:
        self._state.seed(user_id, logs)

    def get_remaining_logs(self, user_id: str) -> List[Dict[str, Any]]:
        return self._state.get_remaining_logs(user_id)

    def start(self) -> str:
        """Start the server and return its base URL (e.g. http://127.0.0.1:PORT)."""
        self._server = HTTPServer(("127.0.0.1", 0), _MockCmsHandler)
        self._server._state = self._state  # type: ignore[attr-defined]
        port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=5)
