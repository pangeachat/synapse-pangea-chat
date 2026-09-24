"""POST /_synapse/client/pangea/v1/create_course_space

Creates a private Matrix space for a custom course request and generates its
class code and single-use admin code. When the request carries the address the
course was requested from, records it and emails that address the claim link
(the admin code); the class code is emailed on the claim, by knock_with_code.
See create-course-space.instructions.md and knock-with-code.instructions.md.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

from synapse.api.constants import EventTypes, RoomCreationPreset
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
from synapse.types import create_requester
from twisted.web.resource import Resource

from synapse_pangea_chat.email_invite.build_join_url import build_join_url
from synapse_pangea_chat.email_invite.course_claim_emails import CourseClaimMailer
from synapse_pangea_chat.email_invite.course_claims import CourseClaimStore
from synapse_pangea_chat.grant_instructor_analytics_access.grant_instructor_analytics_access import (
    COURSE_SETTINGS_STATE_EVENT_TYPE,
    REQUIRE_ANALYTICS_ACCESS_KEY,
)
from synapse_pangea_chat.room_code.constants import (
    ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    EVENT_TYPE_M_ROOM_JOIN_RULES,
    KNOCK_JOIN_RULE_VALUE,
)
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.generate_room_code import generate_access_code
from synapse_pangea_chat.room_code.get_rooms_with_access_code import (
    get_rooms_with_access_code,
)

try:
    import sentry_sdk  # type: ignore[import-not-found]
# silent-ok: sentry-sdk is an optional Synapse extra; without it captures are no-ops (below)
except ImportError:
    sentry_sdk = None

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.email_invite.create_course_space"
)


def _capture_exception(e: Exception) -> None:
    # The course is created and answered 200 even when its email fails, so the
    # failure never reaches Synapse's request-level capture.
    if sentry_sdk is not None:
        sentry_sdk.capture_exception(e)


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# Matches what the client creates a course space with (selected_course_page
# passes spaceChild: 0 into its defaultSpacePowerLevelsContent). m.space.child
# is 0 so a regular member can attach a room: learners' activity sessions fan
# out into their courses as space children, and a member sits at users_default.
DEFAULT_SPACE_POWER_LEVELS: Dict[str, Any] = {
    "ban": 50,
    "kick": 50,
    "invite": 50,
    "redact": 50,
    "events": {
        "m.room.power_levels": 100,
        "m.room.join_rules": 100,
        "m.space.child": 0,
    },
    "events_default": 0,
    "state_default": 50,
    "users_default": 0,
    "notifications": {"room": 50},
}

# Pangea state event type for course plan association
PANGEA_COURSE_PLAN_STATE_EVENT_TYPE = "pangea.course_plan"


def build_course_plan_content(
    course_plan_id: Any, target_language: Any
) -> Dict[str, Any]:
    """Content for the ``pangea.course_plan`` state event of a new space.

    The plan id goes under ``uuid`` and, when the caller knows it, the course's
    target language under ``l2``. Both are read by the public course catalog —
    a space with no ``l2`` is excluded from every language-filtered browse once
    it is published (see public-courses.instructions.md).

    ``target_language`` is optional and taken from the request body, never
    fetched: this endpoint does not call the CMS, and all details arrive in the
    request (see create-course-space.instructions.md). A caller that omits it
    still gets a valid course space; the one-time ``l2`` backfill repairs it
    later. ``l2`` is omitted entirely rather than written empty, because the
    catalog treats an empty value as absent and a key that is sometimes null
    and sometimes missing is two shapes for one state.
    """
    content: Dict[str, Any] = {"uuid": course_plan_id}
    if isinstance(target_language, str) and target_language.strip():
        content["l2"] = target_language.strip()
    return content


class CreateCourseSpace(Resource):
    isLeaf = True

    def __init__(
        self,
        api: ModuleApi,
        config: PangeaChatConfig,
        claim_store: CourseClaimStore,
        mailer: CourseClaimMailer,
    ):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()
        self._claim_store = claim_store
        self._mailer = mailer

    def render_POST(self, request: SynapseRequest):
        run_in_background(self._async_render_POST, request)
        return server.NOT_DONE_YET

    async def _async_render_POST(self, request: SynapseRequest):
        try:
            requester = await self._auth.get_user_by_req(request)
            requester_id = requester.user.to_string()

            body = await extract_body_json(request)
            if not isinstance(body, dict):
                respond_with_json(
                    request,
                    400,
                    {"error": "Invalid JSON in request body"},
                    send_cors=True,
                )
                return

            # Validate required fields
            title = body.get("title")
            if not isinstance(title, str) or not title.strip():
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing or empty 'title'"},
                    send_cors=True,
                )
                return

            # Optional: a course created without the requesting address sends
            # no email and stays bot-administered until admin is granted
            # deliberately (create-course-space.instructions.md).
            teacher_email = body.get("teacher_email")
            if teacher_email is not None and (
                not isinstance(teacher_email, str) or "@" not in teacher_email
            ):
                respond_with_json(
                    request,
                    400,
                    {"error": "'teacher_email' must be an email address"},
                    send_cors=True,
                )
                return
            teacher_email = _optional_str(teacher_email)
            request_summary = _optional_str(body.get("request_summary"))

            description = body.get("description", "")
            course_plan_id = body.get("course_plan_id", "")
            image_url = body.get("image_url")

            course_plan_content = build_course_plan_content(
                course_plan_id, body.get("target_language")
            )

            # Generate two unique access codes
            student_code = await self._generate_unique_code()
            if student_code is None:
                respond_with_json(
                    request,
                    500,
                    {"error": "Failed to generate student access code"},
                    send_cors=True,
                )
                return

            admin_code = await self._generate_unique_code()
            if admin_code is None:
                respond_with_json(
                    request,
                    500,
                    {"error": "Failed to generate admin access code"},
                    send_cors=True,
                )
                return

            # Build initial state events for the space
            initial_state = [
                # Join rules with knock + both access codes
                {
                    "type": EVENT_TYPE_M_ROOM_JOIN_RULES,
                    "state_key": "",
                    "content": {
                        "join_rule": KNOCK_JOIN_RULE_VALUE,
                        ACCESS_CODE_JOIN_RULE_CONTENT_KEY: student_code,
                        ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY: admin_code,
                    },
                },
                # Course plan association
                {
                    "type": PANGEA_COURSE_PLAN_STATE_EVENT_TYPE,
                    "state_key": "",
                    "content": course_plan_content,
                },
                # Require instructor analytics access to join, as a course the
                # client creates does. The setting defaults to off when the
                # event is absent, so it has to be written here or the
                # teacher sees no student analytics.
                {
                    "type": COURSE_SETTINGS_STATE_EVENT_TYPE,
                    "state_key": "",
                    "content": {REQUIRE_ANALYTICS_ACCESS_KEY: True},
                },
            ]

            # Power levels with creator as admin
            power_levels = dict(DEFAULT_SPACE_POWER_LEVELS)
            power_levels["users"] = {requester_id: 100}
            initial_state.append(
                {
                    "type": EventTypes.PowerLevels,
                    "state_key": "",
                    "content": power_levels,
                }
            )

            # Create the space
            room_config = {
                "preset": RoomCreationPreset.PRIVATE_CHAT,
                "name": title,
                "topic": description,
                "creation_content": {"type": "m.space"},
                "initial_state": initial_state,
                "visibility": "private",
            }

            room_creation_handler = self._api._hs.get_room_creation_handler()
            room_id, _, _ = await room_creation_handler.create_room(
                requester=create_requester(
                    requester_id,
                    authenticated_entity=self._api.server_name,
                ),
                config=room_config,
                ratelimit=False,
            )

            # Set room avatar if image URL provided
            if isinstance(image_url, str) and image_url.strip():
                try:
                    await self._api.create_and_send_event_into_room(
                        {
                            "type": EventTypes.RoomAvatar,
                            "room_id": room_id,
                            "sender": requester_id,
                            "state_key": "",
                            "content": {"url": image_url},
                        }
                    )
                except Exception as e:
                    logger.warning(f"Failed to set room avatar: {e}")

            # Build admin join URL — short code on the env's app host, never a
            # hardcoded host (see build_join_url).
            admin_join_url = build_join_url(self._config.app_base_url, admin_code)

            # The address and the claim link stay out of the log: the link is
            # the claim, and whoever reads it can spend it.
            logger.info(f"Course space created: room_id={room_id}")

            emailed = False
            if teacher_email is not None:
                emailed = await self._record_and_email(
                    room_id=room_id,
                    teacher_email=teacher_email,
                    title=title.strip(),
                    description=description if isinstance(description, str) else "",
                    request_summary=request_summary,
                    claim_url=admin_join_url,
                )

            respond_with_json(
                request,
                200,
                {
                    "room_id": room_id,
                    "student_access_code": student_code,
                    "admin_access_code": admin_code,
                    "admin_join_url": admin_join_url,
                    "emailed": emailed,
                },
                send_cors=True,
            )

        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ) as e:
            logger.error(f"Forbidden: {e}")
            respond_with_json(
                request,
                403,
                {"error": "Forbidden"},
                send_cors=True,
            )
        except Exception as e:
            logger.error(f"Error creating course space: {e}")
            respond_with_json(
                request,
                500,
                {"error": "Internal server error"},
                send_cors=True,
            )

    async def _record_and_email(
        self,
        *,
        room_id: str,
        teacher_email: str,
        title: str,
        description: str,
        request_summary: str | None,
        claim_url: str,
    ) -> bool:
        """Record the requesting address, then send it the claim link.

        The record comes first: a claim link whose claim could not send the
        class code would leave the teacher holding a course with no way to
        invite anyone, so without a record no link goes out. Either failure is
        captured and reported as ``emailed: false`` rather than failing the
        request, because the space already exists and a retry would make a
        second one.
        """
        try:
            await self._claim_store.record(
                room_id, teacher_email, self._api._hs.get_clock().time_msec()
            )
        except Exception as e:
            logger.error(f"Failed to record the requesting address for {room_id}: {e}")
            _capture_exception(e)
            return False
        try:
            await self._mailer.send_course_ready(
                email_address=teacher_email,
                course_title=title,
                course_description=description,
                request_summary=request_summary,
                claim_url=claim_url,
            )
        except Exception as e:
            logger.error(
                f"Failed to send the course-ready email for {room_id}: "
                f"{type(e).__name__}"
            )
            _capture_exception(e)
            return False
        return True

    async def _generate_unique_code(self) -> str | None:
        """Generate an access code that doesn't conflict with existing ones."""
        for _ in range(10):
            code = generate_access_code()
            matches = await get_rooms_with_access_code(
                access_code=code, room_store=self._datastores.main
            )
            if len(matches) == 0:
                return code
        return None
