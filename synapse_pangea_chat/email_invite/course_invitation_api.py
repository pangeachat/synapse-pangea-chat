"""Versioned operator API. Transport acceptance is not inbox delivery."""
from __future__ import annotations

import logging
import re

from synapse.api.errors import SynapseError
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.logging.context import run_in_background
from twisted.mail.smtp import SMTPDeliveryError
from twisted.web.resource import Resource

from synapse_pangea_chat.email_invite.build_join_url import build_join_url
from synapse_pangea_chat.email_invite.course_claim_reminder import (
    _capture_exception,
    _text_field,
)
from synapse_pangea_chat.room_code.code_lookup import new_unique_code
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json

logger = logging.getLogger(__name__)


class CourseInvitationAPI(Resource):
    def __init__(
        self, api, config, claims, invitations, mailer, mode, invitation_id=None
    ):
        super().__init__()
        self.api, self.config, self.claims = api, config, claims
        self.invitations, self.mailer, self.mode = invitations, mailer, mode
        self.invitation_id = invitation_id
        self.isLeaf = mode != "status" or invitation_id is not None

    def getChild(self, path, request):
        return CourseInvitationAPI(
            self.api,
            self.config,
            self.claims,
            self.invitations,
            self.mailer,
            "status",
            path.decode("ascii", errors="replace"),
        )

    def render_GET(self, request):
        run_in_background(self.handle, request, "GET")
        return server.NOT_DONE_YET

    def render_POST(self, request):
        run_in_background(self.handle, request, "POST")
        return server.NOT_DONE_YET

    def render_DELETE(self, request):
        run_in_background(self.handle, request, "DELETE")
        return server.NOT_DONE_YET

    async def handle(self, request, method):
        try:
            requester = await self.api._hs.get_auth().get_user_by_req(request)
            operator = requester.user.to_string()
            if not await self.api.is_user_admin(operator):
                raise SynapseError(403, "Admin access required")
            if self.mode == "status":
                if method == "DELETE":
                    await self.invitations.revoke(self.invitation_id)
                elif method != "GET":
                    raise SynapseError(405, "Method not allowed")
                result = await (
                    self.claims.status(self.invitation_id)
                    if self.invitation_id and self.invitation_id.startswith("!")
                    else self.invitations.status(self.invitation_id)
                )
            else:
                if method != "POST":
                    raise SynapseError(405, "Method not allowed")
                body = await extract_body_json(request)
                if not isinstance(body, dict):
                    raise SynapseError(400, "Expected a JSON object")
                result = await (
                    self.prepare(operator, body)
                    if self.mode == "prepare"
                    else self.remind(body)
                )
            respond_with_json(request, 200, result, send_cors=True)
        except SynapseError as error:
            if error.code >= 500:
                _capture_exception(error)
            respond_with_json(
                request, error.code, error.error_dict(None), send_cors=True
            )
        except Exception as error:
            logger.error("Course invitation request failed: %s", type(error).__name__)
            _capture_exception(
                RuntimeError(
                    "Course invitation request failed: " + type(error).__name__
                )
            )
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )

    async def code(self):
        code = await new_unique_code(self.api._hs.get_datastores().main, self.claims)
        if code is None:
            raise SynapseError(503, "Unable to allocate a code")
        return code

    async def prepare(self, operator, body):
        key = _text_field(body, "request_key", 200)
        email = _text_field(body, "teacher_email", 320)
        spec = {}
        for field, limit in (
            ("title", 255),
            ("course_plan_id", 200),
            ("target_language", 35),
        ):
            value = _text_field(body, field, limit)
            if value is None:
                raise SynapseError(400, "Missing or invalid " + field)
            spec[field] = value
        if not key or not email or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise SynapseError(400, "Valid request_key and teacher_email are required")
        if not re.fullmatch(
            r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*", spec["target_language"]
        ):
            raise SynapseError(400, "target_language must be a language code")
        for field, limit in (
            ("description", 10000),
            ("image_url", 2048),
            ("request_summary", 5000),
        ):
            value = body.get(field, "")
            if not isinstance(value, str) or len(value) > limit:
                raise SynapseError(400, "Invalid " + field)
            spec[field] = value.strip()
        code = await self.code()
        ident, created = await self.invitations.prepare(
            operator, key, spec, email, code, self.api._hs.get_clock().time_msec()
        )
        if created:
            await self.send(ident, code, email, spec=spec)
        return await self.invitations.status(ident)

    async def remind(self, body):
        ident = _text_field(body, "invitation_id", 100)
        rendered = {
            name: _text_field(body, name, limit)
            for name, limit in (("subject", 200), ("body", 5000), ("cta_label", 60))
        }
        if not ident or not all(rendered.values()):
            raise SynapseError(
                400, "invitation_id, subject, body and cta_label are required"
            )
        invitation = await self.invitations.get(ident)
        if not invitation:
            raise SynapseError(404, "Invitation not found")
        if invitation["status"] != "prepared" or not invitation["requested_email"]:
            raise SynapseError(
                409,
                "Recover partial claims before sending; completed or revoked invitations cannot be reminded",
            )
        await self.send(
            ident, await self.code(), invitation["requested_email"], rendered=rendered
        )
        return await self.invitations.status(ident)

    async def send(self, ident, code, email, spec=None, rendered=None):
        clock = self.api._hs.get_clock()
        attempt = await self.invitations.begin_delivery(ident, code, clock.time_msec())
        try:
            url = build_join_url(self.config.app_base_url, code)
            if spec is not None:
                await self.mailer.send_course_ready(
                    email_address=email,
                    course_title=spec["title"],
                    course_description=spec["description"],
                    request_summary=spec["request_summary"] or None,
                    claim_url=url,
                    claim_code=code,
                )
            else:
                await self.mailer.send_course_reminder(
                    email_address=email, claim_url=url, claim_code=code, **rendered
                )
        except Exception as error:
            # A transport exception may follow acceptance. Keep the link valid
            # and retain uncertain outcome; never claim it was not delivered.
            outcome = (
                "failed"
                if isinstance(error, SMTPDeliveryError) and 400 <= error.code < 600
                else "uncertain"
            )
            logger.error(
                "Invitation %s mail attempt %s outcome %s: %s",
                ident,
                attempt,
                outcome,
                type(error).__name__,
            )
            _capture_exception(
                RuntimeError("Course invitation mail failed: " + type(error).__name__)
            )
            await self.invitations.finish_delivery(attempt, outcome, clock.time_msec())
            return
        await self.invitations.finish_delivery(attempt, "accepted", clock.time_msec())
