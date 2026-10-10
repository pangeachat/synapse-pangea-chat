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
| Learner, linked | its matching invitations claimed, then `<app>/lti/token?loginToken=<token>` |
| Learner, linked, a Synapse server admin | `<app>/home/login` (own sign-in; no token, no ticket, no claim) |
| Instructor/Administrator, not linked | `<app>/lti/link?ticket=<instructor ticket>&role=instructor` |
| Instructor/Administrator, linked | `<admin-dash>/canvas-connect?ticket=<connect ticket>` |

The link step (`LinkStep`, `POST lti/link`, body `{ticket}`) needs the
account's own token, checks the body, then consumes the ticket in one update,
whatever happens after; any refusal after that writes nothing. It links the
caller's own account; a learner ticket then claims only the invitations
imported with exactly the ticket's (issuer, Canvas course, `sub`). No
checkbox: managed-account consent is in Pangea's Terms (seats amendment
2026-10-10).
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from synapse.http.server import finish_request
from synapse.http.site import SynapseRequest

from synapse_pangea_chat.lti.course_link import (
    TICKET_INVALID,
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
MintToken = Callable[[str], Awaitable[str]]
ExternalIds = Callable[[str, str, str], Awaitable[None]]

MAX_TITLE_LENGTH = 200
# The client's normal sign-in page.
SIGN_IN_PATH = "/home/login"
ALREADY_LINKED: Result = (
    409,
    {
        "error": "This Canvas account or Pangea account is already linked",
        "errcode": "ORG.PANGEA.LTI_ALREADY_LINKED",
    },
)


def _course_title(launch: Launch) -> Optional[str]:
    context = launch.claims.get(CLAIM_CONTEXT)
    title = context.get("title") if isinstance(context, dict) else None
    if not isinstance(title, str) or not title.strip():
        return None
    return title.strip()[:MAX_TITLE_LENGTH]


class LoginTokens:
    """The only way this package issues a login token.

    Never for a Synapse server admin (owner decision 2026-10-09): an LTI
    platform's say-so must not open a homeserver admin account. The flag is
    read when the token would be issued, so an account promoted after its
    Canvas link is covered, and a failed read means no token (fail closed).
    A refusal is logged with its reason only.
    """

    def __init__(
        self, mint: MintToken, is_server_admin: Callable[[str], Awaitable[bool]]
    ) -> None:
        self._mint = mint
        self._is_server_admin = is_server_admin

    async def allowed(self, user_id: str) -> bool:
        try:
            admin = await self._is_server_admin(user_id)
        except Exception as error:
            logger.warning(
                "LTI login token refused: admin check failed (%s)",
                type(error).__name__,
            )
            return False
        if admin is not False:
            logger.info("LTI login token refused: server admin")
            return False
        return True

    async def issue(self, user_id: str) -> Optional[str]:
        """A login token, or None when the account may not get one."""
        if not await self.allowed(user_id):
            return None
        return await self._mint(user_id)


class LaunchRedirects:
    """The `on_launch` step B1's launch endpoint hands each verified launch."""

    def __init__(
        self,
        *,
        links: LtiLinkStore,
        invitations: StudentInvitationStore,
        claims: StudentClaims,
        login_tokens: "LoginTokens",
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
            # One admin decision per launch, made before anything is written:
            # a server admin signs in themself, with no token and no claim.
            token = await self._login_tokens.issue(user_id)
            if token is None:
                return f"{self._app}{SIGN_IN_PATH}"
            # The exact-identity rows (including any imported since the
            # link) are claimed now; the identity is the match.
            await self._claim_rows(launch, user_id)
            return f"{self._app}/lti/token?" + urlencode({"loginToken": token})
        ticket = await self._ticket(KIND_LEARNER, launch, None, None)
        query = {"ticket": ticket}
        title = _course_title(launch)
        if title is not None:
            query["course"] = title
        return f"{self._app}/lti/link?" + urlencode(query)

    async def _claim_rows(self, launch: Launch, user_id: str) -> None:
        """Never raises: a claim that fails is reported, and the launch
        still signs the student in (the next launch retries it)."""
        identity = (launch.issuer, launch.context_id, launch.sub)
        for row in await self._invitations.canvas_invited(identity):
            try:
                outcome, _ = await self._claims.claim(
                    row["id"], user_id, canvas_identity=identity
                )
            except Exception as error:
                report_failure("LTI launch claim", error, invitation=row["id"])
                continue
            logger.info("LTI launch claim: invitation %s %s", row["id"], outcome)

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
        external_ids: ExternalIds,
        clock_ms: Callable[[], int] = now_ms,
    ) -> None:
        self._links = links
        self._invitations = invitations
        self._claims = claims
        self._course_links = course_links
        self._external_ids = external_ids
        self._clock_ms = clock_ms

    async def link(self, caller: Optional[str], body: Any) -> Result:
        """`caller` is the signed-in account, or None when no token was sent."""
        if caller is None:
            # Every link ticket is unbound: only the account's own sign-in
            # links it. Not consumed.
            return 401, UNAUTHORIZED
        if not isinstance(body, dict) or set(body) != {"ticket"}:
            return bad("Expected exactly 'ticket'")
        ticket = ticket_arg(body.get("ticket"))
        if ticket is None:
            return bad("'ticket' is required")
        outcome, found = await self._links.consume_ticket(
            ticket,
            kinds=(KIND_LEARNER, KIND_INSTRUCTOR),
            now_ms=self._clock_ms(),
        )
        if outcome != TICKET_OK or found is None:
            return TICKET_INVALID
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
        logger.info(
            "LTI learner linked: platform=%s claimed=%d",
            found.platform_id,
            len(claimed),
        )
        return 200, {"next": "app", "claimed": claimed}

    async def _mirror(self, ticket: Ticket, user_id: str) -> None:
        # Synapse's own external-id record of the link, for its admin tools.
        # The module's lti_user_link is what every step reads, so a failure
        # here leaves the link working; it is reported, with ids only.
        try:
            await self._external_ids(ticket.issuer, ticket.sub, user_id)
        except Exception as error:
            report_failure("LTI external id record", error, platform=ticket.platform_id)

    async def _claim(self, ticket: Ticket, user_id: str) -> List[Dict[str, str]]:
        """Claim the invitations imported with exactly this Canvas identity
        (C2.4): the identity is the match, never an email."""
        identity = (ticket.issuer, ticket.context_id, ticket.sub)
        claimed: List[Dict[str, str]] = []
        for row in await self._invitations.canvas_invited(identity):
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
