"""The student invitation routes (CONTRACTS C2.1-C2.3) and their handlers.

Base path ``/_synapse/client/pangea/v1``. Teacher routes take the teacher's own
token and require course admin (power level 100 in the course space,
re-checked on every request; room-version creators count); every refusal is
the one 403 body, so a route is no oracle for which rooms exist. Student
routes take the student's own token. ``hint`` and ``managed_disclosure`` are
public and limited per client IP.

Order of checks, as ``course_member_emails``: token (401), rate limit (429),
input (400), course admin (403), then the route. Emails travel only in POST
bodies and in responses to course admins; unauthenticated answers carry the
masked hint only. Nothing here logs an address, or a request body, or an
exception's message.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Tuple

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
from synapse.types import RoomID
from synapse.util.threepids import validate_email
from twisted.mail.smtp import SMTPDeliveryError
from twisted.web.resource import Resource

from synapse_pangea_chat.config import (
    MANAGED_DISCLOSURE_TEXT,
    MANAGED_DISCLOSURE_VERSION,
)
from synapse_pangea_chat.notice_delivery.rate_limit import SlidingWindowRateLimiter
from synapse_pangea_chat.student_invitations.accounts import Accounts, email_key
from synapse_pangea_chat.student_invitations.approvals import Approvals
from synapse_pangea_chat.student_invitations.claim import (
    ClaimJoinFailed,
    StudentClaims,
    now_ms,
)
from synapse_pangea_chat.student_invitations.hint_lookup import (
    hintable,
    mask_email,
    plausible_id,
)
from synapse_pangea_chat.student_invitations.report import report_failure
from synapse_pangea_chat.student_invitations.store import (
    ALREADY_CLAIMED_IN_COURSE,
    CLAIMED,
    DECISION_DENIED,
    LIVE_STATES,
    NOT_LIVE,
    STATE_INVITED,
    STATE_JOINED,
    StudentInvitationStore,
)

logger = logging.getLogger("synapse.module.synapse_pangea_chat.student_invitations.api")

PREFIX = "/_synapse/client/pangea/v1/"
MAX_ADD = 500
MAX_SEND = 50
SOURCES_ADDED = ("manual", "csv")
# Not a full RFC 5322 check: one "@", no spaces, a dot in the domain. Synapse
# canonicalises the rest the way it stores verified addresses.
_EMAIL_SHAPE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_MAX_BODY_BYTES = 256 * 1024

Result = Tuple[int, Dict[str, Any]]

UNAUTHORIZED = {"error": "Unauthorized", "errcode": "M_UNAUTHORIZED"}
RATE_LIMITED = {"error": "Rate limited", "errcode": "M_LIMIT_EXCEEDED"}
FORBIDDEN = {"error": "Forbidden: course admin required", "errcode": "M_FORBIDDEN"}
NOT_FOUND = {"error": "Not found", "errcode": "M_NOT_FOUND"}
INTERNAL = {"error": "Internal server error"}


def _bad(message: str, **extra: Any) -> Result:
    return 400, {"error": message, "errcode": "M_INVALID_PARAM", **extra}


def _conflict(errcode: str, message: str) -> Result:
    return 409, {"error": message, "errcode": errcode}


_NOT_LIVE = ("ORG.PANGEA.INVITATION_NOT_LIVE", "Invitation is not live")
_ALREADY = (
    "ORG.PANGEA.ALREADY_CLAIMED_IN_COURSE",
    "Account already holds an invitation in this course",
)


class CourseAdmins(Protocol):
    async def is_course_admin(self, room_id: str, user_id: str) -> bool:
        ...


class CourseRooms(Protocol):
    async def course_name(self, room_id: str) -> Optional[str]:
        ...

    async def access_code(self, room_id: str) -> Optional[str]:
        ...

    async def membership(self, room_id: str, user_id: str) -> Optional[str]:
        ...


class Mailer(Protocol):
    async def send_invite(self, row: Dict[str, Any], access_code: str) -> None:
        ...


def valid_room_id(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    try:
        RoomID.from_string(value)
    # silent-ok: validation only - the caller answers 400 for an invalid id
    except Exception:
        return None
    return value


def _string(value: Any, limit: int = 255) -> Optional[str]:
    if isinstance(value, str) and 0 < len(value) <= limit:
        return value
    return None


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def parse_email(value: Any) -> Optional[Tuple[str, str]]:
    """(as entered, trimmed; canonical key) or None if not an email."""
    if not isinstance(value, str):
        return None
    entered = value.strip()
    if not _EMAIL_SHAPE.fullmatch(entered):
        return None
    try:
        key = validate_email(entered)
    # silent-ok: an invalid address is reported by index, never echoed
    except ValueError:
        return None
    return entered, key


def invitation_view(
    row: Dict[str, Any],
    pending_count: int = 0,
    same_student_as: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "invitation_id": row["id"],
        "room_id": row["course_room_id"],
        "email": row["email"],
        "state": row["state"],
        "source": row["source"],
        "invited_by": row["invited_by"],
        "claimant": row["claimant"],
        "send_count": row["send_count"],
        "last_sent_at_ms": row["last_sent_at_ms"],
        "created_at_ms": row["created_at_ms"],
        "joined_at_ms": row["joined_at_ms"],
        "canvas_identity": row["lti_user_id"] is not None,
        "pending_count": pending_count if row["state"] == STATE_INVITED else 0,
        "same_student_as": same_student_as if row["state"] == STATE_INVITED else None,
    }


def _send_failure(error: BaseException) -> str:
    if isinstance(error, SMTPDeliveryError) and 500 <= int(error.code or 0) < 600:
        return "address_rejected"
    return "mail_error"


class StudentInvitationHandlers:
    """Each route as a function of the caller and its parsed input, returning
    (status, body)."""

    def __init__(
        self,
        *,
        store: StudentInvitationStore,
        claims: StudentClaims,
        approvals: Approvals,
        accounts: Accounts,
        admins: CourseAdmins,
        rooms: CourseRooms,
        mailer: Mailer,
        new_id: Callable[[], str] = lambda: secrets.token_urlsafe(16),
    ) -> None:
        self._store = store
        self._claims = claims
        self._approvals = approvals
        self._accounts = accounts
        self._admins = admins
        self._rooms = rooms
        self._mailer = mailer
        self._new_id = new_id

    async def _admin_room(self, caller: str, args: Any) -> Tuple[Optional[str], Result]:
        """The course a teacher route acts on, or the refusal to answer."""
        if not isinstance(args, dict):
            return None, _bad("Request body must be a JSON object")
        room_id = valid_room_id(args.get("room_id"))
        if room_id is None:
            return None, _bad("'room_id' must be a valid Matrix room ID")
        if not await self._admins.is_course_admin(room_id, caller):
            return None, (403, FORBIDDEN)
        return room_id, (200, {})

    async def _views(self, room_id: str, rows: List[Dict[str, Any]]) -> List[Dict]:
        approvals = await self._approvals.for_room(room_id)
        return [
            invitation_view(
                row,
                len(approvals.pending.get(row["id"], [])),
                approvals.same_student_as.get(row["id"]),
            )
            for row in rows
        ]

    async def _view(self, room_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
        return (await self._views(room_id, [row]))[0]

    # --- teacher routes ---

    async def add(self, caller: str, body: Any) -> Result:
        room_id, refusal = await self._admin_room(caller, body)
        if room_id is None:
            return refusal
        emails = body.get("emails")
        if not isinstance(emails, list) or not 1 <= len(emails) <= MAX_ADD:
            return _bad(f"'emails' must be a list of 1 to {MAX_ADD} addresses")
        source = body.get("source")
        if source not in SOURCES_ADDED:
            return _bad("'source' must be 'manual' or 'csv'")
        parsed = [parse_email(e) for e in emails]
        invalid = [i for i, p in enumerate(parsed) if p is None]
        if invalid:
            return _bad("Some addresses are not valid emails", invalid_indexes=invalid)
        entries = [p for p in parsed if p is not None]
        rows = await self._store.add(
            room_id, entries, source, caller, now_ms(), self._new_id
        )
        logger.info(
            "student invitations added: room=%s caller=%s count=%d",
            room_id,
            caller,
            len(entries),
        )
        return 200, {"invitations": await self._views(room_id, rows)}

    async def send(self, caller: str, body: Any) -> Result:
        room_id, refusal = await self._admin_room(caller, body)
        if room_id is None:
            return refusal
        items = body.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_SEND:
            return _bad(f"'items' must be a list of 1 to {MAX_SEND} items")
        parsed: List[Tuple[str, int]] = []
        for item in items:
            ident = (
                _string(item.get("invitation_id")) if isinstance(item, dict) else None
            )
            expected = _count(item.get("expected_send_count")) if ident else None
            if ident is None or expected is None:
                return _bad("Each item needs 'invitation_id' and 'expected_send_count'")
            parsed.append((ident, expected))
        code = await self._rooms.access_code(room_id)
        if not code:
            return 400, {
                "error": "Course has no class code",
                "errcode": "ORG.PANGEA.NO_JOIN_CODE",
            }
        results: List[Dict[str, Any]] = []
        stopped: Optional[str] = None
        for ident, expected in parsed:
            status, before = await self._store.reserve_send(
                room_id, ident, expected, now_ms()
            )
            if status != "reserved" or before is None:
                results.append(
                    {"invitation_id": ident, "outcome": "skipped", "reason": status}
                )
                continue
            try:
                await self._mailer.send_invite(before, code)
            except Exception as error:
                reason = _send_failure(error)
                await self._store.release_send(before)
                logger.warning(
                    "student invitation %s not sent: %s (%s)",
                    ident,
                    reason,
                    type(error).__name__,
                )
                results.append(
                    {"invitation_id": ident, "outcome": "failed", "reason": reason}
                )
                if reason == "mail_error":
                    # The mail service itself failed: the rest would fail the
                    # same way, so stop and let the teacher retry them.
                    stopped = reason
                    break
                continue
            results.append({"invitation_id": ident, "outcome": "sent", "reason": None})
        logger.info(
            "student invitations send: room=%s caller=%s sent=%d of %d",
            room_id,
            caller,
            sum(1 for r in results if r["outcome"] == "sent"),
            len(parsed),
        )
        return 200, {"results": results, "stopped_reason": stopped}

    async def list(self, caller: str, query: Any) -> Result:
        room_id, refusal = await self._admin_room(caller, query)
        if room_id is None:
            return refusal
        rows = await self._store.list_room(room_id)
        return 200, {"invitations": await self._views(room_id, rows)}

    async def revoke(self, caller: str, body: Any) -> Result:
        room_id, refusal = await self._admin_room(caller, body)
        if room_id is None:
            return refusal
        ident = _string(body.get("invitation_id"))
        if ident is None:
            return _bad("'invitation_id' is required")
        row = await self._store.revoke(room_id, ident)
        if row is None:
            return 404, NOT_FOUND
        logger.info("student invitation %s revoked by %s", ident, caller)
        return 200, {"invitation": await self._view(room_id, row)}

    async def pending_approvals(self, caller: str, query: Any) -> Result:
        room_id, refusal = await self._admin_room(caller, query)
        if room_id is None:
            return refusal
        return 200, {"pending": await self._approvals.pending_rows(room_id)}

    async def decide(self, caller: str, body: Any) -> Result:
        room_id, refusal = await self._admin_room(caller, body)
        if room_id is None:
            return refusal
        ident = _string(body.get("invitation_id"))
        user_id = _string(body.get("user_id"))
        decision = body.get("decision")
        if ident is None or user_id is None or decision not in ("grant", "deny"):
            return _bad("'invitation_id', 'user_id' and 'decision' are required")
        row = await self._store.in_room(room_id, ident)
        if row is None or await self._store.get_ack(ident, user_id) is None:
            return 404, NOT_FOUND
        if decision == "deny":
            status, denied = await self._store.deny(ident, user_id)
            if status == "no_ack" or denied is None:
                return 404, NOT_FOUND
            if status == "not_live":
                return _conflict(*_NOT_LIVE)
            logger.info("student invitation %s: %s denied", ident, user_id)
            return 200, {"invitation": await self._view(room_id, denied)}
        outcome, claimed = await self._approvals.grant(ident, user_id)
        if outcome == CLAIMED and claimed is not None:
            logger.info("student invitation %s: %s granted", ident, user_id)
            return 200, {"invitation": await self._view(room_id, claimed)}
        if outcome == ALREADY_CLAIMED_IN_COURSE:
            return _conflict(*_ALREADY)
        if outcome == NOT_LIVE:
            return _conflict(*_NOT_LIVE)
        # The confirmation went away since the read above (a re-invite).
        return 404, NOT_FOUND

    async def approve_all(self, caller: str, body: Any) -> Result:
        room_id, refusal = await self._admin_room(caller, body)
        if room_id is None:
            return refusal
        result = await self._approvals.approve_all(room_id)
        logger.info(
            "student invitations approve_all: room=%s caller=%s granted=%d"
            " skipped=%d refused=%d",
            room_id,
            caller,
            len(result["granted"]),
            len(result["skipped_multiple"]),
            len(result["refused"]),
        )
        return 200, result

    async def invite_member(self, caller: str, body: Any) -> Result:
        room_id, refusal = await self._admin_room(caller, body)
        if room_id is None:
            return refusal
        user_id = _string(body.get("user_id"))
        if user_id is None:
            return _bad("'user_id' is required")
        if await self._rooms.membership(room_id, user_id) != "join":
            return _conflict("ORG.PANGEA.NOT_MEMBER", "User is not a course member")
        held = await self._store.joined_in_course(room_id, user_id)
        if held is not None:
            return 200, {"invitation_id": held["id"], "state": held["state"]}
        address = await self._accounts.first_email(user_id)
        key = email_key(address) if address is not None else None
        if key is None:
            return _conflict("ORG.PANGEA.NO_EMAIL", "User has no email")
        (row,) = await self._store.add(
            room_id, [(None, key)], "member", caller, now_ms(), self._new_id
        )
        logger.info(
            "student invitation %s for member %s by %s", row["id"], user_id, caller
        )
        return 200, {"invitation_id": row["id"], "state": row["state"]}

    async def live(self, caller: str, query: Any) -> Result:
        room_id, refusal = await self._admin_room(caller, query)
        if room_id is None:
            return refusal
        ident = _string(query.get("invitation_id"))
        if ident is None:
            return _bad("'invitation_id' is required")
        row = await self._store.in_room(room_id, ident)
        if row is None:
            return 200, {"live": False, "state": None}
        return 200, {"live": row["state"] in LIVE_STATES, "state": row["state"]}

    # --- student routes ---

    async def confirm(self, caller: str, body: Any) -> Result:
        if not isinstance(body, dict):
            return _bad("Request body must be a JSON object")
        ident = _string(body.get("invitation_id"))
        version = body.get("disclosure_version")
        if ident is None or isinstance(version, bool) or not isinstance(version, int):
            return _bad("'invitation_id' and 'disclosure_version' are required")
        if version != MANAGED_DISCLOSURE_VERSION:
            return _conflict(
                "ORG.PANGEA.DISCLOSURE_OUTDATED", "The disclosure has changed"
            )
        row = await self._store.get(ident)
        if row is None or not (
            row["state"] == STATE_INVITED
            or (row["state"] == STATE_JOINED and row["claimant"] == caller)
        ):
            return 404, NOT_FOUND
        room_id = row["course_room_id"]
        claimed = {"result": "claimed", "invitation_id": ident, "room_id": room_id}
        if row["state"] == STATE_JOINED:
            return 200, claimed
        await self._store.record_ack(ident, caller, version, now_ms())
        logger.info("student invitation %s confirmed by %s", ident, caller)
        # The claim itself requires the verified-email match (or a grant) and
        # joins nothing without one.
        outcome, _ = await self._claims.claim(ident, caller)
        if outcome == CLAIMED:
            return 200, claimed
        if outcome == ALREADY_CLAIMED_IN_COURSE:
            return _conflict(*_ALREADY)
        if outcome == NOT_LIVE:
            return 404, NOT_FOUND
        ack = await self._store.get_ack(ident, caller)
        result = (
            "denied"
            if ack is not None and ack["decision"] == DECISION_DENIED
            else "pending_approval"
        )
        return 200, {"result": result, "invitation_id": ident, "room_id": room_id}

    async def mine_pending(self, caller: str) -> Result:
        keys = await self._accounts.verified_email_keys(caller)
        acks = await self._store.acks_by(caller)
        rows = [
            r for r in await self._store.invited_for_keys(keys) if r["id"] not in acks
        ]
        return 200, {
            "invitations": [
                {
                    "invitation_id": r["id"],
                    "room_id": r["course_room_id"],
                    "course_name": await self._rooms.course_name(r["course_room_id"]),
                }
                for r in rows
            ]
        }

    async def mine_joined(self, caller: str) -> Result:
        rows = await self._store.joined_by(caller)
        return 200, {
            "invitations": [
                {
                    "invitation_id": r["id"],
                    "room_id": r["course_room_id"],
                    "invited_by": r["invited_by"],
                }
                for r in rows
            ]
        }

    # --- public routes ---

    async def hint(self, query: Any) -> Result:
        ident = plausible_id(
            query.get("invitation_id") if isinstance(query, dict) else None
        )
        row = await self._store.get(ident) if ident is not None else None
        if row is None or not hintable(row):
            return 404, NOT_FOUND
        return 200, {
            "course_name": await self._rooms.course_name(row["course_room_id"]),
            "masked_email_hint": mask_email(row["email_key"]),
        }

    async def disclosure(self) -> Result:
        return 200, {
            "version": MANAGED_DISCLOSURE_VERSION,
            "text": MANAGED_DISCLOSURE_TEXT,
        }


# --- HTTP ---

Handler = Callable[..., Awaitable[Result]]


async def guarded(name: str, call: Callable[[], Awaitable[Result]]) -> Result:
    """Run a route; an unexpected failure is a 500 reported with ids only."""
    try:
        return await call()
    except Exception as error:
        if isinstance(error, ClaimJoinFailed):
            logger.warning("%s: the account could not join the course", name)
        report_failure(name, error)
        return 500, INTERNAL


def _read_json(request: SynapseRequest) -> Any:
    """The body as JSON, or None. Never logs it: it carries addresses."""
    try:
        raw = request.content.read(_MAX_BODY_BYTES + 1)
        if len(raw) > _MAX_BODY_BYTES:
            return None
        return json.loads(raw.decode("utf-8"))
    # silent-ok: an unreadable body is answered 400, and must not reach a log
    except Exception:
        return None


def _read_query(request: SynapseRequest) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    args: Dict[bytes, List[bytes]] = dict(request.args or {})
    for key, values in args.items():
        if len(values) != 1:
            continue
        try:
            query[key.decode("utf-8")] = values[0].decode("utf-8")
        # silent-ok: an undecodable parameter is treated as absent (400/404)
        except UnicodeDecodeError:
            continue
    return query


class StudentInvitationRoute(Resource):
    """One route: ``kind`` is "teacher" or "student" (token required) or
    "public" (limited per client IP)."""

    isLeaf = True

    def __init__(
        self,
        homeserver: Any,
        name: str,
        method: str,
        kind: str,
        handler: Handler,
        limiter: SlidingWindowRateLimiter,
    ) -> None:
        super().__init__()
        self._auth = homeserver.get_auth()
        self._name = name
        self._method = method
        self._kind = kind
        self._handler = handler
        self._limiter = limiter

    def render_GET(self, request: SynapseRequest) -> Any:
        return self._render(request, "GET")

    def render_POST(self, request: SynapseRequest) -> Any:
        return self._render(request, "POST")

    def _render(self, request: SynapseRequest, method: str) -> Any:
        if method != self._method:
            respond_with_json(
                request,
                405,
                {"error": "Method not allowed", "errcode": "M_UNRECOGNIZED"},
                send_cors=True,
            )
            return server.NOT_DONE_YET
        run_in_background(self._async_render, request)
        return server.NOT_DONE_YET

    async def _async_render(self, request: SynapseRequest) -> None:
        status, payload = await guarded(self._name, lambda: self._dispatch(request))
        respond_with_json(request, status, payload, send_cors=True)

    async def _dispatch(self, request: SynapseRequest) -> Result:
        if self._kind == "public":
            if self._limiter.is_rate_limited(request.getClientAddress().host):
                return 429, RATE_LIMITED
            return await self._handler(_read_query(request))
        try:
            requester = await self._auth.get_user_by_req(request)
        # silent-ok: the caller's own auth failure, answered 401
        except (
            MissingClientTokenError,
            InvalidClientTokenError,
            InvalidClientCredentialsError,
            AuthError,
        ):
            return 401, UNAUTHORIZED
        caller = requester.user.to_string()
        if self._limiter.is_rate_limited(caller):
            return 429, RATE_LIMITED
        if self._kind == "student" and self._method == "GET":
            return await self._handler(caller)
        args = _read_json(request) if self._method == "POST" else _read_query(request)
        return await self._handler(caller, args)
