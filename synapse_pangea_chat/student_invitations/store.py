"""The module's student invitation tables and every write to them.

Three tables (SPEC §6):

- ``pangea_student_invitation``: one row per (course, canonical email). States
  ``invited`` -> ``joined`` (claimed by one account) -> ``left`` (that account
  left, or was removed from, the course); ``revoked`` by a course admin. A
  ``revoked`` or ``left`` row is reset to ``invited`` when the email is added
  again. The ``lti_*`` columns hold the Canvas identity of an imported row.
- ``pangea_invitation_ack``: the **request** table (seats amendment
  2026-10-10). A request is written only when an account opens an invitation
  whose address it does not have verified; it carries the teacher's decision
  (null, granted, denied). A verified match claims with no request.
- ``pangea_managed_account``: written by every claim (``created_at`` only),
  deleted when the invitation is revoked or its claimant leaves the course.
- ``pangea_student_invitation_event``: the append-only activity ledger
  (``events.py``), written in the same transaction as each change.

Every write that decides a claim locks the invitation row first (a no-op
``UPDATE``), so on Postgres's repeatable-read transactions a concurrent change
to the same row aborts one side, which Synapse retries against the new state.
Two partial unique indexes back the rules no lock can see across rows: one
joined invitation per (course, account) and one Canvas identity per course.

Emails never leave this file in a log line or an exception message.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from synapse.storage.engines import PostgresEngine

from synapse_pangea_chat.student_invitations.events import (
    CANVAS_ROSTER_IMPORTED,
    EVENT_SCHEMA,
    INVITATION_CLAIMED,
    INVITATION_WITHDRAWN,
    INVITE_RESENT,
    INVITE_SENT,
    REQUEST_DENIED,
    REQUEST_GRANTED,
    REQUEST_MADE,
    STUDENT_LEFT,
    STUDENTS_ADDED,
    SYSTEM,
    record_event,
)

STATE_INVITED = "invited"
STATE_JOINED = "joined"
STATE_LEFT = "left"
STATE_REVOKED = "revoked"
LIVE_STATES = (STATE_INVITED, STATE_JOINED)

DECISION_GRANTED = "granted"
DECISION_DENIED = "denied"

SCHEMA = (
    (
        """CREATE TABLE IF NOT EXISTS pangea_student_invitation (
        id TEXT PRIMARY KEY,
        course_room_id TEXT NOT NULL,
        email_key TEXT NOT NULL,
        email TEXT,
        state TEXT NOT NULL,
        source TEXT NOT NULL,
        invited_by TEXT NOT NULL,
        claimant TEXT,
        send_count BIGINT NOT NULL DEFAULT 0,
        last_sent_at_ms BIGINT,
        created_at_ms BIGINT NOT NULL,
        joined_at_ms BIGINT,
        lti_issuer TEXT,
        lti_context_id TEXT,
        lti_user_id TEXT,
        member_user_id TEXT,
        UNIQUE (course_room_id, email_key))""",
        """CREATE UNIQUE INDEX IF NOT EXISTS pangea_student_invitation_one_joined
        ON pangea_student_invitation (course_room_id, claimant)
        WHERE state = 'joined'""",
        """CREATE UNIQUE INDEX IF NOT EXISTS pangea_student_invitation_lti_identity
        ON pangea_student_invitation (course_room_id, lti_issuer, lti_user_id)
        WHERE lti_user_id IS NOT NULL""",
        """CREATE INDEX IF NOT EXISTS pangea_student_invitation_claimant
        ON pangea_student_invitation (claimant)""",
        """CREATE INDEX IF NOT EXISTS pangea_student_invitation_email_key
        ON pangea_student_invitation (email_key)""",
        """CREATE TABLE IF NOT EXISTS pangea_invitation_ack (
        invitation_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        requested_at_ms BIGINT NOT NULL,
        decision TEXT,
        PRIMARY KEY (invitation_id, user_id))""",
        """CREATE INDEX IF NOT EXISTS pangea_invitation_ack_user
        ON pangea_invitation_ack (user_id)""",
        """CREATE TABLE IF NOT EXISTS pangea_managed_account (
        user_id TEXT NOT NULL,
        course_room_id TEXT NOT NULL,
        created_at_ms BIGINT NOT NULL,
        PRIMARY KEY (user_id, course_room_id))""",
    )
    + EVENT_SCHEMA
)

COLUMNS = (
    "id",
    "course_room_id",
    "email_key",
    "email",
    "state",
    "source",
    "invited_by",
    "claimant",
    "send_count",
    "last_sent_at_ms",
    "created_at_ms",
    "joined_at_ms",
    "lti_issuer",
    "lti_context_id",
    "lti_user_id",
    "member_user_id",
)
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM pangea_student_invitation "
REQUEST_COLUMNS = ("invitation_id", "user_id", "requested_at_ms", "decision")
_REQUEST_SELECT = (
    "SELECT invitation_id, user_id, requested_at_ms, decision"
    " FROM pangea_invitation_ack "
)
# T12's field names for the event columns (event_id, ts_ms, actor, action,
# invitation_id, count).
EVENT_COLUMNS = ("event_id", "time_ms", "actor", "action", "target", "count")
EVENTS_PAGE = 100

# (lti_issuer, lti_context_id, lti_user_id): a Canvas identity.
CanvasIdentity = Tuple[str, str, str]

SOURCE_CANVAS = "canvas"
# A concurrent import can beat this one to a row once per learner it shares;
# each retry re-reads, so a few attempts always converge.
IMPORT_ATTEMPTS = 3

# Claim outcomes (claim_txn and StudentClaims.claim).
CLAIMED = "claimed"
NOT_LIVE = "not_live"
NOT_ELIGIBLE = "not_eligible"
ALREADY_CLAIMED_IN_COURSE = "already_claimed_in_course"


def _row(raw: Optional[Sequence[Any]]) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    return dict(zip(COLUMNS, raw))


def _request(raw: Optional[Sequence[Any]]) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    return dict(zip(REQUEST_COLUMNS, raw))


def canvas_match(row: Dict[str, Any], identity: Optional[CanvasIdentity]) -> bool:
    """True when the row was imported with exactly this Canvas identity."""
    if identity is None or row["lti_user_id"] is None:
        return False
    return (row["lti_issuer"], row["lti_context_id"], row["lti_user_id"]) == identity


def is_integrity_error(error: BaseException) -> bool:
    """A unique-index violation, from psycopg2 or sqlite alike."""
    return any(cls.__name__ == "IntegrityError" for cls in type(error).__mro__)


def _lock(txn: Any, invitation_id: str) -> None:
    # A no-op write takes the row lock: a concurrent writer of the same row
    # then fails serialisation and Synapse retries it on the new state.
    txn.execute(
        "UPDATE pangea_student_invitation SET state = state WHERE id = ?",
        (invitation_id,),
    )


def _select_one(txn: Any, where: str, args: Tuple[Any, ...]) -> Optional[Dict]:
    txn.execute(_SELECT + where, args)
    return _row(txn.fetchone())


def _joined_in_course(txn: Any, room_id: str, user_id: str) -> Optional[Dict]:
    return _select_one(
        txn,
        "WHERE course_room_id = ? AND claimant = ? AND state = 'joined'",
        (room_id, user_id),
    )


class StudentInvitationStore:
    def __init__(self, homeserver: Any) -> None:
        self.db = homeserver.get_datastores().main.db_pool
        self._ready = False

    async def ensure(self) -> None:
        if self._ready:
            return

        def create(txn: Any) -> None:
            for sql in SCHEMA:
                txn.execute(sql)
            migrate(txn)

        await self.db.runInteraction("pangea_student_invitation_schema", create)
        self._ready = True

    # --- reads ---

    async def get(self, invitation_id: str) -> Optional[Dict[str, Any]]:
        await self.ensure()

        def select(txn: Any) -> Optional[Dict]:
            return _select_one(txn, "WHERE id = ?", (invitation_id,))

        return await self.db.runInteraction("pangea_student_invitation_get", select)

    async def in_room(
        self, room_id: str, invitation_id: str
    ) -> Optional[Dict[str, Any]]:
        row = await self.get(invitation_id)
        if row is None or row["course_room_id"] != room_id:
            return None
        return row

    async def list_room(self, room_id: str) -> List[Dict[str, Any]]:
        await self.ensure()

        def select(txn: Any) -> List[Dict]:
            txn.execute(
                _SELECT + "WHERE course_room_id = ? ORDER BY created_at_ms, id",
                (room_id,),
            )
            return [r for r in (_row(x) for x in txn.fetchall()) if r is not None]

        return await self.db.runInteraction("pangea_student_invitation_list", select)

    async def joined_by(self, user_id: str) -> List[Dict[str, Any]]:
        await self.ensure()

        def select(txn: Any) -> List[Dict]:
            txn.execute(
                _SELECT
                + "WHERE claimant = ? AND state = 'joined' ORDER BY joined_at_ms, id",
                (user_id,),
            )
            return [r for r in (_row(x) for x in txn.fetchall()) if r is not None]

        return await self.db.runInteraction("pangea_student_invitation_joined", select)

    async def joined_in_course(
        self, room_id: str, user_id: str
    ) -> Optional[Dict[str, Any]]:
        await self.ensure()

        def select(txn: Any) -> Optional[Dict]:
            return _joined_in_course(txn, room_id, user_id)

        return await self.db.runInteraction(
            "pangea_student_invitation_joined_in_course", select
        )

    async def invited_for_keys(self, email_keys: Iterable[str]) -> List[Dict]:
        """``invited`` rows, in any course, whose email key is one of these."""
        keys = sorted(set(email_keys))
        if not keys:
            return []
        await self.ensure()

        def select(txn: Any) -> List[Dict]:
            rows: List[Dict] = []
            for key in keys:
                txn.execute(
                    _SELECT + "WHERE state = 'invited' AND email_key = ?", (key,)
                )
                rows.extend(r for r in (_row(x) for x in txn.fetchall()) if r)
            rows.sort(key=lambda r: (r["created_at_ms"], r["id"]))
            return rows

        return await self.db.runInteraction("pangea_student_invitation_keys", select)

    async def canvas_invited(self, identity: CanvasIdentity) -> List[Dict]:
        """``invited`` rows, in any course, imported with exactly this Canvas
        identity."""
        await self.ensure()
        issuer, context_id, user_id = identity

        def select(txn: Any) -> List[Dict]:
            txn.execute(
                _SELECT + "WHERE state = 'invited' AND lti_issuer = ?"
                " AND lti_context_id = ? AND lti_user_id = ?"
                " ORDER BY created_at_ms, id",
                (issuer, context_id, user_id),
            )
            return [r for r in (_row(x) for x in txn.fetchall()) if r is not None]

        return await self.db.runInteraction("pangea_student_invitation_canvas", select)

    async def get_request(self, invitation_id: str, user_id: str) -> Optional[Dict]:
        await self.ensure()

        def select(txn: Any) -> Optional[Dict]:
            txn.execute(
                _REQUEST_SELECT + "WHERE invitation_id = ? AND user_id = ?",
                (invitation_id, user_id),
            )
            return _request(txn.fetchone())

        return await self.db.runInteraction("pangea_invitation_request_get", select)

    async def requests_in_room(self, room_id: str) -> List[Dict[str, Any]]:
        """Every request on an ``invited`` row of this course."""
        await self.ensure()

        def select(txn: Any) -> List[Dict]:
            txn.execute(
                "SELECT a.invitation_id, a.user_id, a.requested_at_ms, a.decision"
                " FROM pangea_invitation_ack a"
                " JOIN pangea_student_invitation i ON i.id = a.invitation_id"
                " WHERE i.course_room_id = ? AND i.state = 'invited'"
                " ORDER BY a.requested_at_ms, a.user_id",
                (room_id,),
            )
            return [r for r in (_request(x) for x in txn.fetchall()) if r is not None]

        return await self.db.runInteraction("pangea_invitation_requests_room", select)

    async def events(
        self,
        room_id: str,
        *,
        from_ms: Optional[int],
        to_ms: Optional[int],
        action: Optional[str],
        before: Optional[Tuple[int, str]],
    ) -> Tuple[List[Dict[str, Any]], Optional[Tuple[int, str]]]:
        """One page of this course's events, newest first, and the position
        after its last row when more follow. ``from_ms`` is inclusive,
        ``to_ms`` exclusive; ``before`` is a position from an earlier page.
        Keyset paging on (time, id), so with a fixed ``to`` the pages never
        shift as new events arrive."""
        await self.ensure()
        before_ts, before_id = before if before is not None else (None, None)

        def select(txn: Any) -> List[Dict[str, Any]]:
            # One fixed statement: each filter is off when its value is NULL.
            txn.execute(
                "SELECT event_id, ts_ms, actor, action, invitation_id, count"
                " FROM pangea_student_invitation_event"
                " WHERE room_id = ?"
                " AND (? IS NULL OR ts_ms >= ?)"
                " AND (? IS NULL OR ts_ms < ?)"
                " AND (? IS NULL OR action = ?)"
                " AND (? IS NULL OR ts_ms < ? OR (ts_ms = ? AND event_id < ?))"
                " ORDER BY ts_ms DESC, event_id DESC LIMIT ?",
                (
                    room_id,
                    from_ms,
                    from_ms,
                    to_ms,
                    to_ms,
                    action,
                    action,
                    before_ts,
                    before_ts,
                    before_ts,
                    before_id,
                    EVENTS_PAGE + 1,
                ),
            )
            return [dict(zip(EVENT_COLUMNS, raw)) for raw in txn.fetchall()]

        rows = await self.db.runInteraction("pangea_invitation_events", select)
        if len(rows) <= EVENTS_PAGE:
            return rows, None
        page = rows[:EVENTS_PAGE]
        return page, (page[-1]["time_ms"], page[-1]["event_id"])

    async def joined_claimants(self, room_id: str) -> Dict[str, str]:
        """claimant -> invitation id, for this course's joined rows."""
        await self.ensure()

        def select(txn: Any) -> Dict[str, str]:
            txn.execute(
                "SELECT claimant, id FROM pangea_student_invitation"
                " WHERE course_room_id = ? AND state = 'joined'",
                (room_id,),
            )
            return {r[0]: r[1] for r in txn.fetchall()}

        return await self.db.runInteraction("pangea_invitation_claimants", select)

    async def managed_record(
        self, user_id: str, room_id: str
    ) -> Optional[Dict[str, Any]]:
        await self.ensure()

        def select(txn: Any) -> Optional[Dict]:
            txn.execute(
                "SELECT user_id, course_room_id, created_at_ms"
                " FROM pangea_managed_account WHERE user_id = ? AND course_room_id = ?",
                (user_id, room_id),
            )
            raw = txn.fetchone()
            if raw is None:
                return None
            return dict(zip(("user_id", "course_room_id", "created_at_ms"), raw))

        return await self.db.runInteraction("pangea_managed_account_get", select)

    # --- writes ---

    async def add(
        self,
        room_id: str,
        entries: Sequence[Tuple[Optional[str], str]],
        source: str,
        invited_by: str,
        now_ms: int,
        new_id: Any,
        member_user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Add (email as entered or None, email key) pairs to a course.
        ``member_user_id`` is the course member an "Invite to a seat" row is
        made for (``source=member``); None for every other source.

        Per (course, key): a live row is returned unchanged; a ``revoked`` or
        ``left`` row is reset to a fresh ``invited`` row from this inviter,
        source and address (claimant and Canvas identity cleared, its
        requests and decisions deleted, send count kept); otherwise a row is
        created. One ``students_added`` event counts the rows created or
        reset (none when nothing changed); ``invited_by`` is its actor.
        Returns one row per entry, in entry order.
        """
        await self.ensure()

        def write(txn: Any) -> List[Dict]:
            by_key: Dict[str, Dict] = {}
            changed: List[str] = []
            for email, key in entries:
                if key in by_key:
                    continue
                txn.execute(
                    "INSERT INTO pangea_student_invitation"
                    " (id, course_room_id, email_key, email, state, source,"
                    "  invited_by, send_count, created_at_ms, member_user_id)"
                    " VALUES (?, ?, ?, ?, 'invited', ?, ?, 0, ?, ?)"
                    " ON CONFLICT (course_room_id, email_key) DO NOTHING",
                    (
                        new_id(),
                        room_id,
                        key,
                        email,
                        source,
                        invited_by,
                        now_ms,
                        member_user_id,
                    ),
                )
                inserted = txn.rowcount == 1
                row = _select_one(
                    txn,
                    "WHERE course_room_id = ? AND email_key = ?",
                    (room_id, key),
                )
                if row is None:
                    raise RuntimeError("invitation row missing after insert")
                if inserted:
                    changed.append(row["id"])
                if row["state"] in (STATE_REVOKED, STATE_LEFT):
                    changed.append(row["id"])
                    # A fresh invitation from this inviter, source and
                    # address; only the send count carries over. A Canvas
                    # identity is cleared: this path is never a Canvas import.
                    txn.execute(
                        "UPDATE pangea_student_invitation SET state = 'invited',"
                        " claimant = NULL, joined_at_ms = NULL, email = ?,"
                        " source = ?, invited_by = ?, created_at_ms = ?,"
                        " lti_issuer = NULL, lti_context_id = NULL,"
                        " lti_user_id = NULL, member_user_id = ? WHERE id = ?",
                        (email, source, invited_by, now_ms, member_user_id, row["id"]),
                    )
                    txn.execute(
                        "DELETE FROM pangea_invitation_ack WHERE invitation_id = ?",
                        (row["id"],),
                    )
                    row = _select_one(txn, "WHERE id = ?", (row["id"],))
                    if row is None:
                        raise RuntimeError("invitation row missing after reset")
                by_key[key] = row
            if changed:
                record_event(
                    txn,
                    room_id=room_id,
                    actor=invited_by,
                    action=STUDENTS_ADDED,
                    now_ms=now_ms,
                    invitation_id=changed[0] if len(changed) == 1 else None,
                    count=len(changed),
                )
            return [by_key[key] for _, key in entries]

        return await self.db.runInteraction("pangea_student_invitation_add", write)

    async def import_canvas(
        self,
        room_id: str,
        issuer: str,
        context_id: str,
        learners: Sequence[Tuple[str, str, str]],
        invited_by: str,
        now_ms: int,
        new_id: Any,
    ) -> Dict[str, Any]:
        """Upsert a Canvas roster (T11): (Canvas user id, email as Canvas
        sent it, canonical key) per learner with a valid email. In one
        transaction, per learner:

        1. a row of this course already bound to (issuer, user id): unchanged,
           its email never rewritten;
        2. else a row with the same canonical email: no Canvas identity ->
           the identity is attached and its state kept (``attached``); bound
           to another Canvas user -> unchanged, reported in ``conflicts``;
        3. else a new ``invited`` row, ``source=canvas`` (``imported``).

        Concurrent imports converge through the unique indexes: an insert or
        attach that a concurrent import beat fails on the index (Postgres
        reports a plain unique violation, which Synapse does not retry), and
        the whole transaction is run again on the new state, where step 1 or
        2 now finds that row.
        """
        await self.ensure()

        def write(txn: Any) -> Dict[str, Any]:
            counts = {"imported": 0, "attached": 0, "unchanged": 0}
            conflicts: List[str] = []
            for user_id, email, key in learners:
                bound = _select_one(
                    txn,
                    "WHERE course_room_id = ? AND lti_issuer = ? AND lti_user_id = ?",
                    (room_id, issuer, user_id),
                )
                if bound is not None:
                    counts["unchanged"] += 1
                    continue
                same_email = _select_one(
                    txn,
                    "WHERE course_room_id = ? AND email_key = ?",
                    (room_id, key),
                )
                if same_email is not None:
                    if same_email["lti_user_id"] is not None:
                        counts["unchanged"] += 1
                        if same_email["id"] not in conflicts:
                            conflicts.append(same_email["id"])
                        continue
                    txn.execute(
                        "UPDATE pangea_student_invitation SET lti_issuer = ?,"
                        " lti_context_id = ?, lti_user_id = ?"
                        " WHERE id = ? AND lti_user_id IS NULL",
                        (issuer, context_id, user_id, same_email["id"]),
                    )
                    if txn.rowcount != 1:
                        raise RuntimeError("invitation identity changed under lock")
                    counts["attached"] += 1
                    continue
                txn.execute(
                    "INSERT INTO pangea_student_invitation"
                    " (id, course_room_id, email_key, email, state, source,"
                    "  invited_by, send_count, created_at_ms,"
                    "  lti_issuer, lti_context_id, lti_user_id)"
                    " VALUES (?, ?, ?, ?, 'invited', ?, ?, 0, ?, ?, ?, ?)",
                    (
                        new_id(),
                        room_id,
                        key,
                        email,
                        SOURCE_CANVAS,
                        invited_by,
                        now_ms,
                        issuer,
                        context_id,
                        user_id,
                    ),
                )
                counts["imported"] += 1
            record_event(
                txn,
                room_id=room_id,
                actor=invited_by,
                action=CANVAS_ROSTER_IMPORTED,
                now_ms=now_ms,
                count=counts["imported"],
            )
            return {**counts, "conflicts": conflicts}

        for attempt in range(IMPORT_ATTEMPTS):
            try:
                return await self.db.runInteraction(
                    "pangea_student_invitation_import_canvas", write
                )
            except Exception as error:
                if not is_integrity_error(error) or attempt + 1 == IMPORT_ATTEMPTS:
                    raise
        raise RuntimeError("unreachable")

    async def reserve_send(
        self,
        room_id: str,
        invitation_id: str,
        expected: int,
        now_ms: int,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Compare-and-set the send count before a send. The send's event is
        written by ``record_sent`` once the email is accepted.

        Returns ("reserved", row before), ("not_invited", None) or
        ("stale", None)."""
        await self.ensure()

        def write(txn: Any) -> Tuple[str, Optional[Dict]]:
            row = _select_one(
                txn,
                "WHERE id = ? AND course_room_id = ?",
                (invitation_id, room_id),
            )
            if row is None or row["state"] != STATE_INVITED:
                return "not_invited", None
            txn.execute(
                "UPDATE pangea_student_invitation"
                " SET send_count = send_count + 1, last_sent_at_ms = ?"
                " WHERE id = ? AND state = 'invited' AND send_count = ?",
                (now_ms, invitation_id, expected),
            )
            if txn.rowcount != 1:
                return "stale", None
            return "reserved", row

        return await self.db.runInteraction("pangea_student_invitation_send", write)

    async def release_send(self, before: Dict[str, Any]) -> None:
        """Undo a reservation whose email was not sent, unless the row moved
        on. A failed send wrote no event, so the ledger is untouched."""

        def write(txn: Any) -> None:
            txn.execute(
                "UPDATE pangea_student_invitation"
                " SET send_count = ?, last_sent_at_ms = ?"
                " WHERE id = ? AND send_count = ?",
                (
                    before["send_count"],
                    before["last_sent_at_ms"],
                    before["id"],
                    before["send_count"] + 1,
                ),
            )

        await self.db.runInteraction("pangea_student_invitation_unsend", write)

    async def record_sent(
        self, before: Dict[str, Any], actor: str, now_ms: int
    ) -> None:
        """The email of a reserved send was accepted: append its
        ``invite_sent`` (first send) or ``invite_resent`` event. This is the
        step that completes the send, so a failed send writes no event."""
        await self.ensure()

        def write(txn: Any) -> None:
            record_event(
                txn,
                room_id=before["course_room_id"],
                actor=actor,
                action=INVITE_SENT if before["send_count"] == 0 else INVITE_RESENT,
                now_ms=now_ms,
                invitation_id=before["id"],
            )

        await self.db.runInteraction("pangea_student_invitation_sent", write)

    async def revoke(
        self, room_id: str, invitation_id: str, actor: str, now_ms: int
    ) -> Optional[Dict]:
        """Revoke; release the managed record if it was joined. None if no
        such invitation in this course."""
        await self.ensure()

        def write(txn: Any) -> Optional[Dict]:
            _lock(txn, invitation_id)
            row = _select_one(
                txn,
                "WHERE id = ? AND course_room_id = ?",
                (invitation_id, room_id),
            )
            if row is None:
                return None
            if row["state"] == STATE_REVOKED:
                return row
            if row["state"] == STATE_JOINED and row["claimant"] is not None:
                _delete_managed(txn, row["claimant"], room_id)
            txn.execute(
                "UPDATE pangea_student_invitation SET state = 'revoked' WHERE id = ?",
                (invitation_id,),
            )
            record_event(
                txn,
                room_id=room_id,
                actor=actor,
                action=INVITATION_WITHDRAWN,
                now_ms=now_ms,
                invitation_id=invitation_id,
            )
            return _select_one(txn, "WHERE id = ?", (invitation_id,))

        return await self.db.runInteraction("pangea_student_invitation_revoke", write)

    async def release_on_leave(
        self, room_id: str, user_id: str, now_ms: int
    ) -> Optional[str]:
        """The claimant left, or was removed from, the course: set its joined
        invitation ``left`` and delete the managed record. Returns the
        invitation id released, if any."""
        await self.ensure()

        def write(txn: Any) -> Optional[str]:
            row = _joined_in_course(txn, room_id, user_id)
            if row is None:
                return None
            _lock(txn, row["id"])
            txn.execute(
                "UPDATE pangea_student_invitation SET state = 'left'"
                " WHERE id = ? AND state = 'joined' AND claimant = ?",
                (row["id"], user_id),
            )
            if txn.rowcount != 1:
                return None
            _delete_managed(txn, user_id, room_id)
            record_event(
                txn,
                room_id=room_id,
                actor=SYSTEM,
                action=STUDENT_LEFT,
                now_ms=now_ms,
                invitation_id=row["id"],
            )
            return row["id"]

        return await self.db.runInteraction("pangea_student_invitation_left", write)

    async def set_managed(
        self, invitation_id: str, user_id: str, managed: bool, now_ms: int
    ) -> str:
        """Apply the managed-record rule (C2.5) to one joined invitation:
        ``managed`` inserts the record, otherwise it is deleted. Idempotent.
        Returns "inserted", "deleted", "unchanged" or "not_joined"."""
        await self.ensure()

        def write(txn: Any) -> str:
            _lock(txn, invitation_id)
            row = _select_one(txn, "WHERE id = ?", (invitation_id,))
            if row is None or row["state"] != STATE_JOINED:
                return "not_joined"
            if row["claimant"] != user_id:
                return "not_joined"
            room_id = row["course_room_id"]
            if not managed:
                _delete_managed(txn, user_id, room_id)
                return "deleted" if txn.rowcount == 1 else "unchanged"
            _insert_managed(txn, user_id, room_id, now_ms)
            return "inserted" if txn.rowcount == 1 else "unchanged"

        return await self.db.runInteraction("pangea_managed_account_set", write)

    async def record_request(
        self, invitation_id: str, user_id: str, now_ms: int
    ) -> Tuple[str, Optional[Dict]]:
        """Record this account's request on an ``invited`` row (the caller
        has checked that the account does not have the invited address
        verified). Idempotent: an existing request and its decision stay.
        Returns ("ok", request) or ("not_live", None)."""
        await self.ensure()

        def write(txn: Any) -> Tuple[str, Optional[Dict]]:
            _lock(txn, invitation_id)
            row = _select_one(txn, "WHERE id = ?", (invitation_id,))
            if row is None or row["state"] != STATE_INVITED:
                return "not_live", None
            txn.execute(
                "INSERT INTO pangea_invitation_ack"
                " (invitation_id, user_id, requested_at_ms, decision)"
                " VALUES (?, ?, ?, NULL)"
                " ON CONFLICT (invitation_id, user_id) DO NOTHING",
                (invitation_id, user_id, now_ms),
            )
            if txn.rowcount == 1:
                record_event(
                    txn,
                    room_id=row["course_room_id"],
                    actor=SYSTEM,
                    action=REQUEST_MADE,
                    now_ms=now_ms,
                    invitation_id=invitation_id,
                )
            txn.execute(
                _REQUEST_SELECT + "WHERE invitation_id = ? AND user_id = ?",
                (invitation_id, user_id),
            )
            return "ok", _request(txn.fetchone())

        return await self.db.runInteraction("pangea_invitation_request", write)

    async def deny(
        self, invitation_id: str, user_id: str, actor: str, now_ms: int
    ) -> Tuple[str, Optional[Dict]]:
        """Deny this account's request on an ``invited`` row. Returns
        ("ok", row), ("no_request", None) or ("not_live", row)."""
        await self.ensure()

        def write(txn: Any) -> Tuple[str, Optional[Dict]]:
            _lock(txn, invitation_id)
            row = _select_one(txn, "WHERE id = ?", (invitation_id,))
            if row is None:
                return "no_request", None
            txn.execute(
                "SELECT decision FROM pangea_invitation_ack"
                " WHERE invitation_id = ? AND user_id = ?",
                (invitation_id, user_id),
            )
            request = txn.fetchone()
            if request is None:
                return "no_request", None
            if row["state"] != STATE_INVITED:
                return "not_live", row
            if request[0] != DECISION_DENIED:
                txn.execute(
                    "UPDATE pangea_invitation_ack SET decision = 'denied'"
                    " WHERE invitation_id = ? AND user_id = ?",
                    (invitation_id, user_id),
                )
                record_event(
                    txn,
                    room_id=row["course_room_id"],
                    actor=actor,
                    action=REQUEST_DENIED,
                    now_ms=now_ms,
                    invitation_id=invitation_id,
                )
            return "ok", row

        return await self.db.runInteraction("pangea_invitation_deny", write)

    async def claim_txn(
        self,
        invitation_id: str,
        user_id: str,
        *,
        email_match: bool,
        grant: bool,
        now_ms: int,
        managed: bool = True,
        canvas_identity: Optional[CanvasIdentity] = None,
        actor: str = SYSTEM,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """The claim's database step (C2.4 step 2), all or nothing.

        Claims only an ``invited`` row, and only on one of (seats amendment
        2026-10-10): the account's verified email matches (``email_match``);
        its request on the row is granted, now (``grant``, the teacher
        ``actor``'s Grant) or before; or ``canvas_identity`` (issuer, Canvas
        course, Canvas user id of the account's own Canvas link) equals the
        identity the row was imported with. And only when the account holds
        no other joined invitation in the course. A grant is recorded only
        together with the claim it allows. ``managed`` is False when the
        claimant administers the course (owner amendment 2026-10-09): no
        managed record is written. The claim and a grant are recorded as
        events in the same transaction.
        """
        await self.ensure()

        def write(txn: Any) -> Tuple[str, Optional[Dict]]:
            _lock(txn, invitation_id)
            row = _select_one(txn, "WHERE id = ?", (invitation_id,))
            if row is None:
                return NOT_LIVE, None
            if row["state"] == STATE_JOINED and row["claimant"] == user_id:
                return CLAIMED, row
            if row["state"] != STATE_INVITED:
                return NOT_LIVE, row
            txn.execute(
                "SELECT decision FROM pangea_invitation_ack"
                " WHERE invitation_id = ? AND user_id = ?",
                (invitation_id, user_id),
            )
            request = txn.fetchone()
            granted = request is not None and (grant or request[0] == DECISION_GRANTED)
            if not (email_match or granted or canvas_match(row, canvas_identity)):
                return NOT_ELIGIBLE, row
            room_id = row["course_room_id"]
            # One claimed invitation per (course, account). The joined-per-
            # course index backs this for concurrent claims.
            if _joined_in_course(txn, room_id, user_id) is not None:
                return ALREADY_CLAIMED_IN_COURSE, row
            if managed:
                _insert_managed(txn, user_id, room_id, now_ms)
                if txn.rowcount != 1:
                    return ALREADY_CLAIMED_IN_COURSE, row
            if grant and request is not None and request[0] != DECISION_GRANTED:
                txn.execute(
                    "UPDATE pangea_invitation_ack SET decision = 'granted'"
                    " WHERE invitation_id = ? AND user_id = ?",
                    (invitation_id, user_id),
                )
                record_event(
                    txn,
                    room_id=room_id,
                    actor=actor,
                    action=REQUEST_GRANTED,
                    now_ms=now_ms,
                    invitation_id=invitation_id,
                )
            txn.execute(
                "UPDATE pangea_student_invitation"
                " SET state = 'joined', claimant = ?, joined_at_ms = ?"
                " WHERE id = ? AND state = 'invited'",
                (user_id, now_ms, invitation_id),
            )
            record_event(
                txn,
                room_id=room_id,
                actor=SYSTEM,
                action=INVITATION_CLAIMED,
                now_ms=now_ms,
                invitation_id=invitation_id,
            )
            return CLAIMED, _select_one(txn, "WHERE id = ?", (invitation_id,))

        try:
            return await self.db.runInteraction(
                "pangea_student_invitation_claim", write
            )
        except Exception as error:
            if is_integrity_error(error):
                # The joined-per-course index caught a concurrent claim of
                # another invitation in this course by the same account.
                return ALREADY_CLAIMED_IN_COURSE, None
            raise


def _columns(txn: Any, table: str) -> List[str]:
    if isinstance(getattr(txn, "database_engine", None), PostgresEngine):
        txn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        )
        return [r[0] for r in txn.fetchall()]
    txn.execute("SELECT name FROM pragma_table_info(?)", (table,))
    return [r[0] for r in txn.fetchall()]


def migrate(txn: Any) -> None:
    """Bring tables created by earlier builds to the current columns, in
    place and idempotently (each step runs only when still needed):

    - ``member_user_id`` on invitations (D2);
    - requests (seats amendment 2026-10-10): ``acked_at_ms`` becomes
      ``requested_at_ms`` and ``disclosure_version`` is dropped;
    - the managed record keeps only ``created_at_ms``: ``since_ms`` is
      renamed and ``invited_by`` dropped.
    """
    invitation = _columns(txn, "pangea_student_invitation")
    if "member_user_id" not in invitation:
        txn.execute(
            "ALTER TABLE pangea_student_invitation ADD COLUMN member_user_id TEXT"
        )
    request = _columns(txn, "pangea_invitation_ack")
    if "acked_at_ms" in request and "requested_at_ms" not in request:
        txn.execute(
            "ALTER TABLE pangea_invitation_ack"
            " RENAME COLUMN acked_at_ms TO requested_at_ms"
        )
    if "disclosure_version" in request:
        txn.execute("ALTER TABLE pangea_invitation_ack DROP COLUMN disclosure_version")
    managed = _columns(txn, "pangea_managed_account")
    if "since_ms" in managed and "created_at_ms" not in managed:
        txn.execute(
            "ALTER TABLE pangea_managed_account RENAME COLUMN since_ms TO created_at_ms"
        )
    if "invited_by" in managed:
        txn.execute("ALTER TABLE pangea_managed_account DROP COLUMN invited_by")


def _insert_managed(txn: Any, user_id: str, room_id: str, now_ms: int) -> None:
    txn.execute(
        "INSERT INTO pangea_managed_account (user_id, course_room_id, created_at_ms)"
        " VALUES (?, ?, ?) ON CONFLICT (user_id, course_room_id) DO NOTHING",
        (user_id, room_id, now_ms),
    )


def _delete_managed(txn: Any, user_id: str, room_id: str) -> None:
    txn.execute(
        "DELETE FROM pangea_managed_account WHERE user_id = ? AND course_room_id = ?",
        (user_id, room_id),
    )
