"""POST /_synapse/client/pangea/v1/send_course_claim_reminder

Emails the requesting address of a course not yet claimed a reminder carrying a
new claim link: another single-use admin code for the same course, whose
fingerprint joins the course's claim record (knock-with-code.instructions.md,
"A reminder carries a new claim link"; create-course-space.instructions.md,
"Claim reminders").

Server admins only. The caller renders the message; the answer says only
whether it was sent, never the code or the link.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from synapse.api.errors import (
    AuthError,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from twisted.web.resource import Resource

from synapse_pangea_chat.email_invite.build_join_url import build_join_url
from synapse_pangea_chat.email_invite.course_claim_emails import (
    CourseClaimMailer,
    reminder_paragraphs,
)
from synapse_pangea_chat.email_invite.course_claims import CourseClaimStore
from synapse_pangea_chat.room_code.code_lookup import new_unique_code
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

try:
    import sentry_sdk  # type: ignore[import-not-found]
# silent-ok: sentry-sdk is an optional Synapse extra; without it captures are no-ops (below)
except ImportError:
    sentry_sdk = None

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.email_invite.course_claim_reminder"
)

ERRCODE_NO_CLAIM_RECORD = "ORG.PANGEA.NO_CLAIM_RECORD"
ERRCODE_COURSE_CLAIMED = "ORG.PANGEA.COURSE_CLAIMED"
ERRCODE_NO_REQUESTING_ADDRESS = "ORG.PANGEA.NO_REQUESTING_ADDRESS"

MAX_SUBJECT_LENGTH = 200
MAX_BODY_LENGTH = 5000
MAX_CTA_LABEL_LENGTH = 60


def _capture_exception(e: Exception) -> None:
    # Failures here are answered, so they never reach Synapse's request-level
    # capture.
    if sentry_sdk is not None:
        sentry_sdk.capture_exception(e)


def _text_field(body: dict[str, Any], name: str, max_length: int) -> str | None:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        return None
    return value.strip()


class SendCourseClaimReminder(Resource):
    isLeaf = True

    def __init__(
        self,
        api: ModuleApi,
        config: "PangeaChatConfig",
        claim_store: CourseClaimStore,
        mailer: CourseClaimMailer,
    ):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = api._hs.get_auth()
        self._datastores = api._hs.get_datastores()
        self._clock = api._hs.get_clock()
        self._claim_store = claim_store
        self._mailer = mailer

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest) -> None:
        try:
            requester = await self._auth.get_user_by_req(request)
            if not await self._api.is_user_admin(requester.user.to_string()):
                respond_with_json(
                    request, 403, {"error": "Admin access required"}, send_cors=True
                )
                return

            body = await extract_body_json(request)
            if not isinstance(body, dict):
                respond_with_json(
                    request,
                    400,
                    {"error": "Invalid JSON in request body"},
                    send_cors=True,
                )
                return
            room_id = body.get("room_id")
            subject = _text_field(body, "subject", MAX_SUBJECT_LENGTH)
            text = _text_field(body, "body", MAX_BODY_LENGTH)
            cta_label = _text_field(body, "cta_label", MAX_CTA_LABEL_LENGTH)
            if not isinstance(room_id, str) or not room_id.strip():
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing or empty 'room_id'"},
                    send_cors=True,
                )
                return
            if subject is None or text is None or cta_label is None:
                respond_with_json(
                    request,
                    400,
                    {
                        "error": "'subject', 'body' and 'cta_label' must be non-empty "
                        f"strings of at most {MAX_SUBJECT_LENGTH}, {MAX_BODY_LENGTH} "
                        f"and {MAX_CTA_LABEL_LENGTH} characters"
                    },
                    send_cors=True,
                )
                return
            if not reminder_paragraphs(text):
                respond_with_json(
                    request, 400, {"error": "Empty 'body'"}, send_cors=True
                )
                return

            target = await self._claim_store.reminder_target(room_id)
            if target is None:
                respond_with_json(
                    request,
                    404,
                    {
                        "errcode": ERRCODE_NO_CLAIM_RECORD,
                        "error": "No claim record for this room",
                    },
                    send_cors=True,
                )
                return
            if target.claimed:
                respond_with_json(
                    request,
                    409,
                    {
                        "errcode": ERRCODE_COURSE_CLAIMED,
                        "error": "The course is already claimed",
                    },
                    send_cors=True,
                )
                return
            if target.requested_email is None:
                respond_with_json(
                    request,
                    422,
                    {
                        "errcode": ERRCODE_NO_REQUESTING_ADDRESS,
                        "error": "The course was created without a requesting address",
                    },
                    send_cors=True,
                )
                return

            code = await new_unique_code(self._datastores.main, self._claim_store)
            if code is None:
                respond_with_json(
                    request,
                    500,
                    {"error": "Failed to generate a claim code"},
                    send_cors=True,
                )
                return
            await self._claim_store.add_code(room_id, code, self._clock.time_msec())
            try:
                await self._mailer.send_course_reminder(
                    email_address=target.requested_email,
                    subject=subject,
                    body=text,
                    cta_label=cta_label,
                    claim_url=build_join_url(self._config.app_base_url, code),
                )
            except Exception as e:
                # The code reached nobody, so it is withdrawn rather than left
                # as a live claim nobody holds.
                logger.error(
                    f"Failed to send the claim reminder for {room_id}: {type(e).__name__}"
                )
                _capture_exception(e)
                await self._claim_store.remove_code(code)
                respond_with_json(
                    request,
                    502,
                    {"sent": False, "reason": "send_failed"},
                    send_cors=True,
                )
                return

            logger.info(f"Claim reminder sent for {room_id}")
            respond_with_json(request, 200, {"sent": True}, send_cors=True)

        # silent-ok: the caller's auth failure, answered 403
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ):
            respond_with_json(request, 403, {"error": "Forbidden"}, send_cors=True)
        except Exception as e:
            logger.error(f"Error sending a claim reminder: {type(e).__name__}")
            _capture_exception(e)
            respond_with_json(
                request, 500, {"error": "Internal server error"}, send_cors=True
            )
