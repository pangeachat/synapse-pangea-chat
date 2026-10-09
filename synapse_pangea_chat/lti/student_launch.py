"""After a verified launch: where it goes, and the link step (lane B3, with
the instructor half of lane B2; CONTRACTS C5.2, L1).

The module never creates an account and never signs anyone in on a
platform's say-so alone. A Canvas identity (issuer + `sub`) opens a Pangea
account only once that account has signed in itself and returned a link
ticket; only then does a later launch get a login token for it. The Canvas
email is never read here, never bound and never matched.

Launch redirects (`LaunchRedirects`):

| Launch | Goes to |
|---|---|
| Learner, not linked | `<app>/lti/link?ticket=<learner ticket>&course=<title>` |
| Learner, linked, unconfirmed matching invitations | the same, the ticket bound to that account |
| Learner, linked, nothing to confirm | `<app>/lti/token?loginToken=<token>` |
| Instructor/Administrator, not linked | `<app>/lti/link?ticket=<instructor ticket>&role=instructor` |
| Instructor/Administrator, linked | `<admin-dash>/canvas-connect?ticket=<connect ticket>` |

The link step (`LinkStep`, `POST lti/link`) checks the body first (it needs no
ticket), then consumes the ticket in one update, whatever happens after; any
refusal after that writes nothing. An unbound ticket links the caller's own
account (their token is required); a bound one acts only for the account it
is bound to. A learner ticket then records the confirmation on, and claims,
only the invitations imported with exactly the ticket's (issuer, Canvas
course, `sub`).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from synapse.http.server import finish_request
from synapse.http.site import SynapseRequest

from synapse_pangea_chat.config import MANAGED_DISCLOSURE_VERSION
from synapse_pangea_chat.lti.course_link import (
    TICKET_INVALID,
    TICKET_WRONG_ACCOUNT,
    CourseLinks,
    bad,
    issue_connect_url,
    ticket_arg,
)
from synapse_pangea_chat.lti.endpoints import refuse_launch
from synapse_pangea_chat.lti.link_store import (
    CONFLICT,
    KIND_INSTRUCTOR,
    KIND_LEARNER,
    TICKET_OK,
    TICKET_UNBOUND,
    TICKET_WRONG_KIND,
    LtiLinkStore,
    Ticket,
)
from synapse_pangea_chat.lti.nrps import nrps_url_from_launch
from synapse_pangea_chat.lti.validation import (
    CLAIM_CONTEXT,
    PATH_INSTRUCTOR,
    Launch,
    LaunchRejected,
)
from synapse_pangea_chat.student_invitations.api import UNAUTHORIZED
from synapse_pangea_chat.student_invitations.claim import StudentClaims, now_ms
from synapse_pangea_chat.student_invitations.report import report_failure
from synapse_pangea_chat.student_invitations.store import (
    CLAIMED,
    StudentInvitationStore,
)

logger = logging.getLogger("synapse.module.synapse_pangea_chat.lti.student_launch")

Result = Tuple[int, Dict[str, Any]]
LoginTokens = Callable[[str], Awaitable[str]]
ExternalIds = Callable[[str, str, str], Awaitable[None]]

MAX_TITLE_LENGTH = 200
ALREADY_LINKED: Result = (
    409,
    {
        "error": "This Canvas account or Pangea account is already linked",
        "errcode": "ORG.PANGEA.LTI_ALREADY_LINKED",
    },
)
DISCLOSURE_OUTDATED: Result = (
    409,
    {
        "error": "The disclosure has changed",
        "errcode": "ORG.PANGEA.DISCLOSURE_OUTDATED",
    },
)


def _course_title(launch: Launch) -> Optional[str]:
    context = launch.claims.get(CLAIM_CONTEXT)
    title = context.get("title") if isinstance(context, dict) else None
    if not isinstance(title, str) or not title.strip():
        return None
    return title.strip()[:MAX_TITLE_LENGTH]


class LaunchRedirects:
    """The `on_launch` step B1's launch endpoint hands each verified launch."""

    def __init__(
        self,
        *,
        links: LtiLinkStore,
        invitations: StudentInvitationStore,
        claims: StudentClaims,
        login_tokens: LoginTokens,
        app_base_url: str,
        admin_dash_base_url: str,
        clock_ms: Callable[[], int] = now_ms,
    ) -> None:
        self._links = links
        self._invitations = invitations
        self._claims = claims
        self._login_tokens = login_tokens
        self._app = app_base_url.rstrip("/")
        self._admin_dash_base_url = admin_dash_base_url
        self._clock_ms = clock_ms

    async def _ticket(
        self,
        kind: str,
        launch: Launch,
        nrps_url: Optional[str],
        bound_user_id: Optional[str],
    ) -> str:
        return await self._links.issue_ticket(
            kind,
            platform_id=launch.platform_id,
            issuer=launch.issuer,
            sub=launch.sub,
            context_id=launch.context_id,
            deployment_id=launch.deployment_id,
            nrps_url=nrps_url,
            bound_user_id=bound_user_id,
            now_ms=self._clock_ms(),
        )

    async def location(self, launch: Launch) -> str:
        """Where this launch goes (C5.2). Raises `LaunchRejected` for an
        instructor launch that carries no NRPS service: a connect would
        record a course whose roster can never be read."""
        user_id = await self._links.linked_user(launch.issuer, launch.sub)
        if launch.path == PATH_INSTRUCTOR:
            nrps_url = nrps_url_from_launch(launch.claims)
            if nrps_url is None:
                raise LaunchRejected("missing_nrps")
            if user_id is None:
                ticket = await self._ticket(KIND_INSTRUCTOR, launch, nrps_url, None)
                return f"{self._app}/lti/link?" + urlencode(
                    {"ticket": ticket, "role": "instructor"}
                )
            return await issue_connect_url(
                self._links,
                self._admin_dash_base_url,
                platform_id=launch.platform_id,
                issuer=launch.issuer,
                sub=launch.sub,
                context_id=launch.context_id,
                deployment_id=launch.deployment_id,
                nrps_url=nrps_url,
                user_id=user_id,
                now=self._clock_ms(),
            )
        if user_id is not None:
            identity = (launch.issuer, launch.context_id, launch.sub)
            matching = await self._invitations.canvas_invited(identity)
            confirmed = await self._invitations.acks_by(user_id)
            if all(row["id"] in confirmed for row in matching):
                # Every match is confirmed by this account, yet still Invited:
                # its claim did not complete (a failed join, say). Retry it on
                # that recorded confirmation, as ClaimByEmail does at sign-in.
                await self._retry_claims(matching, user_id, identity)
                token = await self._login_tokens(user_id)
                return f"{self._app}/lti/token?" + urlencode({"loginToken": token})
        # Not linked, or linked with invitations to confirm: the link page,
        # the ticket bound to the linked account if there is one.
        ticket = await self._ticket(KIND_LEARNER, launch, None, user_id)
        query = {"ticket": ticket}
        title = _course_title(launch)
        if title is not None:
            query["course"] = title
        return f"{self._app}/lti/link?" + urlencode(query)

    async def _retry_claims(
        self,
        rows: List[Dict[str, Any]],
        user_id: str,
        identity: Tuple[str, str, str],
    ) -> None:
        """Never raises: a claim that fails again is reported, and the
        launch still signs the student in."""
        for row in rows:
            try:
                outcome, _ = await self._claims.claim(
                    row["id"], user_id, canvas_identity=identity
                )
            except Exception as error:
                report_failure("LTI launch claim retry", error, invitation=row["id"])
                continue
            logger.info("LTI launch claim retry: invitation %s %s", row["id"], outcome)

    async def __call__(self, request: SynapseRequest, launch: Launch) -> None:
        try:
            location = await self.location(launch)
        except LaunchRejected as rejected:
            logger.info(
                "LTI launch refused: reason=%s platform=%s",
                rejected.code,
                launch.platform_id,
            )
            refuse_launch(request, rejected.status, rejected.code)
            return
        except Exception as error:
            report_failure("LTI launch redirect", error, platform=launch.platform_id)
            refuse_launch(request, 500, "internal_error")
            return
        logger.info(
            "LTI launch redirected: platform=%s path=%s to=%s",
            launch.platform_id,
            launch.path,
            location.split("?", 1)[0],
        )
        request.setHeader(b"Cache-Control", b"no-store")
        request.setHeader(b"Referrer-Policy", b"no-referrer")
        request.setHeader(b"Location", location.encode("ascii"))
        request.setResponseCode(302)
        finish_request(request)


class LinkStep:
    """`POST lti/link` (L1)."""

    def __init__(
        self,
        *,
        links: LtiLinkStore,
        invitations: StudentInvitationStore,
        claims: StudentClaims,
        course_links: CourseLinks,
        login_tokens: LoginTokens,
        external_ids: ExternalIds,
        clock_ms: Callable[[], int] = now_ms,
    ) -> None:
        self._links = links
        self._invitations = invitations
        self._claims = claims
        self._course_links = course_links
        self._login_tokens = login_tokens
        self._external_ids = external_ids
        self._clock_ms = clock_ms

    async def link(self, caller: Optional[str], body: Any) -> Result:
        """`caller` is the signed-in account, or None when no token was sent."""
        if not isinstance(body, dict):
            return bad("Request body must be a JSON object")
        ticket = ticket_arg(body.get("ticket"))
        if ticket is None:
            return bad("'ticket' is required")
        learner = "confirmed" in body or "disclosure_version" in body
        allowed = (
            {"ticket", "confirmed", "disclosure_version"} if learner else {"ticket"}
        )
        if set(body) - allowed:
            return bad("Unexpected fields")
        if learner:
            version = body.get("disclosure_version")
            if body.get("confirmed") is not True:
                return bad("'confirmed' must be true")
            if isinstance(version, bool) or not isinstance(version, int):
                return bad("'disclosure_version' is required")
            if version != MANAGED_DISCLOSURE_VERSION:
                return DISCLOSURE_OUTDATED
        kind = KIND_LEARNER if learner else KIND_INSTRUCTOR

        outcome, found = await self._links.consume_ticket(
            ticket, kind=kind, require_bound=caller is None, now_ms=self._clock_ms()
        )
        if outcome == TICKET_WRONG_KIND and found is not None:
            if found.kind in (KIND_LEARNER, KIND_INSTRUCTOR):
                # The other link kind: the body is wrong for it (a learner
                # ticket without the confirmation). Not consumed.
                return bad("This ticket needs the other form of the link step")
            return TICKET_INVALID
        if outcome == TICKET_UNBOUND:
            return 401, UNAUTHORIZED
        if outcome != TICKET_OK or found is None:
            return TICKET_INVALID

        if found.bound_user_id is not None:
            if caller is not None and caller != found.bound_user_id:
                logger.info(
                    "LTI link refused: platform=%s wrong account", found.platform_id
                )
                return TICKET_WRONG_ACCOUNT
            user_id = found.bound_user_id
        else:
            if caller is None:
                raise RuntimeError("unbound ticket consumed without a caller")
            user_id = caller
            linked = await self._links.link_user(
                found.issuer, found.sub, user_id, self._clock_ms()
            )
            if linked == CONFLICT:
                logger.info(
                    "LTI link refused: platform=%s already linked", found.platform_id
                )
                return ALREADY_LINKED
            await self._mirror(found, user_id)

        if found.kind == KIND_INSTRUCTOR:
            if found.nrps_url is None:
                raise RuntimeError("instructor ticket without an NRPS URL")
            url = await self._course_links.connect_url(
                platform_id=found.platform_id,
                issuer=found.issuer,
                sub=found.sub,
                context_id=found.context_id,
                deployment_id=found.deployment_id,
                nrps_url=found.nrps_url,
                user_id=user_id,
            )
            logger.info("LTI instructor linked: platform=%s", found.platform_id)
            return 200, {"next": "connect", "connect_url": url}

        claimed = await self._claim(found, user_id)
        login_token = await self._login_tokens(user_id) if caller is None else None
        logger.info(
            "LTI learner linked: platform=%s claimed=%d login_token=%s",
            found.platform_id,
            len(claimed),
            "issued" if login_token else "none",
        )
        return 200, {"next": "app", "claimed": claimed, "login_token": login_token}

    async def _mirror(self, ticket: Ticket, user_id: str) -> None:
        # Synapse's own external-id record of the link, for its admin tools.
        # The module's lti_user_link is what every step reads, so a failure
        # here leaves the link working; it is reported, with ids only.
        try:
            await self._external_ids(ticket.issuer, ticket.sub, user_id)
        except Exception as error:
            report_failure("LTI external id record", error, platform=ticket.platform_id)

    async def _claim(self, ticket: Ticket, user_id: str) -> List[Dict[str, str]]:
        """Confirm and claim the invitations imported with exactly this
        Canvas identity (C2.4): the identity is the match, never an email."""
        identity = (ticket.issuer, ticket.context_id, ticket.sub)
        claimed: List[Dict[str, str]] = []
        for row in await self._invitations.canvas_invited(identity):
            await self._invitations.record_ack(
                row["id"], user_id, MANAGED_DISCLOSURE_VERSION, now_ms()
            )
            try:
                outcome, _ = await self._claims.claim(
                    row["id"], user_id, canvas_identity=identity
                )
            except Exception as error:
                report_failure("LTI link claim", error, invitation=row["id"])
                continue
            if outcome == CLAIMED:
                claimed.append(
                    {"invitation_id": row["id"], "room_id": row["course_room_id"]}
                )
            else:
                logger.info(
                    "LTI link: invitation %s not claimed: %s", row["id"], outcome
                )
        return claimed
