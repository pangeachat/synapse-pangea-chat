"""Canvas course link, roster import and student launch link (lanes B2, B3;
CONTRACTS C5 and C2 T10-T11), over the real stores on an in-memory database.

The launch itself (LTI 1.3 validation) is B1's and is tested there; here a
verified `Launch` is handed to the redirect step. The NRPS HTTP client, the
login token and the Synapse external-id mirror are doubles; the real HTTP
client, Postgres, `create_login_token` and `m.login.token` are exercised in
test_canvas_roster_e2e.py.
"""

from __future__ import annotations

import io
import logging
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import jwt
from synapse.api.errors import InvalidClientTokenError

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.lti import nrps as nrps_module
from synapse_pangea_chat.lti.course_link import CourseLinks
from synapse_pangea_chat.lti.http import UpstreamError, next_link
from synapse_pangea_chat.lti.keys import parse_lti_config
from synapse_pangea_chat.lti.link_store import (
    KIND_CONNECT,
    KIND_INSTRUCTOR,
    KIND_LEARNER,
    LtiLinkStore,
)
from synapse_pangea_chat.lti.nrps import (
    MEMBERSHIP_MEDIA_TYPE,
    NRPS_CLAIM,
    NrpsClient,
)
from synapse_pangea_chat.lti.registration import NRPS_SCOPE
from synapse_pangea_chat.lti.routes import KIND_TICKET, LtiRoute
from synapse_pangea_chat.lti.store import LtiStore
from synapse_pangea_chat.lti.student_launch import (
    LaunchRedirects,
    LinkStep,
    LoginTokens,
)
from synapse_pangea_chat.lti.validation import (
    CLAIM_CONTEXT,
    PATH_INSTRUCTOR,
    PATH_LEARNER,
    Launch,
    LaunchRejected,
)
from synapse_pangea_chat.notice_delivery.rate_limit import SlidingWindowRateLimiter
from synapse_pangea_chat.student_invitations import report
from synapse_pangea_chat.student_invitations.accounts import Accounts
from synapse_pangea_chat.student_invitations.api import RATE_LIMITED, UNAUTHORIZED
from synapse_pangea_chat.student_invitations.invite_email import InviteMailer
from tests.test_student_invitations_unit import (
    OTHER,
    OTHER_ROOM,
    ROOM,
    STUDENT,
    TEACHER,
    THIRD,
    FakeMain,
    FakeRooms,
    Harness,
    V,
)

from .lti_platform_double import INSTRUCTOR, LEARNER, new_rsa_key, private_pem

ISSUER = "https://canvas.school.example"
OTHER_ISSUER = "https://canvas.other.example"
CLIENT_ID = "pangea-client-1"
DEPLOYMENT = "deployment-1"
CONTEXT = "canvas-course-7"
OTHER_CONTEXT = "canvas-course-9"
TOKEN_URL = ISSUER + "/login/oauth2/token"
NRPS_URL = ISSUER + "/api/lti/courses/7/names_and_roles"
OTHER_NRPS_URL = OTHER_ISSUER + "/api/lti/courses/9/names_and_roles"
APP = "https://app.example.test"
DASH = "https://admin.example.test"
SUB = "canvas-user-42"
TEACHER_SUB = "canvas-teacher-1"
OTHER_SUB = "canvas-user-77"
CANVAS_EMAIL = "canvas.student@school.example"
ACCESS_TOKEN = "nrps-access-token-sentinel-9d1c"
NOW = 1_700_000_000_000
TEN_MINUTES = 10 * 60 * 1000
_TOOL_PEM = private_pem(new_rsa_key())
TOOL_KEY = parse_lti_config({"private_key_pem": _TOOL_PEM}).signing_key
LEARNER_URI = LEARNER


def _member(
    user_id: str,
    email: Optional[str],
    roles: Optional[List[str]] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    member: Dict[str, Any] = {
        "user_id": user_id,
        "roles": roles if roles is not None else [LEARNER_URI],
        "name": "Learner " + user_id,
    }
    if email is not None:
        member["email"] = email
    if status is not None:
        member["status"] = status
    return member


def _page(members: List[Dict[str, Any]], context: str = CONTEXT) -> Dict[str, Any]:
    return {"id": NRPS_URL, "context": {"id": context}, "members": members}


class FakeNrpsHttp:
    """The two platform calls NRPS makes: the token POST and the page GET."""

    def __init__(self) -> None:
        self.token_requests: List[Tuple[str, Dict[str, str]]] = []
        self.page_requests: List[Tuple[str, str, str]] = []
        self.token_response: Any = {
            "access_token": ACCESS_TOKEN,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": NRPS_SCOPE,
        }
        self.pages: Dict[str, Tuple[Any, Optional[str]]] = {}
        self.page_errors: Dict[str, UpstreamError] = {}

    async def post_form(self, url: str, fields: Dict[str, str]) -> Any:
        self.token_requests.append((url, dict(fields)))
        if isinstance(self.token_response, Exception):
            raise self.token_response
        return self.token_response

    async def get_page(
        self, url: str, bearer: str, accept: str
    ) -> Tuple[Any, Optional[str]]:
        self.page_requests.append((url, bearer, accept))
        if url in self.page_errors:
            raise self.page_errors[url]
        if url not in self.pages:
            raise UpstreamError("status_404")
        return self.pages[url]


class FakeLoginTokens:
    def __init__(self) -> None:
        self.issued: List[Tuple[str, str]] = []

    async def __call__(self, user_id: str) -> str:
        token = f"login-token-{len(self.issued) + 1}"
        self.issued.append((user_id, token))
        return token


class FakeServerAdmins:
    """Synapse's server-admin flag, read at the moment a token is issued."""

    def __init__(self) -> None:
        self.admins: set = set()
        self.error: Optional[Exception] = None
        self.checks: List[str] = []
        #: Answers for the next checks, in order (True, False or an
        #: exception), before falling back to `admins` / `error`: a flag that
        #: flips between two reads in one request.
        self.script: List[Any] = []

    async def __call__(self, user_id: str) -> bool:
        self.checks.append(user_id)
        if self.script:
            answer = self.script.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return bool(answer)
        if self.error is not None:
            raise self.error
        return user_id in self.admins


class FakeExternalIds:
    def __init__(self) -> None:
        self.recorded: List[Tuple[str, str, str]] = []

    async def __call__(self, issuer: str, sub: str, user_id: str) -> None:
        self.recorded.append((issuer, sub, user_id))


class Canvas:
    def __init__(self) -> None:
        self.h = Harness()
        pool = self.h.main.db_pool
        self.now = NOW
        self.platforms = LtiStore(pool)
        self.links = LtiLinkStore(pool)
        self.http = FakeNrpsHttp()
        self.nrps = NrpsClient(self.http, TOOL_KEY, lambda: self.now / 1000)
        self.login_tokens = FakeLoginTokens()
        self.server_admins = FakeServerAdmins()
        tokens = LoginTokens(self.login_tokens, self.server_admins)
        self.external_ids = FakeExternalIds()
        self.course_links = CourseLinks(
            links=self.links,
            platforms=self.platforms,
            invitations=self.h.store,
            admins=self.h.admins,
            nrps=self.nrps,
            admin_dash_base_url=DASH,
            clock_ms=lambda: self.now,
        )
        self.redirects = LaunchRedirects(
            links=self.links,
            invitations=self.h.store,
            claims=self.h.claims,
            login_tokens=tokens,
            app_base_url=APP,
            admin_dash_base_url=DASH,
            clock_ms=lambda: self.now,
        )
        self.link_step = LinkStep(
            links=self.links,
            invitations=self.h.store,
            claims=self.h.claims,
            course_links=self.course_links,
            login_tokens=tokens,
            external_ids=self.external_ids,
            clock_ms=lambda: self.now,
        )
        self.platform_ids: Dict[str, str] = {}

    async def platform(self, issuer: str = ISSUER, client_id: str = CLIENT_ID) -> str:
        platform_id = await self.platforms.create_platform(
            issuer=issuer,
            client_id=client_id,
            auth_login_url=issuer + "/api/lti/authorize_redirect",
            token_url=issuer + "/login/oauth2/token",
            jwks_uri=issuer + "/api/lti/security/jwks",
            product_family="canvas",
            deployment_id=DEPLOYMENT,
            now_ms=self.now,
        )
        await self.platforms.approve(platform_id, operator="@op:x", now_ms=self.now)
        self.platform_ids[issuer] = platform_id
        return platform_id

    def launch(
        self,
        *,
        issuer: str = ISSUER,
        sub: Optional[str] = None,
        context: str = CONTEXT,
        roles: Tuple[str, ...] = (LEARNER,),
        title: Optional[str] = "Spanish 1",
        nrps_url: Optional[str] = NRPS_URL,
    ) -> Launch:
        if sub is None:
            sub = TEACHER_SUB if INSTRUCTOR in roles else SUB
        claims: Dict[str, Any] = {
            "iss": issuer,
            "sub": sub,
            "email": CANVAS_EMAIL,
            CLAIM_CONTEXT: {"id": context},
        }
        if title is not None:
            claims[CLAIM_CONTEXT]["title"] = title
        if nrps_url is not None:
            claims[NRPS_CLAIM] = {
                "context_memberships_url": nrps_url,
                "service_versions": ["2.0"],
            }
        path = PATH_INSTRUCTOR if INSTRUCTOR in roles else PATH_LEARNER
        return Launch(
            platform_id=self.platform_ids[issuer],
            issuer=issuer,
            sub=sub,
            deployment_id=DEPLOYMENT,
            context_id=context,
            roles=roles,
            path=path,
            claims=claims,
        )

    async def go(self, launch: Launch) -> Tuple[str, Dict[str, str]]:
        """The launch's redirect: (scheme://host/path, query)."""
        location = await self.redirects.location(launch)
        parts = urlsplit(location)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        return f"{parts.scheme}://{parts.netloc}{parts.path}", query

    async def ticket(self, launch: Launch) -> str:
        _, query = await self.go(launch)
        return query["ticket"]

    async def l1(
        self, caller: Optional[str], ticket: str, **body: Any
    ) -> Tuple[int, Dict[str, Any]]:
        return await self.link_step.link(caller, {"ticket": ticket, **body})

    async def learner_l1(
        self, caller: Optional[str], ticket: str
    ) -> Tuple[int, Dict[str, Any]]:
        return await self.l1(caller, ticket, confirmed=True, disclosure_version=V)

    async def connect(
        self, caller: str, ticket: str, room: str = ROOM, **extra: Any
    ) -> Tuple[int, Dict[str, Any]]:
        return await self.course_links.connect(
            caller, {"ticket": ticket, "room_id": room, **extra}
        )

    async def connect_ticket(self, teacher: str = TEACHER, **launch: Any) -> str:
        """A connect ticket for `teacher`: an instructor launch, then the
        instructor link step the first time."""
        target, query = await self.go(self.launch(roles=(INSTRUCTOR,), **launch))
        if target == DASH + "/canvas-connect":
            return query["ticket"]
        status, body = await self.l1(teacher, query["ticket"])
        assert status == 200, body
        return parse_qs(urlsplit(body["connect_url"]).query)["ticket"][0]

    async def connected(self, room: str = ROOM, **launch: Any) -> None:
        status, body = await self.connect(
            TEACHER, await self.connect_ticket(**launch), room
        )
        assert status == 200, body

    async def import_(self, caller: str = TEACHER, room: str = ROOM) -> Tuple[int, Any]:
        return await self.course_links.import_roster(caller, {"room_id": room})

    async def status(self, caller: str = TEACHER, room: str = ROOM) -> Tuple[int, Any]:
        return await self.course_links.status(caller, {"room_id": room})

    def serve(self, *pages: List[Dict[str, Any]], context: str = CONTEXT) -> None:
        """NRPS pages at NRPS_URL, NRPS_URL?page=2, ..., linked by rel=next."""
        urls = [NRPS_URL] + [f"{NRPS_URL}?page={i + 2}" for i in range(len(pages) - 1)]
        for i, members in enumerate(pages):
            following = urls[i + 1] if i + 1 < len(urls) else None
            self.http.pages[urls[i]] = (_page(members, context), following)

    async def rows(self, room: str = ROOM) -> List[Dict[str, Any]]:
        return await self.h.store.list_room(room)

    async def table(self, name: str) -> List[Tuple[Any, ...]]:
        def select(txn: Any) -> List[Tuple[Any, ...]]:
            txn.execute(f"SELECT * FROM {name}")
            return list(txn.fetchall())

        return await self.h.main.db_pool.runInteraction("test_table", select)


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.c = Canvas()
        await self.c.platform()
        await self.c.h.store.ensure()
        await self.c.links.ensure()

    async def written(self) -> Tuple[Any, ...]:
        """Every link, claim, confirmation, managed record, external id and
        login token a link or connect step could write. The ticket's own
        consumption is left out on purpose: C5.1 consumes it on every
        request that passes body validation, whatever the outcome after
        that, and the tests assert it explicitly (a later use gets 410)."""
        rows = await self.c.h.store.list_room(ROOM) + await self.c.h.store.list_room(
            OTHER_ROOM
        )
        return (
            await self.c.table("lti_user_link"),
            await self.c.table("lti_course_link"),
            list(self.c.external_ids.recorded),
            list(self.c.h.joiner.joins),
            await self.c.table("pangea_invitation_ack"),
            await self.c.table("pangea_managed_account"),
            list(self.c.login_tokens.issued),
            sorted((r["id"], r["state"], r["claimant"]) for r in rows),
        )

    async def canvas_row(
        self,
        email: str = "student@school.example",
        sub: str = SUB,
        context: str = CONTEXT,
        room: str = ROOM,
    ) -> Dict[str, Any]:
        """An invited row imported from Canvas with this identity."""
        self.c.serve([_member(sub, email)], context=context)
        await self.c.connected(room, context=context)
        status, body = await self.c.import_(room=room)
        self.assertEqual(status, 200, body)
        [row] = [r for r in await self.c.rows(room) if r["lti_user_id"] == sub]
        return row

    async def linked(self, user: str = STUDENT, sub: str = SUB) -> None:
        """`user` linked to `sub` by a first launch and their own sign-in."""
        status, body = await self.c.learner_l1(
            user, await self.c.ticket(self.c.launch(sub=sub, context="ctx-none"))
        )
        self.assertEqual(status, 200, body)


# --- B2: course link ---------------------------------------------------------


class TestInstructorLink(_Base):
    async def test_first_instructor_launch_requires_own_sign_in_before_link(self):
        target, query = await self.c.go(self.c.launch(roles=(INSTRUCTOR,)))
        self.assertEqual(target, APP + "/lti/link")
        self.assertEqual(query["role"], "instructor")
        self.assertNotIn("loginToken", query)
        self.assertNotIn("course", query)
        ticket = query["ticket"]
        self.assertGreaterEqual(len(ticket), 43)

        # No token: refused, nothing written, ticket not consumed.
        before = await self.written()
        status, body = await self.c.l1(None, ticket)
        self.assertEqual(status, 401, body)
        self.assertEqual(await self.written(), before)
        # The instructor then signs in as themself and returns it.
        status, body = await self.c.l1(TEACHER, ticket)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["next"], "connect")
        target = urlsplit(body["connect_url"])
        self.assertEqual(
            f"{target.scheme}://{target.netloc}{target.path}", DASH + "/canvas-connect"
        )
        self.assertEqual(await self.c.links.linked_user(ISSUER, TEACHER_SUB), TEACHER)
        self.assertEqual(self.c.external_ids.recorded, [(ISSUER, TEACHER_SUB, TEACHER)])
        # Linking never claims anything.
        self.assertEqual(self.c.h.joiner.joins, [])

    async def test_instructor_link_never_opens_an_existing_account_without_its_sign_in(
        self,
    ):
        # An account that owns the instructor's Canvas email exists.
        self.c.h.verify(OTHER, CANVAS_EMAIL)
        target, query = await self.c.go(self.c.launch(roles=(INSTRUCTOR,)))
        self.assertEqual(target, APP + "/lti/link")
        self.assertNotIn("loginToken", query)
        self.assertEqual(self.c.login_tokens.issued, [])
        # Whoever signs in is linked; the email's owner is never opened.
        status, body = await self.c.l1(TEACHER, query["ticket"])
        self.assertEqual(status, 200, body)
        self.assertEqual(await self.c.links.linked_user(ISSUER, TEACHER_SUB), TEACHER)
        self.assertEqual(self.c.login_tokens.issued, [])
        # The connect ticket it issues is bound to the signed-in account.
        ticket = parse_qs(urlsplit(body["connect_url"]).query)["ticket"][0]
        status, body = await self.c.connect(OTHER, ticket)
        self.assertEqual(status, 403, body)
        self.assertEqual(body["errcode"], "ORG.PANGEA.TICKET_WRONG_ACCOUNT")

    async def test_linked_instructor_launch_goes_to_admin_dash_with_a_bound_connect_ticket(
        self,
    ):
        await self.c.connect_ticket()
        target, query = await self.c.go(self.c.launch(roles=(INSTRUCTOR,)))
        self.assertEqual(target, DASH + "/canvas-connect")
        self.assertEqual(set(query), {"ticket"})
        self.assertEqual(self.c.login_tokens.issued, [])
        status, body = await self.c.connect(TEACHER, query["ticket"])
        self.assertEqual(status, 200, body)

    async def test_learner_role_launch_never_issues_an_instructor_or_connect_ticket(
        self,
    ):
        target, query = await self.c.go(self.c.launch(roles=(LEARNER,)))
        status, body = await self.c.connect(TEACHER, query["ticket"])
        self.assertEqual(status, 410, body)
        self.assertEqual(await self.c.table("lti_course_link"), [])

    async def test_instructor_launch_without_nrps_is_refused(self):
        with self.assertRaises(LaunchRejected) as caught:
            await self.c.go(self.c.launch(roles=(INSTRUCTOR,), nrps_url=None))
        self.assertEqual(caught.exception.code, "missing_nrps")
        with self.assertRaises(LaunchRejected):
            await self.c.go(
                self.c.launch(roles=(INSTRUCTOR,), nrps_url="http://plain.example/x")
            )


class TestConnect(_Base):
    async def test_connect_completes_only_for_the_tickets_account_with_pl100(self):
        # TEACHER's ticket, TEACHER not PL100 in the chosen course: refused,
        # consumed, nothing linked.
        self.c.h.admins.admins.discard((ROOM, TEACHER))
        ticket = await self.c.connect_ticket()
        status, body = await self.c.connect(TEACHER, ticket)
        self.assertEqual((status, body["errcode"]), (403, "M_FORBIDDEN"))
        self.assertEqual(await self.c.table("lti_course_link"), [])
        self.c.h.admins.admins.add((ROOM, TEACHER))
        status, body = await self.c.connect(TEACHER, ticket)
        self.assertEqual(status, 410, body)
        # A fresh ticket, PL100: connected.
        status, body = await self.c.connect(TEACHER, await self.c.connect_ticket())
        self.assertEqual((status, body), (200, {"status": "connected"}))
        [link] = await self.c.table("lti_course_link")
        self.assertIn(ROOM, link)
        self.assertIn(CONTEXT, link)
        self.assertIn(NRPS_URL, link)

    async def test_connect_ticket_completed_by_a_different_signed_in_account_is_rejected(
        self,
    ):
        self.c.h.admins.admins.add((ROOM, OTHER))
        ticket = await self.c.connect_ticket()
        before = await self.written()
        status, body = await self.c.connect(OTHER, ticket)
        self.assertEqual(status, 403, body)
        self.assertEqual(body["errcode"], "ORG.PANGEA.TICKET_WRONG_ACCOUNT")
        self.assertEqual(await self.written(), before)
        # Consumed by that attempt: the rightful account relaunches.
        status, body = await self.c.connect(TEACHER, ticket)
        self.assertEqual(status, 410, body)
        self.assertEqual(await self.c.table("lti_course_link"), [])

    async def test_expired_connect_ticket_is_rejected_and_writes_no_course_link(self):
        ticket = await self.c.connect_ticket()
        self.c.now += TEN_MINUTES
        status, body = await self.c.connect(TEACHER, ticket)
        self.assertEqual((status, body["errcode"]), (410, "ORG.PANGEA.TICKET_INVALID"))
        self.assertEqual(await self.c.table("lti_course_link"), [])
        # One millisecond before expiry it would have worked.
        ticket = await self.c.connect_ticket()
        self.c.now += TEN_MINUTES - 1
        status, body = await self.c.connect(TEACHER, ticket)
        self.assertEqual(status, 200, body)

    async def test_replayed_connect_ticket_is_rejected_and_writes_no_course_link(self):
        ticket = await self.c.connect_ticket()
        status, body = await self.c.connect(TEACHER, ticket)
        self.assertEqual(status, 200, body)
        before = await self.c.table("lti_course_link")
        status, body = await self.c.connect(TEACHER, ticket, OTHER_ROOM)
        self.assertEqual((status, body["errcode"]), (410, "ORG.PANGEA.TICKET_INVALID"))
        self.assertEqual(await self.c.table("lti_course_link"), before)
        self.assertFalse(await self.c.links.is_connected(OTHER_ROOM))

    async def test_connect_ticket_cannot_link_a_different_canvas_context_than_the_launchs(
        self,
    ):
        ticket = await self.c.connect_ticket()
        # A context, issuer or NRPS URL in the body is refused, not used, and
        # the ticket stays usable.
        for extra in (
            {"context_id": OTHER_CONTEXT},
            {"issuer": OTHER_ISSUER},
            {"nrps_url": OTHER_NRPS_URL},
        ):
            status, body = await self.c.connect(TEACHER, ticket, **extra)
            self.assertEqual((status, body["errcode"]), (400, "M_INVALID_PARAM"))
        self.assertEqual(await self.c.table("lti_course_link"), [])
        status, body = await self.c.connect(TEACHER, ticket)
        self.assertEqual(status, 200, body)
        link = await self.c.links.course_link(ROOM)
        assert link is not None
        self.assertEqual(
            (link.issuer, link.context_id, link.deployment_id, link.nrps_url),
            (ISSUER, CONTEXT, DEPLOYMENT, NRPS_URL),
        )

    async def test_one_canvas_context_links_one_pangea_course(self):
        await self.c.connected(ROOM)
        # The same pair again: 200.
        status, body = await self.c.connect(TEACHER, await self.c.connect_ticket())
        self.assertEqual(status, 200, body)
        # The same context to another course, or another context to this course.
        status, body = await self.c.connect(
            TEACHER, await self.c.connect_ticket(), OTHER_ROOM
        )
        self.assertEqual(
            (status, body["errcode"]), (409, "ORG.PANGEA.LTI_ALREADY_CONNECTED")
        )
        status, body = await self.c.connect(
            TEACHER, await self.c.connect_ticket(context=OTHER_CONTEXT), ROOM
        )
        self.assertEqual(
            (status, body["errcode"]), (409, "ORG.PANGEA.LTI_ALREADY_CONNECTED")
        )
        self.assertEqual(len(await self.c.table("lti_course_link")), 1)

    async def test_connect_rejects_malformed_bodies_without_consuming(self):
        ticket = await self.c.connect_ticket()
        for body in (
            None,
            [],
            {"ticket": ticket},
            {"room_id": ROOM},
            {"ticket": ticket, "room_id": "not-a-room"},
            {"ticket": 7, "room_id": ROOM},
            {"ticket": "x" * 500, "room_id": ROOM},
        ):
            status, answer = await self.c.course_links.connect(TEACHER, body)
            self.assertEqual(status, 400, (body, answer))
        status, answer = await self.c.connect(TEACHER, ticket)
        self.assertEqual(status, 200, answer)

    async def test_a_learner_or_instructor_link_ticket_is_no_connect_ticket(self):
        learner = await self.c.ticket(self.c.launch())
        instructor = await self.c.ticket(self.c.launch(roles=(INSTRUCTOR,)))
        for ticket in (learner, instructor, "unknown-ticket"):
            status, body = await self.c.connect(TEACHER, ticket)
            self.assertEqual(status, 410, body)
        # A linked learner's bound ticket, presented by that same account,
        # which administers the course: a Learner launch never connects.
        bound_learner = await self.c.links.issue_ticket(
            KIND_LEARNER,
            platform_id=self.c.platform_ids[ISSUER],
            issuer=ISSUER,
            sub=SUB,
            context_id=CONTEXT,
            deployment_id=DEPLOYMENT,
            nrps_url=NRPS_URL,
            bound_user_id=TEACHER,
            now_ms=self.c.now,
        )
        status, body = await self.c.connect(TEACHER, bound_learner)
        self.assertEqual(status, 410, body)
        self.assertEqual(await self.c.table("lti_course_link"), [])


class TestCanvasStatus(_Base):
    async def test_canvas_status_is_unconnected_for_a_pl100_admin_of_an_unlinked_course_and_connected_after_link(
        self,
    ):
        self.assertEqual(await self.c.status(), (200, {"status": "unconnected"}))
        await self.c.connected()
        self.assertEqual(await self.c.status(), (200, {"status": "connected"}))
        self.assertEqual(
            await self.c.status(room=OTHER_ROOM), (200, {"status": "unconnected"})
        )

    async def test_canvas_status_refuses_a_caller_who_is_not_pl100_in_that_course(
        self,
    ):
        await self.c.connected()
        status, body = await self.c.status(caller=STUDENT)
        self.assertEqual((status, body["errcode"]), (403, "M_FORBIDDEN"))
        # One body for an unknown room as well: no oracle.
        status, unknown = await self.c.status(caller=STUDENT, room="!nope:x")
        self.assertEqual((status, unknown), (403, body))
        status, body = await self.c.course_links.status(TEACHER, {"room_id": "x"})
        self.assertEqual(status, 400, body)


class TestInviteEmailCanvasLine(unittest.IsolatedAsyncioTestCase):
    async def test_invite_email_for_a_canvas_connected_course_adds_the_open_from_canvas_line_and_nothing_else(
        self,
    ):
        c = Canvas()
        await c.platform()
        await c.connected(ROOM)
        rendered: List[Dict[str, Any]] = []

        class Template:
            def render(self, **kwargs: Any) -> str:
                rendered.append(kwargs)
                return "body"

        api = MagicMock()
        api.read_templates.return_value = [Template(), Template()]
        sent: List[Dict[str, Any]] = []

        async def send_email(**kwargs: Any) -> None:
            sent.append(kwargs)

        api._hs.get_send_email_handler.return_value.send_email = send_email
        api._hs.config.email.email_app_name = "Pangea Chat"
        main = FakeMain()
        api._hs.get_datastores.return_value.main = main
        rooms = FakeRooms(main)
        rooms.names[OTHER_ROOM] = rooms.names[ROOM]
        mailer = InviteMailer(
            api, PangeaChatConfig(app_base_url=APP), rooms, Accounts(api)
        )
        mailer.canvas_connected = c.course_links.is_connected
        row = {
            "id": "inv-1",
            "email": "s@school.example",
            "email_key": "s@school.example",
            "invited_by": TEACHER,
        }
        await mailer.send_invite({**row, "course_room_id": ROOM}, "cl4sscd")
        await mailer.send_invite({**row, "course_room_id": OTHER_ROOM}, "cl4sscd")
        connected_html, connected_text, plain_html, plain_text = rendered
        self.assertIs(connected_html["canvas_connected"], True)
        self.assertIs(plain_html["canvas_connected"], False)
        for connected, plain in (
            (connected_html, plain_html),
            (connected_text, plain_text),
        ):
            differing = {k for k in connected if connected[k] != plain.get(k)}
            # The course description differs only because FakeRooms gives
            # one course a topic; the Canvas flag is the only other change.
            self.assertEqual(differing - {"course_description"}, {"canvas_connected"})
        self.assertEqual(sent[0]["subject"], sent[1]["subject"])


# --- B2: roster import -----------------------------------------------------


class TestImport(_Base):
    async def test_import_requires_pl100_in_the_linked_pangea_course_on_every_request_and_rejects_non_admins_without_fetching_nrps(
        self,
    ):
        await self.c.connected()
        self.c.serve([_member(SUB, "a@school.example")])
        status, body = await self.c.import_()
        self.assertEqual(status, 200, body)
        requests_before = (
            len(self.c.http.token_requests),
            len(self.c.http.page_requests),
        )
        # The linking teacher loses PL100: the next import is refused before
        # any NRPS call.
        self.c.h.admins.admins.discard((ROOM, TEACHER))
        status, body = await self.c.import_()
        self.assertEqual((status, body["errcode"]), (403, "M_FORBIDDEN"))
        for caller in (STUDENT, OTHER):
            status, body = await self.c.import_(caller=caller)
            self.assertEqual((status, body["errcode"]), (403, "M_FORBIDDEN"))
        self.assertEqual(
            (len(self.c.http.token_requests), len(self.c.http.page_requests)),
            requests_before,
        )
        # A co-admin with PL100 may import.
        self.c.h.admins.admins.add((ROOM, OTHER))
        status, body = await self.c.import_(caller=OTHER)
        self.assertEqual(status, 200, body)

    async def test_import_of_an_unconnected_course_is_409_without_nrps(self):
        status, body = await self.c.import_()
        self.assertEqual(
            (status, body["errcode"]), (409, "ORG.PANGEA.LTI_NOT_CONNECTED")
        )
        self.assertEqual(self.c.http.token_requests, [])

    async def test_import_of_a_new_canvas_learner_with_an_email_creates_an_invited_row_with_source_canvas_and_its_lti_identity(
        self,
    ):
        await self.c.connected()
        self.c.serve([_member(SUB, " New.Learner@School.example ")])
        status, body = await self.c.import_()
        self.assertEqual(
            (status, body),
            (
                200,
                {
                    "imported": 1,
                    "attached": 0,
                    "unchanged": 0,
                    "conflicts": [],
                    "no_email": 0,
                },
            ),
        )
        [row] = await self.c.rows()
        self.assertEqual(row["state"], "invited")
        self.assertEqual(row["source"], "canvas")
        self.assertEqual(row["email"], "New.Learner@School.example")
        self.assertEqual(row["email_key"], "new.learner@school.example")
        self.assertEqual(row["invited_by"], TEACHER)
        self.assertEqual(
            (row["lti_issuer"], row["lti_context_id"], row["lti_user_id"]),
            (ISSUER, CONTEXT, SUB),
        )
        self.assertEqual(row["send_count"], 0)
        # Re-import is safe: nothing new.
        status, body = await self.c.import_()
        self.assertEqual((body["imported"], body["unchanged"]), (0, 1))
        self.assertEqual(len(await self.c.rows()), 1)

    async def test_import_gives_a_joined_member_without_an_invitation_an_invited_row_with_canvas_identity(
        self,
    ):
        self.c.h.main.membership[(ROOM, STUDENT)] = "join"
        self.c.h.verify(STUDENT, "member@school.example")
        await self.c.connected()
        self.c.serve([_member(SUB, "member@school.example")])
        status, body = await self.c.import_()
        self.assertEqual(body["imported"], 1, body)
        [row] = await self.c.rows()
        self.assertEqual(
            (row["state"], row["source"], row["lti_user_id"]),
            ("invited", "canvas", SUB),
        )
        self.assertIsNone(row["claimant"])
        # Nothing claimed by the import itself.
        self.assertEqual(self.c.h.joiner.joins, [])
        self.assertEqual(await self.c.table("pangea_managed_account"), [])

    async def test_import_attaches_identity_to_existing_row_no_duplicate(self):
        pasted, joined, revoked = await self.c.h.add(
            "pasted@school.example", "joined@school.example", "revoked@school.example"
        )
        self.c.h.verify(STUDENT, "joined@school.example")
        await self.c.h.confirm(STUDENT, joined["invitation_id"])
        await self.c.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": revoked["invitation_id"]}
        )
        await self.c.connected()
        self.c.serve(
            [
                _member("sub-pasted", "PASTED@school.example"),
                _member("sub-joined", "joined@school.example"),
                _member("sub-revoked", "revoked@school.example"),
            ]
        )
        status, body = await self.c.import_()
        self.assertEqual(
            (body["imported"], body["attached"], body["unchanged"]), (0, 3, 0), body
        )
        rows = {r["id"]: r for r in await self.c.rows()}
        self.assertEqual(len(rows), 3)
        for inv, sub, state in (
            (pasted, "sub-pasted", "invited"),
            (joined, "sub-joined", "joined"),
            (revoked, "sub-revoked", "revoked"),
        ):
            row = rows[inv["invitation_id"]]
            # Identity attached; state, source and the teacher's email kept.
            self.assertEqual(row["lti_user_id"], sub)
            self.assertEqual(row["lti_context_id"], CONTEXT)
            self.assertEqual(row["state"], state)
            self.assertEqual(row["source"], "manual")
        self.assertEqual(
            rows[pasted["invitation_id"]]["email"], "pasted@school.example"
        )
        # Second import: all unchanged, and the email of a bound row is never
        # rewritten even when Canvas now reports another one.
        self.c.serve(
            [
                _member("sub-pasted", "changed@school.example"),
                _member("sub-joined", "joined@school.example"),
                _member("sub-revoked", "revoked@school.example"),
            ]
        )
        status, body = await self.c.import_()
        self.assertEqual((body["imported"], body["unchanged"]), (0, 3), body)
        self.assertEqual(
            (await self.c.h.row(pasted["invitation_id"]))["email_key"],
            "pasted@school.example",
        )

    async def test_row_bound_to_another_canvas_user_reported_as_conflict(self):
        (inv,) = await self.c.h.add("shared@school.example")
        await self.c.connected()
        self.c.serve([_member("sub-first", "shared@school.example")])
        await self.c.import_()
        self.c.serve(
            [
                _member("sub-first", "shared@school.example"),
                _member("sub-second", "shared@school.example"),
            ]
        )
        status, body = await self.c.import_()
        self.assertEqual(body["conflicts"], [inv["invitation_id"]], body)
        # The conflicting learner leaves the row unchanged, and is counted so.
        self.assertEqual(body["unchanged"], 2, body)
        row = await self.c.h.row(inv["invitation_id"])
        self.assertEqual(row["lti_user_id"], "sub-first")
        self.assertEqual(len(await self.c.rows()), 1)

    async def test_no_email_learners_counted_not_guessed(self):
        self.c.h.verify(STUDENT, "student@school.example")
        await self.c.connected()
        self.c.serve(
            [
                _member("sub-a", None),
                _member("sub-b", ""),
                _member("sub-c", "not an email"),
                _member("sub-d", "d@school.example"),
            ]
        )
        status, body = await self.c.import_()
        self.assertEqual((body["no_email"], body["imported"]), (3, 1), body)
        self.assertEqual({r["lti_user_id"] for r in await self.c.rows()}, {"sub-d"})

    async def test_only_active_learners_are_imported(self):
        await self.c.connected()
        self.c.serve(
            [
                _member("sub-teacher", "t@school.example", roles=[INSTRUCTOR]),
                _member("sub-gone", "g@school.example", status="Inactive"),
                _member("sub-short", "s@school.example", roles=["Learner"]),
                _member("sub-active", "a@school.example", status="Active"),
                {"roles": [LEARNER_URI], "email": "noid@school.example"},
            ]
        )
        status, body = await self.c.import_()
        self.assertEqual(body["imported"], 2, body)
        self.assertEqual(
            {r["lti_user_id"] for r in await self.c.rows()},
            {"sub-short", "sub-active"},
        )

    async def test_concurrent_import_converges_instead_of_failing(self):
        """Another import commits the same learner between this import's read
        and its insert: the unique index refuses the insert, and the import
        re-reads and reports the row instead of failing."""
        await self.c.connected()
        self.c.serve([_member(SUB, "race@school.example")])
        pool = self.c.h.main.db_pool
        fired: List[bool] = []

        def concurrent_import(sql: str, args: Any) -> None:
            if fired or "lti_issuer, lti_context_id, lti_user_id)" not in sql:
                return
            fired.append(True)
            pool.connection.execute(
                "INSERT INTO pangea_student_invitation (id, course_room_id,"
                " email_key, email, state, source, invited_by, send_count,"
                " created_at_ms, lti_issuer, lti_context_id, lti_user_id)"
                " VALUES ('raced', ?, 'race@school.example', 'race@school.example',"
                " 'invited', 'canvas', ?, 0, 1, ?, ?, ?)",
                (ROOM, OTHER, ISSUER, CONTEXT, SUB),
            )
            pool.connection.commit()

        pool.on_statement = concurrent_import
        try:
            status, body = await self.c.import_()
        finally:
            pool.on_statement = None
        self.assertEqual(fired, [True])
        self.assertEqual(status, 200, body)
        self.assertEqual((body["imported"], body["unchanged"]), (0, 1), body)
        self.assertEqual([r["id"] for r in await self.c.rows()], ["raced"])

    async def test_import_sends_no_email(self):
        await self.c.connected()
        self.c.serve([_member(SUB, "a@school.example")])
        await self.c.import_()
        self.assertEqual(self.c.h.mailer.sent, [])


class TestNrps(_Base):
    async def test_nrps_follows_paging(self):
        await self.c.connected()
        self.c.serve(
            [_member("sub-1", "1@school.example")],
            [_member("sub-2", "2@school.example")],
            [_member("sub-3", "3@school.example")],
        )
        status, body = await self.c.import_()
        self.assertEqual(body["imported"], 3, body)
        self.assertEqual(
            [url for url, _, _ in self.c.http.page_requests],
            [NRPS_URL, NRPS_URL + "?page=2", NRPS_URL + "?page=3"],
        )
        self.assertEqual(len(self.c.http.token_requests), 1)
        for _, bearer, accept in self.c.http.page_requests:
            self.assertEqual(bearer, ACCESS_TOKEN)
            self.assertEqual(accept, MEMBERSHIP_MEDIA_TYPE)

    async def test_nrps_paging_loop_and_off_host_next_link_are_refused(self):
        await self.c.connected()
        self.c.http.pages[NRPS_URL] = (_page([]), NRPS_URL + "?page=2")
        self.c.http.pages[NRPS_URL + "?page=2"] = (_page([]), NRPS_URL)
        status, body = await self.c.import_()
        self.assertEqual((status, body["errcode"]), (502, "ORG.PANGEA.LTI_UPSTREAM"))
        # It stops at the first page it has already read, not at the cap.
        self.assertEqual(len(self.c.http.page_requests), 2)
        # A next link on another host never receives the token.
        self.c.http.page_requests.clear()
        self.c.http.pages[NRPS_URL] = (_page([]), OTHER_NRPS_URL)
        status, body = await self.c.import_()
        self.assertEqual(status, 502, body)
        self.assertEqual([u for u, _, _ in self.c.http.page_requests], [NRPS_URL])

    async def test_nrps_token_request_is_a_signed_client_credentials_jwt_for_the_platforms_token_url_and_scope(
        self,
    ):
        await self.c.connected()
        self.c.serve([])
        await self.c.import_()
        [(url, fields)] = self.c.http.token_requests
        self.assertEqual(url, TOKEN_URL)
        self.assertEqual(fields["grant_type"], "client_credentials")
        self.assertEqual(
            fields["client_assertion_type"],
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        )
        self.assertEqual(fields["scope"], NRPS_SCOPE)
        assertion = fields["client_assertion"]
        header = jwt.get_unverified_header(assertion)
        self.assertEqual((header["alg"], header["kid"]), ("RS256", TOOL_KEY.kid))
        claims = jwt.decode(
            assertion,
            key=jwt.PyJWK(TOOL_KEY.public_jwk).key,
            algorithms=["RS256"],
            audience=TOKEN_URL,
            options={"verify_exp": False, "verify_iat": False},
        )
        self.assertEqual((claims["iss"], claims["sub"]), (CLIENT_ID, CLIENT_ID))
        self.assertEqual(claims["iat"], NOW // 1000)
        self.assertLessEqual(claims["exp"] - claims["iat"], 300)
        self.assertGreaterEqual(len(claims["jti"]), 16)
        # A fresh jti every time.
        await self.c.import_()
        second = jwt.decode(
            self.c.http.token_requests[1][1]["client_assertion"],
            options={"verify_signature": False},
        )
        self.assertNotEqual(second["jti"], claims["jti"])

    async def test_nrps_refuses_a_bad_or_expired_token_response_without_retrying_forever(
        self,
    ):
        await self.c.connected()
        self.c.serve([_member(SUB, "a@school.example")])
        for response in (
            UpstreamError("status_401"),
            None,
            [],
            {},
            {"access_token": "", "token_type": "Bearer"},
            {"access_token": 7, "token_type": "Bearer"},
            {"access_token": ACCESS_TOKEN, "token_type": "mac"},
            {"access_token": ACCESS_TOKEN, "token_type": "Bearer", "expires_in": 0},
            {"access_token": ACCESS_TOKEN, "token_type": "Bearer", "expires_in": -5},
            {"access_token": ACCESS_TOKEN, "token_type": "Bearer", "expires_in": "x"},
        ):
            self.c.http.token_requests.clear()
            self.c.http.page_requests.clear()
            self.c.http.token_response = response
            status, body = await self.c.import_()
            self.assertEqual(
                (status, body["errcode"]), (502, "ORG.PANGEA.LTI_UPSTREAM"), response
            )
            self.assertEqual(len(self.c.http.token_requests), 1, response)
            self.assertEqual(self.c.http.page_requests, [], response)
        self.assertEqual(await self.c.rows(), [])
        # A page the platform refuses is not retried either.
        self.c.http.token_requests.clear()
        self.c.http.token_response = {
            "access_token": ACCESS_TOKEN,
            "token_type": "bearer",
        }
        self.c.http.page_errors[NRPS_URL] = UpstreamError("status_401")
        status, body = await self.c.import_()
        self.assertEqual(status, 502, body)
        self.assertEqual(len(self.c.http.page_requests), 1)
        self.assertEqual(len(self.c.http.token_requests), 1)

    async def test_nrps_uses_only_the_linked_contexts_issuer_and_deployment(self):
        other_platform = await self.c.platform(OTHER_ISSUER, "other-client")
        self.assertNotEqual(other_platform, self.c.platform_ids[ISSUER])
        await self.c.connected(ROOM)
        await self.c.connected(
            OTHER_ROOM,
            issuer=OTHER_ISSUER,
            context=OTHER_CONTEXT,
            nrps_url=OTHER_NRPS_URL,
        )
        self.c.serve([_member(SUB, "a@school.example")])
        self.c.http.pages[OTHER_NRPS_URL] = (
            _page([_member(OTHER_SUB, "b@school.example")], OTHER_CONTEXT),
            None,
        )
        await self.c.import_(room=OTHER_ROOM)
        [(token_url, fields)] = self.c.http.token_requests
        self.assertEqual(token_url, OTHER_ISSUER + "/login/oauth2/token")
        claims = jwt.decode(
            fields["client_assertion"], options={"verify_signature": False}
        )
        self.assertEqual(claims["iss"], "other-client")
        self.assertEqual([u for u, _, _ in self.c.http.page_requests], [OTHER_NRPS_URL])
        [row] = await self.c.rows(OTHER_ROOM)
        self.assertEqual(
            (row["lti_issuer"], row["lti_context_id"], row["lti_user_id"]),
            (OTHER_ISSUER, OTHER_CONTEXT, OTHER_SUB),
        )
        self.assertEqual(await self.c.rows(ROOM), [])
        # A page that does not name the linked context is refused: another
        # context, no context, or a context without an id.
        for context in ({"id": OTHER_CONTEXT}, None, {}):
            page: Dict[str, Any] = {"members": [_member(SUB, "a@school.example")]}
            if context is not None:
                page["context"] = context
            self.c.http.pages[NRPS_URL] = (page, None)
            status, body = await self.c.import_(room=ROOM)
            self.assertEqual(status, 502, (context, body))
            self.assertEqual(await self.c.rows(ROOM), [])
        # A page that names another context than the linked one is refused.
        self.c.http.pages[NRPS_URL] = (
            _page([_member(SUB, "a@school.example")], OTHER_CONTEXT),
            None,
        )
        status, body = await self.c.import_(room=ROOM)
        self.assertEqual(status, 502, body)
        self.assertEqual(await self.c.rows(ROOM), [])

    async def test_nrps_failure_logs_and_sentry_carry_no_key_token_or_email(self):
        captured = _capture()
        self.addCleanup(captured.detach)
        await self.c.connected()
        with patch.object(report, "sentry_sdk") as sentry:
            self.c.serve([_member(SUB, "secret.learner@school.example")])
            self.c.http.page_errors[NRPS_URL] = UpstreamError("status_500")
            status, _ = await self.c.import_()
            self.assertEqual(status, 502)
            self.c.http.token_response = UpstreamError("unreachable")
            status, _ = await self.c.import_()
            self.assertEqual(status, 502)
            self.c.http.token_response = {"access_token": ACCESS_TOKEN}
            status, _ = await self.c.import_()
            self.assertEqual(status, 502)
        text = captured.text()
        self.assertIn("LTI roster import failed", text)
        sentry_text = str(sentry.mock_calls)
        self.assertTrue(sentry.capture_message.called)
        for secret in [ACCESS_TOKEN, "secret.learner", "school.example", SUB] + list(
            _TOOL_PEM.splitlines()[1:-1]
        ):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, sentry_text)
        for _, fields in self.c.http.token_requests:
            self.assertNotIn(fields["client_assertion"], text)


class TestNrpsLimits(_Base):
    async def test_nrps_page_cap(self):
        await self.c.connected()
        self.c.serve(*[[_member(f"sub-{i}", f"{i}@school.example")] for i in range(5)])
        with patch.object(nrps_module, "MAX_PAGES", 3):
            status, body = await self.c.import_()
        self.assertEqual((status, body["errcode"]), (502, "ORG.PANGEA.LTI_UPSTREAM"))
        self.assertEqual(len(self.c.http.page_requests), 3)
        # Nothing of a refused roster is stored.
        self.assertEqual(await self.c.rows(), [])

    async def test_import_logs_carry_no_email(self):
        captured = _capture()
        self.addCleanup(captured.detach)
        await self.c.connected()
        self.c.serve(
            [_member(SUB, "logged.learner@school.example"), _member("x", None)]
        )
        status, body = await self.c.import_()
        self.assertEqual(status, 200, body)
        text = captured.text()
        self.assertIn("LTI roster imported", text)
        for secret in ("logged.learner", "school.example", SUB, ACCESS_TOKEN):
            self.assertNotIn(secret, text)


class TestLinkRoute(unittest.IsolatedAsyncioTestCase):
    """The link step's route: the token is optional, but one that is sent
    must be valid (a bad token never falls back to the token-less path)."""

    def route(self, token: Optional[str]) -> Tuple[LtiRoute, List[Any], Any]:
        calls: List[Any] = []

        async def handler(caller: Optional[str], body: Any) -> Tuple[int, Any]:
            calls.append((caller, body))
            return 200, {}

        class Auth:
            def has_access_token(self, request: Any) -> bool:
                return token is not None

            async def get_user_by_req(self, request: Any) -> Any:
                if token != "good":
                    raise InvalidClientTokenError()
                return MagicMock(user=MagicMock(to_string=lambda: STUDENT))

        homeserver = MagicMock()
        homeserver.get_auth.return_value = Auth()
        request = MagicMock()
        request.getClientAddress.return_value.host = "192.0.2.1"
        request.content = io.BytesIO(b'{"ticket": "t"}')
        limiter = SlidingWindowRateLimiter(
            requests_per_burst=5, burst_duration_seconds=60
        )
        return (
            LtiRoute(homeserver, "lti_link", "POST", KIND_TICKET, handler, limiter),
            calls,
            request,
        )

    async def test_a_token_that_is_sent_must_be_valid(self):
        route, calls, request = self.route("bad")
        self.assertEqual(await route._dispatch(request), (401, UNAUTHORIZED))
        self.assertEqual(calls, [])
        route, calls, request = self.route(None)
        self.assertEqual(await route._dispatch(request), (200, {}))
        self.assertEqual(calls, [(None, {"ticket": "t"})])
        route, calls, request = self.route("good")
        self.assertEqual(await route._dispatch(request), (200, {}))
        self.assertEqual(calls, [(STUDENT, {"ticket": "t"})])

    async def test_link_is_rate_limited_per_client_address(self):
        route, calls, request = self.route(None)
        for _ in range(5):
            await route._dispatch(request)
        self.assertEqual(await route._dispatch(request), (429, RATE_LIMITED))
        self.assertEqual(len(calls), 5)


class TestNextLink(unittest.TestCase):
    def test_next_link_reads_rel_next_only(self):
        self.assertEqual(
            next_link(
                [
                    '<https://a.example/p?page=1>; rel="first", '
                    '<https://a.example/p?page=2>; rel="next"'
                ]
            ),
            "https://a.example/p?page=2",
        )
        self.assertEqual(next_link(['<https://a.example/x>; rel="last"']), None)
        self.assertEqual(next_link([]), None)
        self.assertEqual(
            next_link(["<https://a.example/x>; rel=next"]), "https://a.example/x"
        )


# --- B3: student launch link ------------------------------------------------


class TestLearnerLaunch(_Base):
    async def test_first_learner_launch_goes_to_the_link_page_with_an_unbound_ticket(
        self,
    ):
        target, query = await self.c.go(self.c.launch(title="Spanish 1 & 2"))
        self.assertEqual(target, APP + "/lti/link")
        self.assertEqual(set(query), {"ticket", "course"})
        self.assertEqual(query["course"], "Spanish 1 & 2")
        self.assertEqual(self.c.login_tokens.issued, [])
        location = await self.c.redirects.location(self.c.launch())
        self.assertNotIn(CANVAS_EMAIL, location)
        self.assertNotIn(SUB, location)
        self.assertNotIn(CONTEXT, location)

    async def test_linked_learner_with_nothing_to_confirm_signs_in_with_a_login_token(
        self,
    ):
        await self.linked()
        target, query = await self.c.go(self.c.launch())
        self.assertEqual(target, APP + "/lti/token")
        self.assertEqual(set(query), {"loginToken"})
        self.assertEqual(self.c.login_tokens.issued, [(STUDENT, query["loginToken"])])

    async def test_existing_account_never_opened_without_its_own_sign_in(self):
        # A Pangea account owns the address Canvas reports, and an invitation
        # to that address exists. Neither opens the account.
        self.c.h.verify(STUDENT, CANVAS_EMAIL)
        (inv,) = await self.c.h.add(CANVAS_EMAIL)
        target, query = await self.c.go(self.c.launch())
        self.assertEqual(target, APP + "/lti/link")
        self.assertEqual(self.c.login_tokens.issued, [])
        # The ticket is unbound: without the account's own token it does nothing.
        before = await self.written()
        status, body = await self.c.learner_l1(None, query["ticket"])
        self.assertEqual(status, 401, body)
        self.assertEqual(await self.written(), before)
        self.assertEqual(self.c.login_tokens.issued, [])
        self.assertEqual((await self.c.h.row(inv["invitation_id"]))["state"], "invited")
        # Someone else signing in is linked themself, never the email's owner.
        status, body = await self.c.learner_l1(OTHER, query["ticket"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["login_token"], None)
        self.assertEqual(await self.c.links.linked_user(ISSUER, SUB), OTHER)
        self.assertEqual(self.c.login_tokens.issued, [])

    async def test_canvas_email_never_bound(self):
        (inv,) = await self.c.h.add(CANVAS_EMAIL)
        row = await self.canvas_row(email="imported@school.example")
        ticket = await self.c.ticket(self.c.launch())
        status, body = await self.c.learner_l1(STUDENT, ticket)
        self.assertEqual(status, 200, body)
        # The identity-matched row is claimed; the account gains no address.
        self.assertEqual(
            body["claimed"], [{"invitation_id": row["id"], "room_id": ROOM}]
        )
        self.assertEqual(await self.c.h.accounts.verified_email_keys(STUDENT), set())
        self.assertEqual(self.c.h.main.threepids.get(STUDENT, []), [])
        # The row with the Canvas-reported address is not claimed by it.
        self.assertEqual((await self.c.h.row(inv["invitation_id"]))["state"], "invited")
        self.assertIsNone(await self.c.h.store.get_ack(inv["invitation_id"], STUDENT))

    async def test_claim_only_exact_issuer_context_sub(self):
        await self.c.platform(OTHER_ISSUER, "other-client")
        exact = await self.canvas_row("exact@school.example")
        await self.c.h.store.import_canvas(
            OTHER_ROOM,
            OTHER_ISSUER,
            CONTEXT,
            [(SUB, "x", "same-sub-other-issuer@school.example")],
            TEACHER,
            NOW,
            lambda: "row-other-issuer",
        )
        await self.c.h.store.import_canvas(
            ROOM,
            ISSUER,
            CONTEXT,
            [(OTHER_SUB, "x", "other-sub@school.example")],
            TEACHER,
            NOW,
            lambda: "row-other-sub",
        )
        status, body = await self.c.learner_l1(
            STUDENT, await self.c.ticket(self.c.launch())
        )
        self.assertEqual(status, 200, body)
        self.assertEqual([c["invitation_id"] for c in body["claimed"]], [exact["id"]])
        for ident in ("row-other-issuer", "row-other-sub"):
            self.assertEqual((await self.c.h.row(ident))["state"], "invited")
            self.assertIsNone(await self.c.h.store.get_ack(ident, STUDENT))
        joined = await self.c.h.row(exact["id"])
        self.assertEqual((joined["state"], joined["claimant"]), ("joined", STUDENT))
        self.assertIsNotNone(await self.c.h.store.managed_record(STUDENT, ROOM))
        ack = await self.c.h.store.get_ack(exact["id"], STUDENT)
        assert ack is not None
        self.assertEqual(ack["disclosure_version"], V)

    async def test_canvas_claim_transaction_rechecks_the_identity_itself(self):
        row = await self.canvas_row()
        await self.c.h.store.record_ack(row["id"], STUDENT, V, NOW)
        for identity in (
            (OTHER_ISSUER, CONTEXT, SUB),
            (ISSUER, OTHER_CONTEXT, SUB),
            (ISSUER, CONTEXT, OTHER_SUB),
            None,
        ):
            outcome, _ = await self.c.h.store.claim_txn(
                row["id"],
                STUDENT,
                email_match=False,
                grant=False,
                now_ms=NOW,
                canvas_identity=identity,
            )
            self.assertEqual(outcome, "not_eligible", identity)
        outcome, _ = await self.c.h.store.claim_txn(
            row["id"],
            STUDENT,
            email_match=False,
            grant=False,
            now_ms=NOW,
            canvas_identity=(ISSUER, CONTEXT, SUB),
        )
        self.assertEqual(outcome, "claimed")

    async def test_link_ticket_for_another_canvas_context_never_claims_rows_of_that_course(
        self,
    ):
        row = await self.canvas_row()
        # The same student launches from another Canvas course.
        ticket = await self.c.ticket(self.c.launch(context=OTHER_CONTEXT))
        status, body = await self.c.learner_l1(STUDENT, ticket)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["claimed"], [])
        self.assertEqual((await self.c.h.row(row["id"]))["state"], "invited")
        self.assertIsNone(await self.c.h.store.get_ack(row["id"], STUDENT))
        self.assertEqual(self.c.h.joiner.joins, [])

    async def test_link_step_refuses_without_confirmation(self):
        row = await self.canvas_row()
        ticket = await self.c.ticket(self.c.launch())
        before = await self.written()
        for body in (
            {},
            {"confirmed": False, "disclosure_version": V},
            {"confirmed": "true", "disclosure_version": V},
            {"confirmed": True},
            {"confirmed": True, "disclosure_version": "2"},
            {"disclosure_version": V},
        ):
            status, answer = await self.c.l1(STUDENT, ticket, **body)
            self.assertEqual(
                (status, answer["errcode"]), (400, "M_INVALID_PARAM"), body
            )
        status, answer = await self.c.l1(
            STUDENT, ticket, confirmed=True, disclosure_version=V - 1
        )
        self.assertEqual(
            (status, answer["errcode"]), (409, "ORG.PANGEA.DISCLOSURE_OUTDATED")
        )
        self.assertEqual(await self.written(), before)
        self.assertEqual((await self.c.h.row(row["id"]))["state"], "invited")
        # None of these consumed the ticket.
        status, answer = await self.c.learner_l1(STUDENT, ticket)
        self.assertEqual(status, 200, answer)
        self.assertEqual(len(answer["claimed"]), 1)

    async def test_expired_link_ticket_is_rejected_and_writes_no_link(self):
        await self.canvas_row()
        ticket = await self.c.ticket(self.c.launch())
        before = await self.written()
        self.c.now += TEN_MINUTES
        status, body = await self.c.learner_l1(STUDENT, ticket)
        self.assertEqual((status, body["errcode"]), (410, "ORG.PANGEA.TICKET_INVALID"))
        self.assertEqual(await self.written(), before)

    async def test_replayed_link_ticket_is_rejected_and_writes_no_link(self):
        await self.canvas_row()
        ticket = await self.c.ticket(self.c.launch())
        status, body = await self.c.learner_l1(STUDENT, ticket)
        self.assertEqual(status, 200, body)
        before = await self.written()
        # Replayed by the same account or by another one.
        for caller in (STUDENT, OTHER):
            status, body = await self.c.learner_l1(caller, ticket)
            self.assertEqual(
                (status, body["errcode"]), (410, "ORG.PANGEA.TICKET_INVALID")
            )
        self.assertEqual(await self.written(), before)
        self.assertEqual(await self.c.links.linked_user(ISSUER, SUB), STUDENT)

    async def test_link_ticket_returned_by_a_different_signed_in_account_than_the_one_it_binds_is_rejected(
        self,
    ):
        await self.linked()
        row = await self.canvas_row()
        # The later launch binds its ticket to the linked account.
        target, query = await self.c.go(self.c.launch())
        self.assertEqual(target, APP + "/lti/link")
        before = await self.written()
        status, body = await self.c.learner_l1(OTHER, query["ticket"])
        self.assertEqual(status, 403, body)
        self.assertEqual(await self.written(), before)
        self.assertEqual(body["errcode"], "ORG.PANGEA.TICKET_WRONG_ACCOUNT")
        self.assertEqual((await self.c.h.row(row["id"]))["state"], "invited")
        self.assertIsNone(await self.c.h.store.get_ack(row["id"], OTHER))
        self.assertIsNone(await self.c.h.store.get_ack(row["id"], STUDENT))
        self.assertEqual(self.c.login_tokens.issued, [])
        self.assertEqual(await self.c.links.linked_user(ISSUER, SUB), STUDENT)
        # Consumed by the refused attempt.
        status, body = await self.c.learner_l1(STUDENT, query["ticket"])
        self.assertEqual(status, 410, body)

    async def test_later_launch_with_a_newly_imported_matching_row_asks_for_confirmation_then_claims(
        self,
    ):
        await self.linked()
        # Imported after the first launch.
        row = await self.canvas_row()
        target, query = await self.c.go(self.c.launch())
        self.assertEqual(target, APP + "/lti/link")
        self.assertEqual(set(query), {"ticket", "course"})
        self.assertEqual(self.c.login_tokens.issued, [])
        # Confirmed from the link page; the bound ticket needs no token and
        # the login token is issued only now, after the confirmation.
        status, body = await self.c.learner_l1(None, query["ticket"])
        self.assertEqual(status, 200, body)
        self.assertEqual(body["next"], "app")
        self.assertEqual(
            body["claimed"], [{"invitation_id": row["id"], "room_id": ROOM}]
        )
        self.assertEqual(self.c.login_tokens.issued, [(STUDENT, body["login_token"])])
        joined = await self.c.h.row(row["id"])
        self.assertEqual((joined["state"], joined["claimant"]), ("joined", STUDENT))
        # Next launch: nothing left to confirm, straight to the app.
        target, query = await self.c.go(self.c.launch())
        self.assertEqual(target, APP + "/lti/token")

    async def test_later_launch_retries_a_confirmed_claim_that_did_not_complete(
        self,
    ):
        """The student confirmed, but the claim's join failed: the row is
        acked and still Invited. A later launch retries the claim from that
        recorded confirmation instead of skipping it for good."""
        row = await self.canvas_row()
        self.c.h.joiner.refuse.add(STUDENT)
        with patch.object(report, "sentry_sdk"):
            status, body = await self.c.learner_l1(
                STUDENT, await self.c.ticket(self.c.launch())
            )
            self.assertEqual((status, body["claimed"]), (200, []))
            self.assertIsNotNone(await self.c.h.store.get_ack(row["id"], STUDENT))
            # Still refused: the launch still signs in, the row stays Invited,
            # and the failure is reported, not raised into the launch.
            target, _ = await self.c.go(self.c.launch())
            self.assertEqual(target, APP + "/lti/token")
            self.assertEqual((await self.c.h.row(row["id"]))["state"], "invited")
        self.c.h.joiner.refuse.discard(STUDENT)
        target, _ = await self.c.go(self.c.launch())
        self.assertEqual(target, APP + "/lti/token")
        joined = await self.c.h.row(row["id"])
        self.assertEqual((joined["state"], joined["claimant"]), ("joined", STUDENT))
        self.assertIsNotNone(await self.c.h.store.managed_record(STUDENT, ROOM))

    async def test_later_launch_never_claims_without_the_accounts_own_confirmation(
        self,
    ):
        await self.linked()
        row = await self.canvas_row()
        # Another account's confirmation does not count for this one.
        await self.c.h.store.record_ack(row["id"], OTHER, V, NOW)
        target, _ = await self.c.go(self.c.launch())
        self.assertEqual(target, APP + "/lti/link")
        self.assertEqual((await self.c.h.row(row["id"]))["state"], "invited")
        self.assertEqual(self.c.h.joiner.joins, [])

    async def test_bound_ticket_with_the_accounts_own_token_returns_no_login_token(
        self,
    ):
        await self.linked()
        await self.canvas_row()
        ticket = await self.c.ticket(self.c.launch())
        status, body = await self.c.learner_l1(STUDENT, ticket)
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["login_token"])
        self.assertEqual(self.c.login_tokens.issued, [])

    async def test_canvas_identity_already_linked_to_another_account_is_refused(self):
        await self.linked(STUDENT, SUB)
        row = await self.canvas_row(sub=OTHER_SUB, email="o@school.example")
        # OTHER_SUB's first launch, but STUDENT signs in: STUDENT is already
        # linked to another sub on this issuer.
        ticket = await self.c.ticket(self.c.launch(sub=OTHER_SUB))
        status, body = await self.c.learner_l1(STUDENT, ticket)
        self.assertEqual(
            (status, body["errcode"]), (409, "ORG.PANGEA.LTI_ALREADY_LINKED")
        )
        self.assertEqual((await self.c.h.row(row["id"]))["state"], "invited")
        self.assertIsNone(await self.c.h.store.get_ack(row["id"], STUDENT))
        # A first-launch ticket for SUB (already linked to STUDENT) presented
        # by OTHER: refused, never re-linked. (A linked SUB's launch gets a
        # login token, not a ticket; the store issues an unbound one directly,
        # as a launch that raced the first link would have.)
        ticket = await self.c.links.issue_ticket(
            KIND_LEARNER,
            platform_id=self.c.platform_ids[ISSUER],
            issuer=ISSUER,
            sub=SUB,
            context_id=CONTEXT,
            deployment_id=DEPLOYMENT,
            nrps_url=None,
            bound_user_id=None,
            now_ms=self.c.now,
        )
        status, body = await self.c.learner_l1(OTHER, ticket)
        self.assertEqual(
            (status, body["errcode"]), (409, "ORG.PANGEA.LTI_ALREADY_LINKED")
        )
        self.assertEqual(await self.c.links.linked_user(ISSUER, SUB), STUDENT)

    async def test_ticket_kinds_and_forms(self):
        learner = await self.c.ticket(self.c.launch())
        instructor = await self.c.ticket(self.c.launch(roles=(INSTRUCTOR,)))
        connect = await self.c.connect_ticket(teacher=OTHER, sub="canvas-teacher-2")
        # A learner ticket in the instructor form, and the reverse: 400, not
        # consumed.
        status, body = await self.c.l1(STUDENT, learner)
        self.assertEqual(status, 400, body)
        status, body = await self.c.learner_l1(TEACHER, instructor)
        self.assertEqual(status, 400, body)
        # A connect ticket is not a link ticket.
        status, body = await self.c.l1(OTHER, connect)
        self.assertEqual(status, 410, body)
        status, body = await self.c.learner_l1(STUDENT, "no-such-ticket")
        self.assertEqual(status, 410, body)
        # Unknown fields are refused.
        status, body = await self.c.l1(TEACHER, instructor, room_id=ROOM)
        self.assertEqual(status, 400, body)
        # The two link tickets still work.
        self.assertEqual((await self.c.learner_l1(STUDENT, learner))[0], 200)
        self.assertEqual((await self.c.l1(TEACHER, instructor))[0], 200)

    async def test_tickets_are_opaque_and_stored_only_as_a_hash(self):
        ticket = await self.c.ticket(self.c.launch())
        rows = await self.c.table("lti_ticket")
        self.assertEqual(len(rows), 1)
        self.assertNotIn(ticket, repr(rows))
        for kind in (KIND_LEARNER, KIND_INSTRUCTOR, KIND_CONNECT):
            self.assertIsInstance(kind, str)

    async def test_join_failure_claims_nothing_and_is_reported(self):
        row = await self.canvas_row()
        self.c.h.joiner.refuse.add(STUDENT)
        with patch.object(report, "sentry_sdk"):
            status, body = await self.c.learner_l1(
                STUDENT, await self.c.ticket(self.c.launch())
            )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["claimed"], [])
        self.assertEqual((await self.c.h.row(row["id"]))["state"], "invited")

    async def test_link_logs_carry_no_email_sub_or_ticket(self):
        captured = _capture()
        self.addCleanup(captured.detach)
        await self.canvas_row("logged@school.example")
        ticket = await self.c.ticket(self.c.launch())
        await self.c.learner_l1(OTHER, "bogus")
        await self.c.learner_l1(STUDENT, ticket)
        await self.c.learner_l1(STUDENT, ticket)
        text = captured.text()
        self.assertTrue(text)
        for secret in (
            "logged@school.example",
            CANVAS_EMAIL,
            SUB,
            ticket,
            ACCESS_TOKEN,
        ):
            self.assertNotIn(secret, text)


class TestNoLoginTokenForServerAdmins(_Base):
    """Owner decision 2026-10-09: a Canvas launch never issues a login token
    to a Synapse server admin. The admin signs in themself on the normal
    sign-in page; the check runs when the token would be issued, and fails
    closed."""

    async def test_server_admin_later_launch_gets_no_login_token(self):
        await self.linked()
        # Promoted after the link was made.
        self.c.server_admins.admins.add(STUDENT)
        captured = _capture()
        self.addCleanup(captured.detach)
        location = await self.c.redirects.location(self.c.launch())
        self.assertEqual(location, APP + "/home/login")
        self.assertEqual(self.c.login_tokens.issued, [])
        # Nor a ticket that would lead to one: unconfirmed rows to claim
        # change nothing.
        await self.canvas_row()
        tickets_before = await self.c.table("lti_ticket")
        location = await self.c.redirects.location(self.c.launch())
        self.assertEqual(location, APP + "/home/login")
        self.assertEqual(await self.c.table("lti_ticket"), tickets_before)
        self.assertEqual(self.c.login_tokens.issued, [])
        text = captured.text()
        self.assertIn("LTI login token refused: server admin", text)
        self.assertNotIn(STUDENT, text)
        self.assertNotIn(SUB, text)

    async def test_bound_ticket_of_an_account_promoted_since_gets_no_login_token(
        self,
    ):
        await self.linked()
        await self.canvas_row()
        ticket = await self.c.ticket(self.c.launch())
        self.c.server_admins.admins.add(STUDENT)
        before = await self.written()
        status, body = await self.c.learner_l1(None, ticket)
        self.assertEqual(status, 401, body)
        self.assertEqual(await self.written(), before)
        self.assertEqual(self.c.login_tokens.issued, [])
        # The ticket itself is consumed (C5.1): it cannot be retried with a
        # token either; a relaunch issues a new one.
        status, body = await self.c.learner_l1(STUDENT, ticket)
        self.assertEqual(status, 410, body)
        # Signing in themself, the admin still confirms and claims normally.
        ticket = await self.c.links.issue_ticket(
            KIND_LEARNER,
            platform_id=self.c.platform_ids[ISSUER],
            issuer=ISSUER,
            sub=SUB,
            context_id=CONTEXT,
            deployment_id=DEPLOYMENT,
            nrps_url=None,
            bound_user_id=STUDENT,
            now_ms=self.c.now,
        )
        status, body = await self.c.learner_l1(STUDENT, ticket)
        self.assertEqual((status, len(body["claimed"])), (200, 1), body)
        self.assertIsNone(body["login_token"])

    async def test_login_tokens_never_mint_for_an_admin_whoever_calls(self):
        tokens = LoginTokens(self.c.login_tokens, self.c.server_admins)
        self.c.server_admins.admins.add(STUDENT)
        self.assertIsNone(await tokens.issue(STUDENT))
        self.c.server_admins.admins.clear()
        self.c.server_admins.error = RuntimeError("down")
        self.assertIsNone(await tokens.issue(STUDENT))
        self.c.server_admins.error = None
        self.assertEqual(await tokens.issue(STUDENT), "login-token-1")
        self.assertEqual(self.c.login_tokens.issued, [(STUDENT, "login-token-1")])

    async def test_admin_flag_flipping_mid_link_step_never_leaves_writes_without_a_token(
        self,
    ):
        """One admin decision per request: if the flag read passes and a
        later read would fail or flip, the request still either refuses
        before writing (401) or completes with its token, never a claim
        written with no token."""
        await self.linked()
        row = await self.canvas_row()
        for later in (True, RuntimeError("database down")):
            ticket = await self.c.ticket(self.c.launch())
            self.c.server_admins.checks.clear()
            self.c.server_admins.script = [False, later]
            before = await self.written()
            status, body = await self.c.learner_l1(None, ticket)
            self.c.server_admins.script = []
            if status == 401:
                self.assertEqual(await self.written(), before, later)
            else:
                self.assertEqual(status, 200, body)
                self.assertIsNotNone(body["login_token"], later)
                self.assertEqual(len(body["claimed"]), 1)
                break
            self.assertEqual(self.c.server_admins.checks, [STUDENT])
        joined = await self.c.h.row(row["id"])
        self.assertEqual(joined["state"], "joined")

    async def test_admin_flag_flipping_mid_launch_never_writes_before_refusing(self):
        await self.linked()
        row = await self.canvas_row()
        # Confirmed, but the claim's join failed: the launch would retry it.
        self.c.h.joiner.refuse.add(STUDENT)
        with patch.object(report, "sentry_sdk"):
            await self.c.learner_l1(STUDENT, await self.c.ticket(self.c.launch()))
        self.c.h.joiner.refuse.discard(STUDENT)
        # An admin, a failed read, and a flag that passes then flips: the
        # launch decides once, before the retry writes anything.
        for script in ([True], [RuntimeError("database down")], [False, True]):
            self.c.server_admins.script = list(script)
            self.c.server_admins.checks.clear()
            before = await self.written()
            location = await self.c.redirects.location(self.c.launch())
            self.c.server_admins.script = []
            self.assertEqual(len(self.c.server_admins.checks), 1, script)
            if location == APP + "/home/login":
                self.assertEqual(await self.written(), before, script)
                self.assertEqual(
                    (await self.c.h.row(row["id"]))["state"], "invited", script
                )
            else:
                self.assertEqual(script, [False, True])
                self.assertTrue(location.startswith(APP + "/lti/token?loginToken="))
                self.assertEqual((await self.c.h.row(row["id"]))["state"], "joined")

    async def test_non_admin_later_launch_is_unchanged(self):
        await self.linked()
        target, query = await self.c.go(self.c.launch())
        self.assertEqual(target, APP + "/lti/token")
        self.assertEqual(self.c.login_tokens.issued, [(STUDENT, query["loginToken"])])
        # The flag was read when the token was issued.
        self.assertIn(STUDENT, self.c.server_admins.checks)

    async def test_an_error_in_the_admin_check_means_no_token(self):
        await self.linked()
        self.c.server_admins.error = RuntimeError("database down")
        captured = _capture()
        self.addCleanup(captured.detach)
        location = await self.c.redirects.location(self.c.launch())
        self.assertEqual(location, APP + "/home/login")
        await self.canvas_row()
        self.c.server_admins.error = None
        ticket = await self.c.ticket(self.c.launch())
        self.c.server_admins.error = RuntimeError("database down")
        status, body = await self.c.learner_l1(None, ticket)
        self.assertEqual(status, 401, body)
        self.assertEqual(self.c.login_tokens.issued, [])
        text = captured.text()
        self.assertIn("LTI login token refused: admin check failed", text)
        self.assertNotIn(STUDENT, text)


class _Captured(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: List[logging.LogRecord] = []
        self._logger = logging.getLogger("synapse.module.synapse_pangea_chat")
        self._level = self._logger.level
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)

    def detach(self) -> None:
        self._logger.removeHandler(self)
        self._logger.setLevel(self._level)


def _capture() -> _Captured:
    return _Captured()


del time, THIRD

if __name__ == "__main__":
    unittest.main()
