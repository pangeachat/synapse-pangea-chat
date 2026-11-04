"""Mock CMS HTTP server for E2E tests.

Implements Payload-compatible routes for matrix-users, user-exports,
activities-v2 (looked up by activity id) and quest-plans, backed by
in-memory storage.
"""

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
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
        parsed = urlparse(self.path)
        if not self._require_service_user_auth():
            return
        if parsed.path == "/api/matrix-users":
            self._handle_get_matrix_users(parsed.query)
        elif parsed.path == "/api/activities-v2":
            self._handle_get_activities(parsed.query)
        elif parsed.path.startswith("/api/quest-plans/"):
            self._handle_get_quest(parsed.path.split("/")[-1])
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

    def _handle_get_matrix_users(self, query_string: str) -> None:
        qs = parse_qs(query_string)
        username = qs.get("where[username][equals]", [None])[0]

        state: "_MockCmsState" = self.server._state  # type: ignore[attr-defined]
        docs = state.get_matrix_users(username)
        self._send_json(200, {"docs": docs})

    # --- activities-v2 and quest-plans handlers ---

    def _handle_get_activities(self, query_string: str) -> None:
        """Answers the lookup of one activity's canonical row by its id."""
        qs = parse_qs(query_string)
        activity_id = qs.get("where[res.plan.activity_id][equals]", [None])[0]
        state: "_MockCmsState" = self.server._state  # type: ignore[attr-defined]
        status, delay_seconds = state.activity_fault(activity_id)
        if delay_seconds:
            time.sleep(delay_seconds)
        if status is not None:
            self._send_json(status, {"errors": [{"message": "CMS fault"}]})
            return
        self._send_json(200, {"docs": state.get_activities(activity_id)})

    def _handle_get_quest(self, quest_id: str) -> None:
        state: "_MockCmsState" = self.server._state  # type: ignore[attr-defined]
        quest = state.get_quest(quest_id)
        if quest is None:
            self._send_json(404, {"errors": [{"message": "Not found"}]})
        else:
            self._send_json(200, quest)

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
    """Thread-safe in-memory store for matrix users and exports."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._matrix_users_by_username: Dict[str, Dict[str, Any]] = {}
        self._matrix_users_by_id: Dict[str, Dict[str, Any]] = {}
        self._exports_by_id: Dict[str, Dict[str, Any]] = {}
        self._activities_by_id: Dict[str, List[Dict[str, Any]]] = {}
        self._activity_faults: Dict[str, Tuple[Optional[int], float]] = {}
        self._quests_by_id: Dict[str, Dict[str, Any]] = {}

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

    def seed_activity(self, doc: Dict[str, Any]) -> None:
        """Adds an ``activities-v2`` row; a second row for one activity id is
        kept, so a lookup can answer two."""
        activity_id = doc["res"]["plan"]["activity_id"]
        with self._lock:
            self._activities_by_id.setdefault(activity_id, []).append(dict(doc))

    def get_activities(self, activity_id: Optional[str]) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                dict(doc) for doc in self._activities_by_id.get(activity_id or "", [])
            ]

    def set_activity_fault(
        self, activity_id: str, status: Optional[int], delay_seconds: float
    ) -> None:
        with self._lock:
            self._activity_faults[activity_id] = (status, delay_seconds)

    def activity_fault(self, activity_id: Optional[str]) -> Tuple[Optional[int], float]:
        with self._lock:
            return self._activity_faults.get(activity_id or "", (None, 0.0))

    def seed_quest(self, quest: Dict[str, Any]) -> None:
        with self._lock:
            self._quests_by_id[quest["id"]] = dict(quest)

    def get_quest(self, quest_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            quest = self._quests_by_id.get(quest_id)
            return dict(quest) if quest is not None else None

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


class _CmsHttpServer(ThreadingHTTPServer):
    # A class tapping at once opens many connections together; the default
    # backlog of 5 resets the rest, which a real CMS would not.
    request_queue_size = 64


class MockCmsServer:
    """Start/stop a mock CMS HTTP server on a random port."""

    def __init__(self) -> None:
        self._state = _MockCmsState()
        self._server: Optional[_CmsHttpServer] = None
        self._thread: Optional[threading.Thread] = None

    def seed_matrix_user(self, username: str) -> Dict[str, Any]:
        return self._state.seed_matrix_user(username)

    def get_exports_for_matrix_user_id(
        self, matrix_user_id: str
    ) -> List[Dict[str, Any]]:
        return self._state.get_exports_for_matrix_user_id(matrix_user_id)

    def seed_activity(self, doc: Dict[str, Any]) -> None:
        self._state.seed_activity(doc)

    def seed_quest(self, quest: Dict[str, Any]) -> None:
        self._state.seed_quest(quest)

    def set_activity_fault(
        self,
        activity_id: str,
        *,
        status: Optional[int] = None,
        delay_seconds: float = 0.0,
    ) -> None:
        """Answer ``status`` for this activity's lookup, after waiting
        ``delay_seconds``: a CMS that is down or slow."""
        self._state.set_activity_fault(activity_id, status, delay_seconds)

    def start(self) -> str:
        """Start the server and return its base URL (e.g. http://127.0.0.1:PORT)."""
        # Threaded, so a request held open (a slow CMS) never stalls the rest.
        self._server = _CmsHttpServer(("127.0.0.1", 0), _MockCmsHandler)
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
    line_end = raw_body.find(b"\r\n")
    if line_end == -1:
        return None

    boundary = raw_body[:line_end]
    if not boundary.startswith(b"--"):
        return None

    payload: Optional[Dict[str, Any]] = None
    filename: Optional[str] = None

    for part in raw_body.split(boundary):
        stripped_part = part.strip()
        if not stripped_part or stripped_part == b"--":
            continue

        if stripped_part.startswith(b"--"):
            stripped_part = stripped_part[2:].lstrip(b"\r\n")
            if not stripped_part:
                continue

        headers, separator, body = stripped_part.partition(b"\r\n\r\n")
        if not separator:
            continue

        content_disposition: Optional[str] = None
        for header_line in headers.split(b"\r\n"):
            try:
                decoded_header = header_line.decode("utf-8")
            except UnicodeDecodeError:
                decoded_header = header_line.decode("utf-8", errors="replace")

            if decoded_header.lower().startswith("content-disposition:"):
                content_disposition = decoded_header
                break

        if content_disposition is None:
            continue

        body = body.removesuffix(b"\r\n")

        if 'name="_payload"' in content_disposition:
            payload = json.loads(body.decode("utf-8"))

        if 'filename="' in content_disposition:
            filename_start = content_disposition.find('filename="') + len('filename="')
            filename_end = content_disposition.find('"', filename_start)
            if filename_end != -1:
                filename = content_disposition[filename_start:filename_end]

    if payload is None or filename is None:
        return None

    return {"payload": payload, "filename": filename}
