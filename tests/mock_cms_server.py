"""Mock CMS HTTP server for E2E tests.

Implements Payload-compatible routes for process-token-feedback-logs
and user-exports, backed by in-memory storage.
"""

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

EXPECTED_AUTH_SCHEME = "service-users API-Key "


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

    def _read_json_body(self) -> Dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        return json.loads(raw_body.decode("utf-8"))

    def _require_service_user_auth(self) -> bool:
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith(EXPECTED_AUTH_SCHEME):
            self._send_json(401, {"error": "Unauthorized"})
            return False
        return True

    # --- routing ---

    def do_GET(self) -> None:
        if not self._require_service_user_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/process-token-feedback-logs":
            self._handle_get_feedback_logs(parsed.query)
        elif parsed.path == "/api/matrix-users":
            self._handle_get_matrix_users(parsed.query)
        else:
            self._send_json(404, {"error": "Not found"})

    def do_DELETE(self) -> None:
        if not self._require_service_user_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/process-token-feedback-logs":
            self._handle_delete_feedback_logs(parsed.query)
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if not self._require_service_user_auth():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/user-exports":
            self._handle_create_export()
        else:
            self._send_json(404, {"error": "Not found"})

    def do_PATCH(self) -> None:
        if not self._require_service_user_auth():
            return
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

    def _handle_get_matrix_users(self, query_string: str) -> None:
        qs = parse_qs(query_string)
        username = qs.get("where[username][equals]", [None])[0]

        state: "_MockCmsState" = self.server._state  # type: ignore[attr-defined]
        docs = state.get_matrix_users(username)
        self._send_json(200, {"docs": docs})

    # --- user-exports handlers ---

    def _handle_create_export(self) -> None:
        state: "_MockCmsState" = self.server._state  # type: ignore[attr-defined]
        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b""

        if not content_type.startswith("multipart/form-data"):
            self._send_json(400, {"errors": [{"message": "No files were uploaded."}]})
            return

        parsed = _parse_multipart_export_body(raw_body)
        if parsed is None:
            self._send_json(400, {"errors": [{"message": "No files were uploaded."}]})
            return

        body = parsed["payload"]
        user_id = body.get("user")
        if not isinstance(user_id, str) or not state.has_matrix_user_id(user_id):
            self._send_json(400, {"error": "Invalid matrix user relationship"})
            return

        export_doc = state.create_export(
            body,
            filename=parsed["filename"],
            mime_type="application/zip",
        )
        self._send_json(200, {"doc": export_doc})

    def _handle_update_export(self, path: str) -> None:
        state: "_MockCmsState" = self.server._state  # type: ignore[attr-defined]
        export_id = path.split("/")[-1]
        content_type = self.headers.get("Content-Type", "")
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b""

        if content_type.startswith("application/json"):
            doc = state.update_export_json(export_id, json.loads(raw_body or b"{}"))
        elif content_type.startswith("multipart/form-data"):
            doc = state.update_export_multipart(export_id, raw_body)
        else:
            self._send_json(400, {"error": "Unsupported Content-Type"})
            return

        if doc is None:
            self._send_json(404, {"error": "Export record not found"})
            return

        self._send_json(200, {"doc": doc})


class _MockCmsState:
    """Thread-safe in-memory store for feedback logs, matrix users, and exports."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._logs: Dict[str, List[Dict[str, Any]]] = {}
        self._matrix_users_by_username: Dict[str, Dict[str, Any]] = {}
        self._matrix_users_by_id: Dict[str, Dict[str, Any]] = {}
        self._exports_by_id: Dict[str, Dict[str, Any]] = {}

    def seed(self, user_id: str, logs: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._logs.setdefault(user_id, []).extend(logs)

    def seed_matrix_user(self, username: str) -> Dict[str, Any]:
        with self._lock:
            existing = self._matrix_users_by_username.get(username)
            if existing is not None:
                return dict(existing)

            doc = {"id": str(uuid.uuid4()), "username": username}
            self._matrix_users_by_username[username] = doc
            self._matrix_users_by_id[doc["id"]] = doc
            return dict(doc)

    def get_matrix_users(self, username: Optional[str]) -> List[Dict[str, Any]]:
        with self._lock:
            if username is None:
                return []
            doc = self._matrix_users_by_username.get(username)
            return [dict(doc)] if doc is not None else []

    def has_matrix_user_id(self, matrix_user_id: str) -> bool:
        with self._lock:
            return matrix_user_id in self._matrix_users_by_id

    def create_export(
        self,
        body: Dict[str, Any],
        *,
        filename: Optional[str] = None,
        mime_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            export_id = str(uuid.uuid4())
            doc = {
                "id": export_id,
                "user": body["user"],
                "status": body.get("status", "pending"),
                "requestedAt": body.get("requestedAt"),
            }
            if filename is not None:
                doc["filename"] = filename
            if mime_type is not None:
                doc["mimeType"] = mime_type
            self._exports_by_id[export_id] = doc
            return dict(doc)

    def update_export_json(
        self, export_id: str, body: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            doc = self._exports_by_id.get(export_id)
            if doc is None:
                return None
            doc.update(body)
            return dict(doc)

    def update_export_multipart(
        self, export_id: str, raw_body: bytes
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            doc = self._exports_by_id.get(export_id)
            if doc is None:
                return None

            decoded_body = raw_body.decode("utf-8", errors="replace")
            if 'name="status"' in decoded_body and "complete" in decoded_body:
                doc["status"] = "complete"

            filename_marker = 'filename="'
            filename_start = decoded_body.find(filename_marker)
            if filename_start != -1:
                start = filename_start + len(filename_marker)
                end = decoded_body.find('"', start)
                doc["filename"] = decoded_body[start:end]
                doc["mimeType"] = "application/zip"

            return dict(doc)

    def get_exports_for_matrix_user_id(
        self, matrix_user_id: str
    ) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                dict(doc)
                for doc in self._exports_by_id.values()
                if doc.get("user") == matrix_user_id
            ]

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

    def seed_matrix_user(self, username: str) -> Dict[str, Any]:
        return self._state.seed_matrix_user(username)

    def get_exports_for_matrix_user_id(
        self, matrix_user_id: str
    ) -> List[Dict[str, Any]]:
        return self._state.get_exports_for_matrix_user_id(matrix_user_id)

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
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None


def _parse_multipart_export_body(raw_body: bytes) -> Optional[Dict[str, Any]]:
    decoded_body = raw_body.decode("utf-8", errors="replace")

    payload_match = re.search(
        r'name="_payload"\r\n\r\n(.*?)\r\n--', decoded_body, re.DOTALL
    )
    filename_match = re.search(r'filename="([^"]+)"', decoded_body)

    if payload_match is None or filename_match is None:
        return None

    return {
        "payload": json.loads(payload_match.group(1)),
        "filename": filename_match.group(1),
    }
