"""``/_synapse/client/pangea/v2/instructor_invitations`` — additional-instructor
invitations into an existing course (knock-with-code.instructions.md, "Codes,
share kit and existing courses").

``POST`` prepares one and sends no email: the engagement runner sends every
instructor-invitation email and follow-up through the v2 claim-reminder
endpoint with its own rendered copy. ``GET`` lists them for reporting. Server
admins only; answers never carry the address or a code.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict

from synapse.api.errors import Codes, SynapseError
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.logging.context import run_in_background
from twisted.web.resource import Resource

from synapse_pangea_chat.email_invite.course_claim_reminder import (
    _capture_exception,
    _text_field,
)
from synapse_pangea_chat.room_code.code_lookup import new_unique_code
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.instructor_access import joined_local_instructors

logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
ERRCODE_NO_ELIGIBLE_INSTRUCTOR = "ORG.PANGEA.NO_ELIGIBLE_INSTRUCTOR"

LIST_STATUSES = ("prepared", "provisioning", "completed", "revoked")
LIST_FIELDS = (
    "invitation_id",
    "room_id",
    "status",
    "claimant",
    "created_at_ms",
    "completed_at_ms",
    "delivery_outcome",
)


class InstructorInvitationAPI(Resource):
    isLeaf = True

    def __init__(self, api, config, claims, invitations):
        super().__init__()
        self.api, self.config = api, config
        self.claims, self.invitations = claims, invitations

    def render_GET(self, request):
        run_in_background(self.handle, request, "GET")
        return server.NOT_DONE_YET

    def render_POST(self, request):
        run_in_background(self.handle, request, "POST")
        return server.NOT_DONE_YET

    async def handle(self, request, method):
        try:
            requester = await self.api._hs.get_auth().get_user_by_req(request)
            operator = requester.user.to_string()
            if not await self.api.is_user_admin(operator):
                raise SynapseError(403, "Admin access required")
            if method == "GET":
                result = await self.list(request)
            elif method == "POST":
                body = await extract_body_json(request)
                if not isinstance(body, dict):
                    raise SynapseError(400, "Expected a JSON object")
                result = await self.prepare(operator, body)
            else:
                raise SynapseError(405, "Method not allowed")
            respond_with_json(request, 200, result, send_cors=True)
        except SynapseError as error:
            if error.code >= 500:
                _capture_exception(error)
            respond_with_json(
                request, error.code, error.error_dict(None), send_cors=True
            )
        except Exception as error:
            logger.error(
                "Instructor invitation request failed: %s", type(error).__name__
            )
            _capture_exception(
                RuntimeError(
                    "Instructor invitation request failed: " + type(error).__name__
                )
            )
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    async def prepare(self, operator: str, body: Dict[str, Any]) -> Dict[str, Any]:
        key = _text_field(body, "request_key", 200)
        room_id = _text_field(body, "room_id", 255)
        email = _text_field(body, "teacher_email", 320)
        if not key or not email or not EMAIL_PATTERN.fullmatch(email):
            raise SynapseError(400, "Valid request_key and teacher_email are required")
        if not room_id or not room_id.startswith("!"):
            raise SynapseError(400, "Missing or invalid room_id")
        state = await self.api.get_room_state(room_id)
        create = state.get(("m.room.create", ""))
        if create is None:
            raise SynapseError(404, "Room not found", Codes.NOT_FOUND)
        plan = state.get(("pangea.course_plan", ""))
        if create.content.get("type") != "m.space" or plan is None:
            raise SynapseError(400, "room_id must be a course space")
        if not joined_local_instructors(self.api, state):
            # Existing-room invitations are sent by a currently eligible
            # joined administrator; a room without one needs explicit recovery
            # before any invitation into it can be honoured.
            raise SynapseError(
                409, "no eligible instructor", ERRCODE_NO_ELIGIBLE_INSTRUCTOR
            )

        name = state.get(("m.room.name", ""))
        spec = {
            "kind": "instructor",
            "title": _content_text(name, "name"),
            "course_plan_id": _content_text(plan, "uuid"),
            "target_language": _content_text(plan, "l2"),
        }
        code = await new_unique_code(self.api._hs.get_datastores().main, self.claims)
        if code is None:
            raise SynapseError(503, "Unable to allocate a code")
        ident, _ = await self.invitations.prepare(
            operator,
            key,
            spec,
            email,
            code,
            self.api._hs.get_clock().time_msec(),
            room_id=room_id,
        )
        return await self.invitations.status(ident)

    async def list(self, request) -> Dict[str, Any]:
        args: Dict[bytes, list] = dict(request.args or {})
        values = args.get(b"status") or [b"prepared"]
        status = values[0].decode("utf-8", errors="replace")
        if len(values) != 1 or status not in LIST_STATUSES:
            raise SynapseError(400, "status must be one of " + ", ".join(LIST_STATUSES))
        rows = await self.invitations.list_instructor(status)
        return {
            "invitations": [
                {
                    **{field: row[field] for field in LIST_FIELDS},
                    "deliveries": len(row["deliveries"]),
                }
                for row in rows
            ]
        }


def _content_text(event, key: str) -> str:
    value = event.content.get(key) if event is not None else None
    return value if isinstance(value, str) else ""
