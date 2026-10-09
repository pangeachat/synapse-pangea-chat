"""The module's student invitation tables and every write to them.

Three tables (SPEC §6):

- ``pangea_student_invitation``: one row per (course, canonical email). States
  ``invited`` -> ``joined`` (claimed by one account) -> ``left`` (that account
  left, or was removed from, the course); ``revoked`` by a course admin. A
  ``revoked`` or ``left`` row is reset to ``invited`` when the email is added
  again. The ``lti_*`` columns hold the Canvas identity of an imported row.
- ``pangea_invitation_ack``: an account's confirmation of "Your teacher will
  manage this account" for one invitation, with the disclosure version it was
  shown and the teacher's decision (null, granted, denied).
- ``pangea_managed_account``: written by every claim, deleted when the
  invitation is revoked or its claimant leaves the course (release).

Every write that decides a claim locks the invitation row first (a no-op
``UPDATE``), so on Postgres's repeatable-read transactions a concurrent change
to the same row aborts one side, which Synapse retries against the new state.
Two partial unique indexes back the rules no lock can see across rows: one
joined invitation per (course, account) and one Canvas identity per course.

Emails never leave this file in a log line or an exception message.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

STATE_INVITED = "invited"
STATE_JOINED = "joined"
STATE_LEFT = "left"
STATE_REVOKED = "revoked"
LIVE_STATES = (STATE_INVITED, STATE_JOINED)

DECISION_GRANTED = "granted"
DECISION_DENIED = "denied"

SCHEMA = (
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
        acked_at_ms BIGINT NOT NULL,
        disclosure_version INTEGER NOT NULL,
        decision TEXT,
        PRIMARY KEY (invitation_id, user_id))""",
    """CREATE INDEX IF NOT EXISTS pangea_invitation_ack_user
        ON pangea_invitation_ack (user_id)""",
    """CREATE TABLE IF NOT EXISTS pangea_managed_account (
        user_id TEXT NOT NULL,
        course_room_id TEXT NOT NULL,
        invited_by TEXT NOT NULL,
        since_ms BIGINT NOT NULL,
        PRIMARY KEY (user_id, course_room_id))""",
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
)
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM pangea_student_invitation "
ACK_COLUMNS = (
    "invitation_id",
    "user_id",
    "acked_at_ms",
    "disclosure_version",
    "decision",
)
_ACK_SELECT = (
    "SELECT invitation_id, user_id, acked_at_ms, disclosure_version, decision"
    " FROM pangea_invitation_ack "
)

# Claim outcomes (claim_txn and StudentClaims.claim).
CLAIMED = "claimed"
NOT_LIVE = "not_live"
NOT_ELIGIBLE = "not_eligible"
ALREADY_CLAIMED_IN_COURSE = "already_claimed_in_course"


def _row(raw: Optional[Sequence[Any]]) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    return dict(zip(COLUMNS, raw))


def _ack(raw: Optional[Sequence[Any]]) -> Optional[Dict[str, Any]]:
    if raw is None:
        return None
    return dict(zip(ACK_COLUMNS, raw))


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

    async def acks_by(self, user_id: str) -> Dict[str, Dict[str, Any]]:
        """This account's confirmations, by invitation id."""
        await self.ensure()

        def select(txn: Any) -> Dict[str, Dict]:
            txn.execute(_ACK_SELECT + "WHERE user_id = ?", (user_id,))
            acks = [_ack(x) for x in txn.fetchall()]
            return {a["invitation_id"]: a for a in acks if a is not None}

        return await self.db.runInteraction("pangea_invitation_acks_by", select)

    async def get_ack(self, invitation_id: str, user_id: str) -> Optional[Dict]:
        await self.ensure()

        def select(txn: Any) -> Optional[Dict]:
            txn.execute(
                _ACK_SELECT + "WHERE invitation_id = ? AND user_id = ?",
                (invitation_id, user_id),
            )
            return _ack(txn.fetchone())

        return await self.db.runInteraction("pangea_invitation_ack_get", select)

    async def acks_in_room(self, room_id: str) -> List[Dict[str, Any]]:
        """Every confirmation of an ``invited`` row of this course."""
        await self.ensure()

        def select(txn: Any) -> List[Dict]:
            txn.execute(
                "SELECT a.invitation_id, a.user_id, a.acked_at_ms,"
                " a.disclosure_version, a.decision"
                " FROM pangea_invitation_ack a"
                " JOIN pangea_student_invitation i ON i.id = a.invitation_id"
                " WHERE i.course_room_id = ? AND i.state = 'invited'"
                " ORDER BY a.acked_at_ms, a.user_id",
                (room_id,),
            )
            return [a for a in (_ack(x) for x in txn.fetchall()) if a is not None]

        return await self.db.runInteraction("pangea_invitation_acks_room", select)

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
                "SELECT user_id, course_room_id, invited_by, since_ms"
                " FROM pangea_managed_account WHERE user_id = ? AND course_room_id = ?",
                (user_id, room_id),
            )
            raw = txn.fetchone()
            if raw is None:
                return None
            return dict(
                zip(("user_id", "course_room_id", "invited_by", "since_ms"), raw)
            )

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
    ) -> List[Dict[str, Any]]:
        """Add (email as entered or None, email key) pairs to a course.

        Per (course, key): a live row is returned unchanged; a ``revoked`` or
        ``left`` row is reset to ``invited`` (claimant cleared, its
        confirmations deleted, send count kept); otherwise a row is created.
        Returns one row per entry, in entry order.
        """
        await self.ensure()

        def write(txn: Any) -> List[Dict]:
            by_key: Dict[str, Dict] = {}
            for email, key in entries:
                if key in by_key:
                    continue
                txn.execute(
                    "INSERT INTO pangea_student_invitation"
                    " (id, course_room_id, email_key, email, state, source,"
                    "  invited_by, send_count, created_at_ms)"
                    " VALUES (?, ?, ?, ?, 'invited', ?, ?, 0, ?)"
                    " ON CONFLICT (course_room_id, email_key) DO NOTHING",
                    (new_id(), room_id, key, email, source, invited_by, now_ms),
                )
                row = _select_one(
                    txn,
                    "WHERE course_room_id = ? AND email_key = ?",
                    (room_id, key),
                )
                if row is None:
                    raise RuntimeError("invitation row missing after insert")
                if row["state"] in (STATE_REVOKED, STATE_LEFT):
                    txn.execute(
                        "UPDATE pangea_student_invitation SET state = 'invited',"
                        " claimant = NULL, joined_at_ms = NULL WHERE id = ?",
                        (row["id"],),
                    )
                    txn.execute(
                        "DELETE FROM pangea_invitation_ack WHERE invitation_id = ?",
                        (row["id"],),
                    )
                    row = _select_one(txn, "WHERE id = ?", (row["id"],))
                    if row is None:
                        raise RuntimeError("invitation row missing after reset")
                by_key[key] = row
            return [by_key[key] for _, key in entries]

        return await self.db.runInteraction("pangea_student_invitation_add", write)

    async def reserve_send(
        self, room_id: str, invitation_id: str, expected: int, now_ms: int
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Compare-and-set the send count before a send.

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
        """Undo a reservation whose email was not sent, unless the row moved on."""

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

    async def revoke(self, room_id: str, invitation_id: str) -> Optional[Dict]:
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
            return _select_one(txn, "WHERE id = ?", (invitation_id,))

        return await self.db.runInteraction("pangea_student_invitation_revoke", write)

    async def release_on_leave(self, room_id: str, user_id: str) -> Optional[str]:
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
            return row["id"]

        return await self.db.runInteraction("pangea_student_invitation_left", write)

    async def set_managed(
        self, invitation_id: str, user_id: str, managed: bool, now_ms: int
    ) -> str:
        """Apply the managed-record rule (C2.5) to one joined invitation:
        ``managed`` inserts the record from the claimant's confirmation,
        otherwise it is deleted. Idempotent. Returns "inserted", "deleted",
        "unchanged", "not_joined" or "no_ack"."""
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
            # The record rests on the claimant's own confirmation, whose
            # disclosure_version stays on the ack.
            txn.execute(
                "SELECT disclosure_version FROM pangea_invitation_ack"
                " WHERE invitation_id = ? AND user_id = ?",
                (invitation_id, user_id),
            )
            if txn.fetchone() is None:
                return "no_ack"
            txn.execute(
                "INSERT INTO pangea_managed_account"
                " (user_id, course_room_id, invited_by, since_ms)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (user_id, course_room_id) DO NOTHING",
                (user_id, room_id, row["invited_by"], now_ms),
            )
            return "inserted" if txn.rowcount == 1 else "unchanged"

        return await self.db.runInteraction("pangea_managed_account_set", write)

    async def record_ack(
        self, invitation_id: str, user_id: str, version: int, now_ms: int
    ) -> None:
        """Upsert this account's confirmation; a decision already taken stays."""
        await self.ensure()

        def write(txn: Any) -> None:
            txn.execute(
                "INSERT INTO pangea_invitation_ack"
                " (invitation_id, user_id, acked_at_ms, disclosure_version, decision)"
                " VALUES (?, ?, ?, ?, NULL)"
                " ON CONFLICT (invitation_id, user_id) DO UPDATE SET"
                " acked_at_ms = excluded.acked_at_ms,"
                " disclosure_version = excluded.disclosure_version",
                (invitation_id, user_id, now_ms, version),
            )

        await self.db.runInteraction("pangea_invitation_ack_record", write)

    async def deny(
        self, invitation_id: str, user_id: str
    ) -> Tuple[str, Optional[Dict]]:
        """Record a deny on an ``invited`` row. Returns ("ok", row),
        ("no_ack", None) or ("not_live", row)."""
        await self.ensure()

        def write(txn: Any) -> Tuple[str, Optional[Dict]]:
            _lock(txn, invitation_id)
            row = _select_one(txn, "WHERE id = ?", (invitation_id,))
            if row is None:
                return "no_ack", None
            txn.execute(
                "SELECT 1 FROM pangea_invitation_ack"
                " WHERE invitation_id = ? AND user_id = ?",
                (invitation_id, user_id),
            )
            if txn.fetchone() is None:
                return "no_ack", None
            if row["state"] != STATE_INVITED:
                return "not_live", row
            txn.execute(
                "UPDATE pangea_invitation_ack SET decision = 'denied'"
                " WHERE invitation_id = ? AND user_id = ?",
                (invitation_id, user_id),
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
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """The claim's database step (C2.4 step 2), all or nothing.

        Claims only an ``invited`` row this account has confirmed, when its
        verified email matches, or the teacher grants it now or granted it
        before, and when the account holds no other joined invitation in the
        course. A grant is recorded only together with the claim it allows.
        ``managed`` is False when the claimant administers the course (owner
        amendment 2026-10-09): a teacher is never managed by a course they
        administer, so no managed record is written.
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
            ack = txn.fetchone()
            if ack is None:
                return NOT_ELIGIBLE, row
            if not (email_match or grant or ack[0] == DECISION_GRANTED):
                return NOT_ELIGIBLE, row
            room_id = row["course_room_id"]
            # One claimed invitation per (course, account). The joined-per-
            # course index backs this for concurrent claims.
            if _joined_in_course(txn, room_id, user_id) is not None:
                return ALREADY_CLAIMED_IN_COURSE, row
            if managed:
                txn.execute(
                    "INSERT INTO pangea_managed_account"
                    " (user_id, course_room_id, invited_by, since_ms)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT (user_id, course_room_id) DO NOTHING",
                    (user_id, room_id, row["invited_by"], now_ms),
                )
                if txn.rowcount != 1:
                    return ALREADY_CLAIMED_IN_COURSE, row
            if grant:
                txn.execute(
                    "UPDATE pangea_invitation_ack SET decision = 'granted'"
                    " WHERE invitation_id = ? AND user_id = ?",
                    (invitation_id, user_id),
                )
            txn.execute(
                "UPDATE pangea_student_invitation"
                " SET state = 'joined', claimant = ?, joined_at_ms = ?"
                " WHERE id = ? AND state = 'invited'",
                (user_id, now_ms, invitation_id),
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


def _delete_managed(txn: Any, user_id: str, room_id: str) -> None:
    txn.execute(
        "DELETE FROM pangea_managed_account WHERE user_id = ? AND course_room_id = ?",
        (user_id, room_id),
    )
