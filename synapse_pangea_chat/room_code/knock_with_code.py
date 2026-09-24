from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

import logging
from typing import List, Optional

from synapse.api.constants import EventTypes
from synapse.api.errors import (
    AuthError,
    Codes,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from synapse.types import UserID
from twisted.web.resource import Resource

from synapse_pangea_chat.blocked_join_gate import is_blocked_by_room_admin
from synapse_pangea_chat.email_invite.build_join_url import build_join_url
from synapse_pangea_chat.email_invite.course_claim_emails import CourseClaimMailer
from synapse_pangea_chat.email_invite.course_claims import (
    CourseClaim,
    CourseClaimStore,
)
from synapse_pangea_chat.room_code.burn_admin_code import burn_admin_code
from synapse_pangea_chat.room_code.constants import (
    ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    ERRCODE_BANNED_FROM_ROOM,
    ERRCODE_CODE_NOT_FOUND,
    ERRCODE_INVITE_FAILED,
    EVENT_TYPE_M_ROOM_JOIN_RULES,
    MEMBERSHIP_BAN,
    MEMBERSHIP_INVITE,
    MEMBERSHIP_JOIN,
)

try:
    import sentry_sdk  # type: ignore[import-not-found]
# silent-ok: sentry-sdk is an optional Synapse extra; without it captures are no-ops (below)
except ImportError:
    # Sentry is an optional Synapse extra; without it captures are no-ops.
    sentry_sdk = None
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.get_inviter_user import promote_user_to_admin
from synapse_pangea_chat.room_code.get_rooms_with_access_code import (
    get_rooms_with_access_code,
)
from synapse_pangea_chat.room_code.invite_user_to_room import invite_user_to_room
from synapse_pangea_chat.room_code.is_rate_limited import is_rate_limited
from synapse_pangea_chat.room_code.user_is_room_member import (
    get_user_room_membership,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.room_code.knock_with_code"
)


def _capture_exception(e: Exception) -> None:
    # respond_with_json paths never propagate to Synapse's request-level
    # Sentry capture, so failures this handler absorbs must be captured
    # explicitly or they are invisible (issue #197).
    if sentry_sdk is not None:
        sentry_sdk.capture_exception(e)


class KnockWithCode(Resource):
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
            if is_rate_limited(requester_id, self._config):
                respond_with_json(
                    request,
                    429,
                    {"error": "Rate limited"},
                    send_cors=True,
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

            # Check if the request body contains the access code
            if "access_code" not in body:
                logger.error("Missing 'access_code' in request body")
                respond_with_json(
                    request,
                    400,
                    {"error": "Missing 'access_code' in request body"},
                    send_cors=True,
                )
                return
            access_code = body["access_code"]

            # Check if the access code is a string and has the correct format
            if not isinstance(access_code, str):
                logger.error("'access_code' must be a string")
                respond_with_json(
                    request,
                    400,
                    {"error": "'access_code' must be a string"},
                    send_cors=True,
                )
                return
            if (
                len(access_code) != 7
                or not access_code.isalnum()
                or not any(char.isdigit() for char in access_code)  # At least one digit
            ):
                logger.warning(f"Invalid 'access_code': {access_code}")
                respond_with_json(
                    request,
                    400,
                    {"error": f"Invalid 'access_code': {access_code}"},
                    send_cors=True,
                )
                return

            # Get the rooms with the access code
            matches = await get_rooms_with_access_code(
                access_code=access_code, room_store=self._datastores.main
            )
            if matches is None:
                respond_with_json(
                    request,
                    500,
                    {"error": "Internal server error"},
                    send_cors=True,
                )
                return
            if len(matches) == 0:
                # 404, not 400: the request is well-formed — the code just
                # doesn't exist. The errcode lets clients show a "check the
                # code with your teacher" message and log it distinctly
                # (issue #197 / client#8693).
                respond_with_json(
                    request,
                    404,
                    {
                        "errcode": ERRCODE_CODE_NOT_FOUND,
                        "error": f"No rooms found with the access code: {access_code}",
                    },
                    send_cors=True,
                )
                return

            # Send knock with access code to the rooms as requester
            invited_rooms: List[str] = []
            already_joined_rooms: List[str] = []
            banned_rooms: List[str] = []
            failed_rooms: List[str] = []
            # Rooms where every admin has blocked the requester. Never
            # returned to the client — the refusal must not reveal the block
            # (see blocked-join-gate.instructions.md).
            blocked_rooms: List[str] = []
            # Requested courses whose claim another account already holds. The
            # admin code is burned right after a claim, so this is the narrow
            # window before the burn lands; the code is spent either way.
            spent_rooms: List[str] = []
            for match in matches:
                try:
                    membership = await get_user_room_membership(
                        api=self._api,
                        user_id=requester_id,
                        room_id=match.room_id,
                    )
                    if membership == MEMBERSHIP_JOIN:
                        already_joined_rooms.append(match.room_id)
                        continue
                    if membership == MEMBERSHIP_BAN:
                        # Inviting a banned user would be rejected by
                        # Synapse anyway; surface it distinctly instead of
                        # letting the failure look like a nonexistent code
                        # (issue #127 / client#6820).
                        banned_rooms.append(match.room_id)
                        continue
                    if (
                        self._config.blocked_join_gate_enabled
                        and await is_blocked_by_room_admin(
                            self._api, match.room_id, requester_id
                        )
                    ):
                        blocked_rooms.append(match.room_id)
                        continue
                    claim: Optional[CourseClaim] = None
                    if match.is_admin_code:
                        claim = await self._claim_store.get(match.room_id)
                        if claim is not None and not await self._claim_store.claim(
                            match.room_id,
                            requester_id,
                            self._api._hs.get_clock().time_msec(),
                        ):
                            spent_rooms.append(match.room_id)
                            continue
                    if membership != MEMBERSHIP_INVITE:
                        # An already-invited user holds the invite the
                        # endpoint exists to issue; re-inviting is at best
                        # redundant and at worst a failure that hid the room
                        # from every list. Return it in `rooms` so the
                        # client's own /join proceeds (issue #148).
                        await invite_user_to_room(
                            api=self._api,
                            user_id=requester_id,
                            room_id=match.room_id,
                        )
                    invited_rooms.append(match.room_id)

                    # Admin code: promote to admin and burn the code
                    if match.is_admin_code:
                        await promote_user_to_admin(
                            api=self._api,
                            room_id=match.room_id,
                            user_to_promote=requester_id,
                            invite_power=100,
                        )
                        await burn_admin_code(
                            api=self._api,
                            room_id=match.room_id,
                            burner_user_id=requester_id,
                        )
                        if claim is not None and not claim.notice_sent:
                            await self._send_claim_notice(
                                match.room_id, requester_id, claim
                            )
                except Exception as e:
                    # A failed room must not block the others, but it must
                    # not vanish either: capture it, and count it so an
                    # all-rooms-failed code doesn't answer 200-with-empty-
                    # lists, which clients render as "code not found"
                    # (issue #197).
                    failed_rooms.append(match.room_id)
                    logger.error(
                        f"Error sending knock with code to {match.room_id}: {e}"
                    )
                    _capture_exception(e)
            if banned_rooms and not invited_rooms and not already_joined_rooms:
                # The code was valid but every matched room has banned the
                # user — a distinct failure, not a nonexistent code.
                respond_with_json(
                    request,
                    403,
                    {
                        "errcode": ERRCODE_BANNED_FROM_ROOM,
                        "error": "You are banned from the course for this code",
                        "banned": banned_rooms,
                    },
                    send_cors=True,
                )
                return
            if failed_rooms and not invited_rooms and not already_joined_rooms:
                # The code was valid but every invite failed — a server-side
                # problem (no eligible inviter, rate limiting, ...), not a
                # nonexistent code.
                respond_with_json(
                    request,
                    500,
                    {
                        "errcode": ERRCODE_INVITE_FAILED,
                        "error": "Failed to invite to any room matching the code",
                        "failed": failed_rooms,
                    },
                    send_cors=True,
                )
                return
            if (
                spent_rooms
                and not invited_rooms
                and not already_joined_rooms
                and not banned_rooms
                and not failed_rooms
                and not blocked_rooms
            ):
                # The admin code was used a moment ago by someone else: to
                # this requester it is a code that no longer exists.
                respond_with_json(
                    request,
                    404,
                    {
                        "errcode": ERRCODE_CODE_NOT_FOUND,
                        "error": f"No rooms found with the access code: {access_code}",
                    },
                    send_cors=True,
                )
                return
            if (
                blocked_rooms
                and not invited_rooms
                and not already_joined_rooms
                and not banned_rooms
                and not failed_rooms
            ):
                # Every matched room refused the requester because its admins
                # all blocked them. Generic forbidden on purpose: no reason, no
                # errcode of its own, no room list.
                respond_with_json(
                    request,
                    403,
                    {"errcode": Codes.FORBIDDEN, "error": "Forbidden"},
                    send_cors=True,
                )
                return
            respond_with_json(
                request,
                200,
                {
                    "message": f"Invited {requester_id}",
                    "rooms": invited_rooms,
                    "already_joined": already_joined_rooms,
                    "banned": banned_rooms,
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
            logger.error(f"Error processing request: {e}")
            _capture_exception(e)
            respond_with_json(
                request,
                500,
                {"error": "Internal server error"},
                send_cors=True,
            )

    async def _send_claim_notice(
        self, room_id: str, claimer_id: str, claim: CourseClaim
    ) -> None:
        """Email the class link to the address the course was requested from.

        It goes to that address, not the claimer's account, and names the
        claimer, so it doubles as the claim notice (knock-with-code, "Claiming
        a course"). A failure is captured and never fails the claim: the
        claimer is already the course's admin, and the class code is in the
        app for them either way.
        """
        if claim.requested_email is None:
            return
        try:
            state = await self._api.get_room_state(
                room_id=room_id,
                event_filter=[
                    (EVENT_TYPE_M_ROOM_JOIN_RULES, None),
                    (EventTypes.Name, None),
                ],
            )
            class_code: Optional[str] = None
            title = ""
            for event in state.values():
                if event.type == EVENT_TYPE_M_ROOM_JOIN_RULES:
                    class_code = event.content.get(ACCESS_CODE_JOIN_RULE_CONTENT_KEY)
                elif event.type == EventTypes.Name:
                    title = event.content.get("name") or ""
            if not isinstance(class_code, str) or not class_code:
                raise ValueError(f"Claimed course {room_id} has no class code")
            display_name: Optional[str] = None
            if self._api.is_mine(claimer_id):
                profile = await self._api.get_profile_for_user(
                    UserID.from_string(claimer_id).localpart
                )
                display_name = profile.display_name
            await self._mailer.send_course_claimed(
                email_address=claim.requested_email,
                course_title=title,
                claimed_by_user_id=claimer_id,
                claimed_by_display_name=display_name,
                class_url=build_join_url(self._config.app_base_url, class_code),
                class_code=class_code,
            )
            await self._claim_store.mark_notice_sent(
                room_id, claimer_id, self._api._hs.get_clock().time_msec()
            )
        except Exception as e:
            logger.error(
                f"Failed to send the claim notice for {room_id}: {type(e).__name__}"
            )
            _capture_exception(e)
