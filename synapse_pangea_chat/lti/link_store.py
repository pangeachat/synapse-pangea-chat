"""Module-owned tables for the Canvas hand-offs (CONTRACTS C5).

- `lti_ticket`: the single-use tickets a verified launch hands to the client
  or admin-dash. Every field comes from the validated launch; the client only
  ever holds the opaque id, and the table stores only its SHA-256, so a read
  of the table yields no usable ticket. A ticket expires 10 minutes after it
  is issued and is consumed by one atomic update, whatever happens after.
- `lti_user_link`: a Canvas identity (issuer + `sub`) linked to the Pangea
  account that signed in to claim it. One account per identity and one
  identity per account on an issuer.
- `lti_course_link`: a Canvas course (issuer, deployment, context) linked to
  one Pangea course, with the NRPS URL the roster import reads.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any, Optional, Tuple

import attr

KIND_LEARNER = "learner_link"
KIND_INSTRUCTOR = "instructor_link"
KIND_CONNECT = "connect"
TICKET_TTL_MS = 10 * 60 * 1000
MAX_TICKET_LENGTH = 128

# consume_ticket outcomes
TICKET_OK = "ok"
TICKET_INVALID = "invalid"  # unknown, expired or already consumed
TICKET_WRONG_KIND = "wrong_kind"  # live, but not the kind this step takes
TICKET_UNBOUND = "unbound"  # live, but needs the account's own sign-in

# link_user / link_course outcomes
LINKED = "linked"
SAME = "same"
CONFLICT = "conflict"

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS lti_ticket (
        ticket_hash TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        platform_id TEXT NOT NULL,
        issuer TEXT NOT NULL,
        sub TEXT NOT NULL,
        context_id TEXT NOT NULL,
        deployment_id TEXT NOT NULL,
        nrps_url TEXT,
        bound_user_id TEXT,
        expires_at_ms BIGINT NOT NULL,
        consumed_at_ms BIGINT)""",
    "CREATE INDEX IF NOT EXISTS lti_ticket_expires ON lti_ticket (expires_at_ms)",
    """CREATE TABLE IF NOT EXISTS lti_user_link (
        issuer TEXT NOT NULL,
        sub TEXT NOT NULL,
        user_id TEXT NOT NULL,
        linked_at_ms BIGINT NOT NULL,
        PRIMARY KEY (issuer, sub),
        UNIQUE (issuer, user_id))""",
    """CREATE TABLE IF NOT EXISTS lti_course_link (
        room_id TEXT PRIMARY KEY,
        platform_id TEXT NOT NULL,
        issuer TEXT NOT NULL,
        deployment_id TEXT NOT NULL,
        context_id TEXT NOT NULL,
        nrps_url TEXT NOT NULL,
        linked_by TEXT NOT NULL,
        linked_at_ms BIGINT NOT NULL,
        UNIQUE (issuer, deployment_id, context_id))""",
)

# The ticket's fields, in `Ticket` order.
_SELECT_TICKET = (
    "SELECT kind, platform_id, issuer, sub, context_id, deployment_id, nrps_url,"
    " bound_user_id, consumed_at_ms, expires_at_ms FROM lti_ticket"
    " WHERE ticket_hash = ?"
)
_TICKET_FIELDS = 8


@attr.s(frozen=True, auto_attribs=True)
class Ticket:
    kind: str
    platform_id: str
    issuer: str
    # Kept out of repr: a Canvas user id is a student identifier.
    sub: str = attr.ib(repr=False)
    context_id: str
    deployment_id: str
    nrps_url: Optional[str]
    bound_user_id: Optional[str]


@attr.s(frozen=True, auto_attribs=True)
class CourseLink:
    room_id: str
    platform_id: str
    issuer: str
    deployment_id: str
    context_id: str
    nrps_url: str


def _hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


class LtiLinkStore:
    def __init__(self, db_pool: Any):
        self._db = db_pool
        self._ready = False

    async def ensure(self) -> None:
        if self._ready:
            return

        def create(txn: Any) -> None:
            for sql in SCHEMA:
                txn.execute(sql)

        await self._db.runInteraction("lti_link_schema", create)
        self._ready = True

    # -- tickets ------------------------------------------------------------

    async def issue_ticket(
        self,
        kind: str,
        *,
        platform_id: str,
        issuer: str,
        sub: str,
        context_id: str,
        deployment_id: str,
        nrps_url: Optional[str],
        bound_user_id: Optional[str],
        now_ms: int,
    ) -> str:
        """A fresh opaque ticket; expired ones are swept in the same
        transaction so the table stays small."""
        await self.ensure()
        ticket = secrets.token_urlsafe(32)

        def insert(txn: Any) -> None:
            txn.execute("DELETE FROM lti_ticket WHERE expires_at_ms <= ?", (now_ms,))
            txn.execute(
                "INSERT INTO lti_ticket (ticket_hash, kind, platform_id, issuer,"
                " sub, context_id, deployment_id, nrps_url, bound_user_id,"
                " expires_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _hash(ticket),
                    kind,
                    platform_id,
                    issuer,
                    sub,
                    context_id,
                    deployment_id,
                    nrps_url,
                    bound_user_id,
                    now_ms + TICKET_TTL_MS,
                ),
            )

        await self._db.runInteraction("lti_issue_ticket", insert)
        return ticket

    async def consume_ticket(
        self, ticket: str, *, kind: str, require_bound: bool, now_ms: int
    ) -> Tuple[str, Optional[Ticket]]:
        """Consume a live ticket of `kind` in one conditional update.

        Of two concurrent presentations exactly one consumes it. A ticket that
        is live but of another kind (returned, unconsumed, so the caller can
        tell which), or unbound when `require_bound` (no signed-in caller), is
        left unconsumed and reported as such; anything else that is not
        consumable is `invalid`.
        """
        await self.ensure()
        key = _hash(ticket)

        def take(txn: Any) -> Tuple[str, Optional[Ticket]]:
            sql = (
                "UPDATE lti_ticket SET consumed_at_ms = ?"
                " WHERE ticket_hash = ? AND kind = ? AND consumed_at_ms IS NULL"
                " AND expires_at_ms > ?"
            )
            if require_bound:
                sql += " AND bound_user_id IS NOT NULL"
            txn.execute(sql, (now_ms, key, kind, now_ms))
            consumed = txn.rowcount == 1
            txn.execute(_SELECT_TICKET, (key,))
            row = txn.fetchone()
            if consumed:
                if row is None:
                    raise RuntimeError("ticket row missing after consume")
                return TICKET_OK, Ticket(*row[:_TICKET_FIELDS])
            if row is None or row[-2] is not None or row[-1] <= now_ms:
                return TICKET_INVALID, None
            if row[0] != kind:
                return TICKET_WRONG_KIND, Ticket(*row[:_TICKET_FIELDS])
            if require_bound and row[7] is None:
                return TICKET_UNBOUND, None
            return TICKET_INVALID, None

        return await self._db.runInteraction("lti_consume_ticket", take)

    # -- account links ------------------------------------------------------

    async def linked_user(self, issuer: str, sub: str) -> Optional[str]:
        await self.ensure()

        def select(txn: Any) -> Optional[str]:
            txn.execute(
                "SELECT user_id FROM lti_user_link WHERE issuer = ? AND sub = ?",
                (issuer, sub),
            )
            row = txn.fetchone()
            return row[0] if row else None

        return await self._db.runInteraction("lti_linked_user", select)

    async def link_user(self, issuer: str, sub: str, user_id: str, now_ms: int) -> str:
        """Link (issuer, sub) to `user_id`: `linked`, `same` (already this
        pair) or `conflict` (the identity is another account's, or the account
        holds another identity on this issuer). A conflict writes nothing."""
        await self.ensure()

        def write(txn: Any) -> str:
            txn.execute(
                "SELECT sub, user_id FROM lti_user_link"
                " WHERE issuer = ? AND (sub = ? OR user_id = ?)",
                (issuer, sub, user_id),
            )
            rows = txn.fetchall()
            if rows:
                return SAME if list(rows) == [(sub, user_id)] else CONFLICT
            txn.execute(
                "INSERT INTO lti_user_link (issuer, sub, user_id, linked_at_ms)"
                " VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (issuer, sub, user_id, now_ms),
            )
            return LINKED if txn.rowcount == 1 else CONFLICT

        return await self._db.runInteraction("lti_link_user", write)

    # -- course links -------------------------------------------------------

    async def course_link(self, room_id: str) -> Optional[CourseLink]:
        await self.ensure()

        def select(txn: Any) -> Optional[CourseLink]:
            txn.execute(
                "SELECT room_id, platform_id, issuer, deployment_id, context_id,"
                " nrps_url FROM lti_course_link WHERE room_id = ?",
                (room_id,),
            )
            row = txn.fetchone()
            return CourseLink(*row) if row else None

        return await self._db.runInteraction("lti_course_link", select)

    async def is_connected(self, room_id: str) -> bool:
        return await self.course_link(room_id) is not None

    async def link_course(
        self, ticket: Ticket, room_id: str, linked_by: str, now_ms: int
    ) -> str:
        """Link the ticket's Canvas course to `room_id`: `linked`, `same`, or
        `conflict` when either side is already linked elsewhere."""
        await self.ensure()
        if ticket.nrps_url is None:
            raise ValueError("a connect ticket always carries an NRPS URL")

        def write(txn: Any) -> str:
            txn.execute(
                "SELECT room_id, issuer, deployment_id, context_id"
                " FROM lti_course_link WHERE room_id = ?"
                " OR (issuer = ? AND deployment_id = ? AND context_id = ?)",
                (room_id, ticket.issuer, ticket.deployment_id, ticket.context_id),
            )
            rows = txn.fetchall()
            if rows:
                pair = (room_id, ticket.issuer, ticket.deployment_id, ticket.context_id)
                return SAME if list(rows) == [pair] else CONFLICT
            txn.execute(
                "INSERT INTO lti_course_link (room_id, platform_id, issuer,"
                " deployment_id, context_id, nrps_url, linked_by, linked_at_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (
                    room_id,
                    ticket.platform_id,
                    ticket.issuer,
                    ticket.deployment_id,
                    ticket.context_id,
                    ticket.nrps_url,
                    linked_by,
                    now_ms,
                ),
            )
            return LINKED if txn.rowcount == 1 else CONFLICT

        return await self._db.runInteraction("lti_link_course", write)
