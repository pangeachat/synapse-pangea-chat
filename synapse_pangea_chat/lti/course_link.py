"""The teacher side of Canvas (lane B2; CONTRACTS C5 L2, C2 T10-T11).

- Connect (L2): admin-dash returns a `connect` ticket, which only an
  Instructor or Administrator launch issues, bound to that teacher's linked
  Pangea account. The connect completes only for that same signed-in account,
  and only for a course it administers (power level 100, re-read now); the
  Canvas course comes from the ticket, never from the request. One Canvas
  course <-> one Pangea course.
- Status (T10): `connected` or `unconnected`, to the course's admins only.
- Import (T11): the course admin's roster pull. Admin rights are checked
  before any NRPS call, and NRPS uses only what the course link stored at
  connect time (issuer, deployment, context, NRPS URL). Learners with an
  email are upserted (`StudentInvitationStore.import_canvas`); those without
  one are counted, never guessed. No email is sent.

Logs and Sentry carry room and platform ids, counts and reason codes: never an
email, a Canvas user id, a ticket or a token.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple
from urllib.parse import urlencode

from synapse_pangea_chat.lti.http import UpstreamError
from synapse_pangea_chat.lti.link_store import (
    CONFLICT,
    KIND_CONNECT,
    MAX_TICKET_LENGTH,
    TICKET_OK,
    LtiLinkStore,
)
from synapse_pangea_chat.lti.nrps import NrpsClient
from synapse_pangea_chat.lti.store import LtiStore
from synapse_pangea_chat.student_invitations.api import (
    FORBIDDEN,
    parse_email,
    valid_room_id,
)
from synapse_pangea_chat.student_invitations.claim import now_ms
from synapse_pangea_chat.student_invitations.report import report_failure
from synapse_pangea_chat.student_invitations.store import StudentInvitationStore

logger = logging.getLogger("synapse.module.synapse_pangea_chat.lti.course_link")

Result = Tuple[int, Dict[str, Any]]

TICKET_INVALID: Result = (
    410,
    {"error": "Ticket is invalid or expired", "errcode": "ORG.PANGEA.TICKET_INVALID"},
)
TICKET_WRONG_ACCOUNT: Result = (
    403,
    {
        "error": "Ticket belongs to another account",
        "errcode": "ORG.PANGEA.TICKET_WRONG_ACCOUNT",
    },
)
NOT_CONNECTED: Result = (
    409,
    {"error": "Course is not connected", "errcode": "ORG.PANGEA.LTI_NOT_CONNECTED"},
)
ALREADY_CONNECTED: Result = (
    409,
    {
        "error": "Canvas course or Pangea course already connected",
        "errcode": "ORG.PANGEA.LTI_ALREADY_CONNECTED",
    },
)
UPSTREAM: Result = (
    502,
    {"error": "Canvas did not answer", "errcode": "ORG.PANGEA.LTI_UPSTREAM"},
)


def bad(message: str) -> Result:
    return 400, {"error": message, "errcode": "M_INVALID_PARAM"}


def ticket_arg(value: Any) -> Optional[str]:
    if isinstance(value, str) and 0 < len(value) <= MAX_TICKET_LENGTH:
        return value
    return None


class CourseAdmins(Protocol):
    async def is_course_admin(self, room_id: str, user_id: str) -> bool:
        ...


async def issue_connect_url(
    links: LtiLinkStore,
    admin_dash_base_url: str,
    *,
    platform_id: str,
    issuer: str,
    sub: str,
    context_id: str,
    deployment_id: str,
    nrps_url: str,
    user_id: str,
    now: int,
) -> str:
    """A connect ticket bound to `user_id`, as admin-dash's connect URL."""
    ticket = await links.issue_ticket(
        KIND_CONNECT,
        platform_id=platform_id,
        issuer=issuer,
        sub=sub,
        context_id=context_id,
        deployment_id=deployment_id,
        nrps_url=nrps_url,
        bound_user_id=user_id,
        now_ms=now,
    )
    base = admin_dash_base_url.rstrip("/")
    return f"{base}/canvas-connect?" + urlencode({"ticket": ticket})


class CourseLinks:
    def __init__(
        self,
        *,
        links: LtiLinkStore,
        platforms: LtiStore,
        invitations: StudentInvitationStore,
        admins: CourseAdmins,
        nrps: NrpsClient,
        admin_dash_base_url: str,
        clock_ms: Callable[[], int] = now_ms,
        new_id: Callable[[], str] = lambda: secrets.token_urlsafe(16),
    ) -> None:
        self._links = links
        self._platforms = platforms
        self._invitations = invitations
        self._admins = admins
        self._nrps = nrps
        self._admin_dash_base_url = admin_dash_base_url
        self._clock_ms = clock_ms
        self._new_id = new_id

    async def is_connected(self, room_id: str) -> bool:
        return await self._links.is_connected(room_id)

    async def _admin_room(self, caller: str, args: Any) -> Tuple[Optional[str], Result]:
        """The course a teacher route acts on, or the refusal."""
        if not isinstance(args, dict):
            return None, bad("Request body must be a JSON object")
        room_id = valid_room_id(args.get("room_id"))
        if room_id is None:
            return None, bad("'room_id' must be a valid Matrix room ID")
        if not await self._admins.is_course_admin(room_id, caller):
            return None, (403, FORBIDDEN)
        return room_id, (200, {})

    async def connect_url(
        self,
        *,
        platform_id: str,
        issuer: str,
        sub: str,
        context_id: str,
        deployment_id: str,
        nrps_url: str,
        user_id: str,
    ) -> str:
        return await issue_connect_url(
            self._links,
            self._admin_dash_base_url,
            platform_id=platform_id,
            issuer=issuer,
            sub=sub,
            context_id=context_id,
            deployment_id=deployment_id,
            nrps_url=nrps_url,
            user_id=user_id,
            now=self._clock_ms(),
        )

    # --- L2 ---

    async def connect(self, caller: str, body: Any) -> Result:
        if not isinstance(body, dict) or set(body) != {"ticket", "room_id"}:
            return bad("Expected exactly 'ticket' and 'room_id'")
        ticket = ticket_arg(body.get("ticket"))
        room_id = valid_room_id(body.get("room_id"))
        if ticket is None or room_id is None:
            return bad("'ticket' and a valid 'room_id' are required")
        outcome, found = await self._links.consume_ticket(
            ticket,
            kinds=(KIND_CONNECT,),
            now_ms=self._clock_ms(),
        )
        if outcome != TICKET_OK or found is None:
            return TICKET_INVALID
        if found.bound_user_id != caller:
            logger.info(
                "LTI connect refused: platform=%s wrong account", found.platform_id
            )
            return TICKET_WRONG_ACCOUNT
        if not await self._admins.is_course_admin(room_id, caller):
            return 403, FORBIDDEN
        linked = await self._links.link_course(found, room_id, caller, self._clock_ms())
        if linked == CONFLICT:
            logger.info(
                "LTI connect refused: room=%s platform=%s already connected",
                room_id,
                found.platform_id,
            )
            return ALREADY_CONNECTED
        logger.info(
            "LTI course connected: room=%s platform=%s (%s)",
            room_id,
            found.platform_id,
            linked,
        )
        return 200, {"status": "connected"}

    # --- T10 ---

    async def status(self, caller: str, query: Any) -> Result:
        room_id, refusal = await self._admin_room(caller, query)
        if room_id is None:
            return refusal
        connected = await self._links.is_connected(room_id)
        return 200, {"status": "connected" if connected else "unconnected"}

    # --- T11 ---

    async def import_roster(self, caller: str, body: Any) -> Result:
        room_id, refusal = await self._admin_room(caller, body)
        if room_id is None:
            return refusal
        link = await self._links.course_link(room_id)
        if link is None:
            return NOT_CONNECTED
        platform = await self._platforms.get_platform(link.platform_id)
        if platform is None or not platform.approved or platform.issuer != link.issuer:
            logger.warning(
                "LTI roster import refused: room=%s platform=%s not usable",
                room_id,
                link.platform_id,
            )
            return NOT_CONNECTED
        try:
            learners = await self._nrps.learners(
                platform, link.nrps_url, link.context_id
            )
        except UpstreamError as error:
            report_failure(
                "LTI roster import",
                error,
                room=room_id,
                platform=link.platform_id,
                reason=error.reason,
            )
            return UPSTREAM
        with_email: List[Tuple[str, str, str]] = []
        no_email = 0
        for learner in learners:
            parsed = parse_email(learner.email) if learner.email else None
            if parsed is None:
                no_email += 1
                continue
            entered, key = parsed
            with_email.append((learner.user_id, entered, key))
        result = await self._invitations.import_canvas(
            room_id,
            link.issuer,
            link.context_id,
            with_email,
            caller,
            self._clock_ms(),
            self._new_id,
        )
        logger.info(
            "LTI roster imported: room=%s platform=%s imported=%d attached=%d"
            " unchanged=%d conflicts=%d no_email=%d",
            room_id,
            link.platform_id,
            result["imported"],
            result["attached"],
            result["unchanged"],
            len(result["conflicts"]),
            no_email,
        )
        return 200, {**result, "no_email": no_email}
