"""``POST /_synapse/client/pangea/v1/knock_with_code`` — join a course with a code.

Design: knock-with-code.instructions.md. Not a Matrix knock: the user never
enters ``knock`` membership; the module invites them as a joined admin.

Bearer auth. Body ``{"access_code": "<7 chars>"}``. Answers:

- 200 ``{rooms, already_joined, banned}``: ``rooms`` were invited now or were
  already invited (the client's own ``/join`` then succeeds, issue #148);
  ``already_joined`` includes a claimed or replayed course. A room is in
  exactly one list. The invite arrives over ``/sync``, and the client hides
  its invite dialog while the code flow handles the join ("Space invite
  priority" in the client's joining-courses doc).
- 400 ``M_INVALID_PARAM``: not seven alphanumerics with a digit. This is the
  only place the format rule lives. Other 400s (no body, no or non-string
  ``access_code``) carry no errcode, since only a client bug causes them.
- 404 ``ORG.PANGEA.CODE_NOT_FOUND``: well-formed, opens nothing; also what a
  spent, revoked or someone else's claim code answers.
- 403 ``ORG.PANGEA.BANNED_FROM_ROOM`` with ``banned``: banned from every room
  the code opens (issue #127 / client#6820).
- 403 ``M_FORBIDDEN``: every room's admins blocked the requester; no detail,
  so the block is not revealed (blocked-join-gate).
- 500 ``ORG.PANGEA.INVITE_FAILED`` with ``failed``: every room failed. Before
  this an all-failed code answered 200 with empty lists, which clients render
  as "code not found" (issue #197).
- 503 ``ORG.PANGEA.CLAIM_RECOVERY_REQUIRED``: a requested course's claim
  could not complete; the reservation stays with the winning account.
- 429: rate limited (``is_rate_limited``).

Order matters: a private claim or an admin code is processed before the
"already a member" shortcut, so a member can still be made an admin.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

import logging
from typing import List, Optional, Set, Tuple

from synapse.api.errors import (
    AuthError,
    Codes,
    InvalidClientCredentialsError,
    InvalidClientTokenError,
    MissingClientTokenError,
    SynapseError,
)
from synapse.http import server
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.logging.context import run_in_background
from synapse.module_api import ModuleApi
from twisted.web.resource import Resource

from synapse_pangea_chat.blocked_join_gate import is_blocked_by_room_admin
from synapse_pangea_chat.email_invite.course_claim_notice import CourseClaimNotifier
from synapse_pangea_chat.email_invite.course_claims import (
    CourseClaim,
    CourseClaimStore,
)
from synapse_pangea_chat.email_invite.course_invitations import invitation_kind
from synapse_pangea_chat.room_code.burn_admin_code import burn_admin_code
from synapse_pangea_chat.room_code.constants import (
    ERRCODE_BANNED_FROM_ROOM,
    ERRCODE_CODE_NOT_FOUND,
    ERRCODE_INVITE_FAILED,
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
from synapse_pangea_chat.joined_via import (
    VIA_CODE,
    VIA_INSTRUCTOR,
    module_write,
    named,
)
from synapse_pangea_chat.room_code.code_index import RoomCodeMatch
from synapse_pangea_chat.room_code.code_lookup import rooms_for_code
from synapse_pangea_chat.room_code.extract_body_json import extract_body_json
from synapse_pangea_chat.room_code.get_inviter_user import (
    get_inviter_user,
    promote_user_to_admin,
    was_previously_admin,
)
from synapse_pangea_chat.room_code.instructor_access import is_joined_instructor
from synapse_pangea_chat.room_code.invite_user_to_room import invite_user_to_room
from synapse_pangea_chat.room_code.is_rate_limited import is_rate_limited
from synapse_pangea_chat.room_code.user_is_room_member import (
    get_user_room_membership,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.room_code.knock_with_code"
)


class ClaimPromotionFailed(Exception):
    """The claimer of a requested course could not be promoted; the claim is
    held for them and the admin code left unburned, so they can retry."""


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
        notifier: CourseClaimNotifier,
        provisioner=None,
    ):
        super().__init__()
        self._api = api
        self._config = config
        self._auth = self._api._hs.get_auth()
        self._datastores = self._api._hs.get_datastores()
        self._claim_store = claim_store
        self._notifier = notifier
        self._provisioner = provisioner

    async def _hold_after_creation(self) -> None:
        """Wait before answering a claim that has just created its course.

        Synapse stores a new room's create event and its creator's join before
        the rest of its state, so the claimant's app receives the room as
        joined about a second before it receives ``pangea.course_plan``. The
        released apps open the course page as soon as this endpoint answers,
        read the plan once, and show "This course is no longer available" when
        it is not there yet. Answering a few seconds later lets the sync that
        carries the plan reach the app first. It narrows the race rather than
        closing it: the app's own fix is to read the plan again when it
        arrives. The verified-address claim at sign-in does not wait, because
        no app is waiting on it to open the course. The released apps handle
        a course answered as already joined before sync catches up only in
        this narrowed form; end-to-end checks on each supported app are still
        owed. May deserve a line in knock-with-code.instructions.md,
        "Claiming a course".
        """
        hold = self._config.course_claim_response_hold_seconds
        if hold > 0:
            await self._api.sleep(hold)

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
                # The client sends whatever the learner typed, so this is
                # usually a learner entering something that isn't a code (a
                # course name) — the errcode marks it as their input. The
                # other 400s above carry none: only a client bug causes them.
                logger.warning("Invalid access_code")
                respond_with_json(
                    request,
                    400,
                    {"errcode": Codes.INVALID_PARAM, "error": "Invalid access_code"},
                    send_cors=True,
                )
                return

            if self._provisioner is not None:
                invitation = await self._claim_store.invitations.for_code(access_code)
                if invitation is not None:
                    # Read before the claim: a completed invitation is a replay
                    # of a course that already exists.
                    # An instructor invitation joins a course that already
                    # holds its plan.
                    created_now = (
                        invitation["status"] != "completed"
                        and invitation_kind(invitation) == "course"
                    )
                    room = await self._provisioner.claim(invitation, requester_id)
                    if created_now:
                        await self._hold_after_creation()
                    respond_with_json(
                        request,
                        200,
                        {"rooms": [], "already_joined": [room], "banned": []},
                        send_cors=True,
                    )
                    return

            # A student invitation's own code opens its course like the class code and
            # records which invitation the join came from; it is never spent.
            student = await self._claim_store.students.for_code(access_code)
            found: Optional[Tuple[List[RoomCodeMatch], Set[str]]]
            if student is not None:
                found = (
                    [RoomCodeMatch(room_id=student["room_id"], is_admin_code=False)],
                    set(),
                )
            else:
                # Join rules, and the claim record for a requested course's claim
                # code, which members must not be able to read (code_lookup).
                found = await rooms_for_code(
                    access_code, self._datastores.main, self._claim_store
                )
            if found is None:
                respond_with_json(
                    request,
                    500,
                    {"error": "Internal server error"},
                    send_cors=True,
                )
                return
            matches, claim_room_ids = found
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
                        "error": "No rooms found with the access code",
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
            # Requested courses whose claim another account already holds: the
            # narrow window between that account taking the claim and its
            # promotion spending the code. The code is spent either way.
            spent_rooms: List[str] = []
            for match in matches:
                try:
                    membership = await get_user_room_membership(
                        api=self._api,
                        user_id=requester_id,
                        room_id=match.room_id,
                    )
                    # Only the code a requested course was created with is a
                    # claim; an admin code in join rules, such as one granted
                    # later to a co-teacher, is an ordinary admin grant.
                    claim: Optional[CourseClaim] = None
                    if match.room_id in claim_room_ids:
                        claim = await self._claim_store.get(match.room_id)
                    if claim is not None and claim.promoted_at_ms is not None:
                        state = await self._api.get_room_state(match.room_id)
                        if claim.claimed_by == requester_id and is_joined_instructor(
                            state, requester_id
                        ):
                            already_joined_rooms.append(match.room_id)
                        else:
                            spent_rooms.append(match.room_id)
                        continue
                    if claim is not None and membership == "leave":
                        raise ClaimPromotionFailed(
                            "Claimant left or was removed; operator recovery required"
                        )
                    if (
                        claim is not None
                        and claim.claimed_by == requester_id
                        and membership == MEMBERSHIP_JOIN
                    ):
                        state = await self._api.get_room_state(match.room_id)
                        power = state.get(("m.room.power_levels", ""))
                        if (
                            power
                            and power.content.get("users", {}).get(requester_id, 0)
                            < 100
                            and await was_previously_admin(
                                self._api, match.room_id, requester_id
                            )
                        ):
                            raise ClaimPromotionFailed(
                                "Claimant was demoted; operator recovery required"
                            )
                    # Holding the claim code proves the requesting inbox, and
                    # it is in no state a member can read, so a member who
                    # joined first (the teacher trying their class link, or a
                    # claimer whose promotion failed) can still claim with it.
                    if (
                        membership == MEMBERSHIP_JOIN
                        and claim is None
                        and not match.is_admin_code
                    ):
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
                    if claim is not None and not await self._claim_store.claim(
                        match.room_id,
                        requester_id,
                        self._api._hs.get_clock().time_msec(),
                    ):
                        spent_rooms.append(match.room_id)
                        continue
                    if membership not in (MEMBERSHIP_INVITE, MEMBERSHIP_JOIN):
                        # An already-invited user holds the invite the
                        # endpoint exists to issue; re-inviting is at best
                        # redundant and at worst a failure that hid the room
                        # from every list. Return it in `rooms` so the
                        # client's own /join proceeds (issue #148).
                        await invite_user_to_room(
                            api=self._api,
                            user_id=requester_id,
                            room_id=match.room_id,
                            # An admin code or a course claim makes an admin.
                            via=(
                                VIA_INSTRUCTOR
                                if match.is_admin_code or claim is not None
                                else VIA_CODE
                            ),
                        )

                    if claim is not None and membership != MEMBERSHIP_JOIN:
                        with module_write():
                            await self._api.update_room_membership(
                                sender=requester_id,
                                target=requester_id,
                                room_id=match.room_id,
                                new_membership=MEMBERSHIP_JOIN,
                                content=named(VIA_INSTRUCTOR),
                            )
                        membership = MEMBERSHIP_JOIN
                    # Admin code: promote to admin and burn the code
                    if match.is_admin_code:
                        if await get_inviter_user(self._api, match.room_id) is None:
                            raise ClaimPromotionFailed(
                                "No authorized administrator remains"
                            )
                        promoted = await promote_user_to_admin(
                            api=self._api,
                            room_id=match.room_id,
                            user_to_promote=requester_id,
                            invite_power=100,
                        )
                        if not promoted:
                            raise ClaimPromotionFailed("Instructor grant failed")
                        if claim is None:
                            burned = await burn_admin_code(
                                api=self._api,
                                room_id=match.room_id,
                                burner_user_id=requester_id,
                            )
                            if not burned:
                                raise ClaimPromotionFailed(
                                    "Instructor code could not be spent"
                                )
                        else:
                            # A claim is spent, and its notice made owed, only
                            # once the promotion is real, in one write. Failing
                            # here leaves the code unspent and the claim held,
                            # so the claimer can retry.
                            state = await self._api.get_room_state(match.room_id)
                            if not is_joined_instructor(state, requester_id):
                                raise ClaimPromotionFailed(
                                    "Claimant authority could not be verified"
                                )
                            await self._claim_store.mark_promoted(
                                match.room_id,
                                requester_id,
                                self._api._hs.get_clock().time_msec(),
                            )
                            # In the background: the code is spent, so a join
                            # held open on a slow mail server and timed out
                            # could not be retried. The notice is already
                            # recorded as owed; the retry loop covers a send
                            # that does not finish.
                            run_in_background(
                                self._notifier.notify, match.room_id, requester_id
                            )
                    # Listed only once everything for the room has succeeded,
                    # so a room is in exactly one list.
                    if membership == MEMBERSHIP_JOIN:
                        already_joined_rooms.append(match.room_id)
                    else:
                        invited_rooms.append(match.room_id)
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
                        "error": "No rooms found with the access code",
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
            if student is not None and student["room_id"] in (
                invited_rooms + already_joined_rooms
            ):
                await self._claim_store.students.record_use(
                    student["invitation_id"],
                    requester_id,
                    self._api._hs.get_clock().time_msec(),
                )
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

        except SynapseError as e:
            if e.code >= 500:
                _capture_exception(e)
            respond_with_json(request, e.code, e.error_dict(None), send_cors=True)
        except Exception as e:
            logger.error(f"Error processing request: {e}")
            _capture_exception(e)
            respond_with_json(
                request,
                500,
                {"error": "Internal server error"},
                send_cors=True,
            )
