"""The address a requested course was created for, and whether it is claimed.

A course created by ``create_course_space`` has no human member until the
teacher uses the claim link, so the address it was requested from is recorded
here: the claim notice goes to that address, not to whichever account used the
link (knock-with-code.instructions.md, "Claiming a course").

It lives in a module table rather than in room state because every member of a
course can read its room state, and the address must not be visible to the
students who join (create-course-space.instructions.md).

The row does four jobs beyond holding the address:

- It holds the claim codes. A requested course's admin codes are never
  written to room state: every member can read ``m.room.join_rules``, so a
  student who joined with the class code could read one there and claim the
  course. Only their digests are kept, one row each in
  ``pangea_course_claim_code``: the code the course was created with, and one
  more for every reminder (``add_code``). ``rooms_for_admin_code`` is how
  ``knock_with_code`` finds the room from any of them. Promotion spends them
  all at once. ``pangea_course_claim.admin_code_sha256`` still holds the
  creation code for rows written before the code table existed; the table is
  backfilled from it and it is otherwise unread.
- It makes the claim single use under concurrency: the conditional update in
  ``claim`` lets one account through.
- It makes the claim notice send once. ``reserve_notice`` takes a lease before
  a send, so two requests from the claimer, or two workers retrying, cannot
  both send it.
- It keeps a failed notice owed. A notice whose send failed stays unsent, and
  ``outstanding_notices`` hands it to the background retry once its lease runs
  out, up to ``MAX_NOTICE_ATTEMPTS``.
"""

from __future__ import annotations

import hashlib
from typing import Any, List, Optional, Tuple

import attr

COURSE_CLAIM_TABLE = "pangea_course_claim"
COURSE_CLAIM_CODE_TABLE = "pangea_course_claim_code"

#: Sends of one claim notice before the retry gives up on it.
MAX_NOTICE_ATTEMPTS = 8

# Literal statements rather than f-strings over the table name: see
# moderation/disposition.py for why, and `STATEMENTS` for the drift test.
_CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS pangea_course_claim (
        room_id TEXT PRIMARY KEY,
        requested_email TEXT,
        admin_code_sha256 TEXT NOT NULL,
        created_at_ms BIGINT NOT NULL,
        claimed_by TEXT,
        claimed_at_ms BIGINT,
        promoted_at_ms BIGINT,
        notice_attempts INTEGER NOT NULL DEFAULT 0,
        notice_leased_until_ms BIGINT,
        notice_sent_at_ms BIGINT
    )
"""

_CREATE_CODE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS pangea_course_claim_code (
        admin_code_sha256 TEXT PRIMARY KEY,
        room_id TEXT NOT NULL,
        created_at_ms BIGINT NOT NULL
    )
"""

_CREATE_CODE_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS pangea_course_claim_code_room_idx
    ON pangea_course_claim_code (room_id)
"""

# Courses created before the code table carry their one code in the claim row.
# Copied over on every start; the NOT EXISTS makes it a no-op once done.
_BACKFILL_CODES_SQL = """
    INSERT INTO pangea_course_claim_code (admin_code_sha256, room_id, created_at_ms)
    SELECT k.admin_code_sha256, k.room_id, k.created_at_ms
    FROM pangea_course_claim k
    WHERE NOT EXISTS (
        SELECT 1 FROM pangea_course_claim_code c
        WHERE c.admin_code_sha256 = k.admin_code_sha256
    )
"""

_INSERT_CODE_SQL = """
    INSERT INTO pangea_course_claim_code (admin_code_sha256, room_id, created_at_ms)
    VALUES (?, ?, ?)
"""

_DELETE_CODE_SQL = """
    DELETE FROM pangea_course_claim_code WHERE admin_code_sha256 = ?
"""

_INSERT_SQL = """
    INSERT INTO pangea_course_claim
        (room_id, requested_email, admin_code_sha256, created_at_ms)
    VALUES (?, ?, ?, ?)
"""

_ROOMS_FOR_CODE_SQL = """
    SELECT c.room_id
    FROM pangea_course_claim_code c
    JOIN pangea_course_claim k ON k.room_id = c.room_id
    WHERE c.admin_code_sha256 = ? AND k.promoted_at_ms IS NULL
"""

_CODE_IN_USE_SQL = """
    SELECT 1 FROM pangea_course_claim_code WHERE admin_code_sha256 = ?
"""

_REMINDER_SQL = """
    SELECT requested_email, claimed_by
    FROM pangea_course_claim
    WHERE room_id = ?
"""

_SELECT_SQL = """
    SELECT claimed_by
    FROM pangea_course_claim
    WHERE room_id = ?
"""

# The same user may claim again: a claim whose invite or promotion failed has
# to be retryable by the person who won it, and nobody else.
_CLAIM_SQL = """
    UPDATE pangea_course_claim
    SET claimed_by = ?, claimed_at_ms = ?
    WHERE room_id = ? AND (claimed_by IS NULL OR claimed_by = ?)
"""

# Promotion is what makes the notice owed: a claim whose invite failed has not
# happened yet, and the retry must not announce it.
_PROMOTED_SQL = """
    UPDATE pangea_course_claim
    SET promoted_at_ms = ?
    WHERE room_id = ? AND claimed_by = ? AND promoted_at_ms IS NULL
"""

_RESERVE_NOTICE_SQL = """
    UPDATE pangea_course_claim
    SET notice_leased_until_ms = ?, notice_attempts = notice_attempts + 1
    WHERE room_id = ? AND claimed_by = ?
        AND promoted_at_ms IS NOT NULL
        AND notice_sent_at_ms IS NULL
        AND requested_email IS NOT NULL
        AND notice_attempts < ?
        AND (notice_leased_until_ms IS NULL OR notice_leased_until_ms <= ?)
"""

_SELECT_NOTICE_SQL = """
    SELECT requested_email, notice_attempts
    FROM pangea_course_claim
    WHERE room_id = ?
"""

# Once the notice is sent the address has done its job, so it is cleared: a
# claimed course keeps who claimed it and when, and no longer holds anybody's
# email address.
_NOTICE_SENT_SQL = """
    UPDATE pangea_course_claim
    SET notice_sent_at_ms = ?, requested_email = NULL,
        notice_leased_until_ms = NULL
    WHERE room_id = ? AND claimed_by = ?
"""

_OUTSTANDING_SQL = """
    SELECT room_id, claimed_by
    FROM pangea_course_claim
    WHERE promoted_at_ms IS NOT NULL
        AND notice_sent_at_ms IS NULL
        AND requested_email IS NOT NULL
        AND notice_attempts < ?
        AND (notice_leased_until_ms IS NULL OR notice_leased_until_ms <= ?)
"""

#: Every statement above, for the drift test.
STATEMENTS = (
    _CREATE_TABLE_SQL,
    _CREATE_CODE_TABLE_SQL,
    _CREATE_CODE_INDEX_SQL,
    _BACKFILL_CODES_SQL,
    _INSERT_CODE_SQL,
    _DELETE_CODE_SQL,
    _INSERT_SQL,
    _ROOMS_FOR_CODE_SQL,
    _CODE_IN_USE_SQL,
    _REMINDER_SQL,
    _SELECT_SQL,
    _CLAIM_SQL,
    _PROMOTED_SQL,
    _RESERVE_NOTICE_SQL,
    _SELECT_NOTICE_SQL,
    _NOTICE_SENT_SQL,
    _OUTSTANDING_SQL,
)


def admin_code_digest(admin_code: str) -> str:
    # Codes match case-insensitively (get_rooms_with_access_code), so the
    # digest is of the lower-cased code.
    return hashlib.sha256(admin_code.lower().encode()).hexdigest()


@attr.s(frozen=True, auto_attribs=True)
class CourseClaim:
    claimed_by: Optional[str]


@attr.s(frozen=True, auto_attribs=True)
class ReminderTarget:
    """What a reminder needs from a room's claim record."""

    requested_email: Optional[str]
    claimed: bool


@attr.s(frozen=True, auto_attribs=True)
class NoticeReservation:
    requested_email: str
    attempt: int


class CourseClaimStore:
    """Reads and writes ``pangea_course_claim``."""

    def __init__(self, homeserver: Any) -> None:
        self._db_pool = homeserver.get_datastores().main.db_pool
        self._table_ready = False

    async def _ensure_table(self) -> None:
        if self._table_ready:
            return

        def _create(txn: Any) -> None:
            txn.execute(_CREATE_TABLE_SQL)
            txn.execute(_CREATE_CODE_TABLE_SQL)
            txn.execute(_CREATE_CODE_INDEX_SQL)
            txn.execute(_BACKFILL_CODES_SQL)

        await self._db_pool.runInteraction("pangea_course_claim_create", _create)
        self._table_ready = True

    async def record(
        self,
        room_id: str,
        requested_email: Optional[str],
        admin_code: str,
        now_ms: int,
    ) -> None:
        await self._ensure_table()

        digest = admin_code_digest(admin_code)

        def _insert(txn: Any) -> None:
            txn.execute(_INSERT_SQL, (room_id, requested_email, digest, now_ms))
            txn.execute(_INSERT_CODE_SQL, (digest, room_id, now_ms))

        await self._db_pool.runInteraction("pangea_course_claim_record", _insert)

    async def add_code(self, room_id: str, admin_code: str, now_ms: int) -> None:
        """Another claim code for a course: valid until the course is claimed."""
        await self._ensure_table()
        digest = admin_code_digest(admin_code)

        def _insert(txn: Any) -> None:
            txn.execute(_INSERT_CODE_SQL, (digest, room_id, now_ms))

        await self._db_pool.runInteraction("pangea_course_claim_add_code", _insert)

    async def remove_code(self, admin_code: str) -> None:
        """Withdraw a code that was never delivered."""
        await self._ensure_table()
        digest = admin_code_digest(admin_code)

        def _delete(txn: Any) -> None:
            txn.execute(_DELETE_CODE_SQL, (digest,))

        await self._db_pool.runInteraction("pangea_course_claim_remove_code", _delete)

    async def reminder_target(self, room_id: str) -> Optional[ReminderTarget]:
        """None when the room has no claim record."""
        await self._ensure_table()

        def _select(txn: Any) -> Optional[ReminderTarget]:
            txn.execute(_REMINDER_SQL, (room_id,))
            row = txn.fetchone()
            if row is None:
                return None
            return ReminderTarget(requested_email=row[0], claimed=row[1] is not None)

        return await self._db_pool.runInteraction(
            "pangea_course_claim_reminder_target", _select
        )

    async def rooms_for_admin_code(self, admin_code: str) -> List[str]:
        """Rooms whose unspent claim code this is."""
        await self._ensure_table()
        digest = admin_code_digest(admin_code)

        def _select(txn: Any) -> List[str]:
            txn.execute(_ROOMS_FOR_CODE_SQL, (digest,))
            return [row[0] for row in txn.fetchall()]

        return await self._db_pool.runInteraction(
            "pangea_course_claim_rooms_for_code", _select
        )

    async def code_in_use(self, admin_code: str) -> bool:
        await self._ensure_table()
        digest = admin_code_digest(admin_code)

        def _select(txn: Any) -> bool:
            txn.execute(_CODE_IN_USE_SQL, (digest,))
            return txn.fetchone() is not None

        return await self._db_pool.runInteraction(
            "pangea_course_claim_code_in_use", _select
        )

    async def get(self, room_id: str) -> Optional[CourseClaim]:
        """The claim record for a room, or None when the room has none.

        A room has none unless ``create_course_space`` made it: courses
        teachers create in the client carry their admin code in join rules,
        and it is not a claim.
        """
        await self._ensure_table()

        def _select(txn: Any) -> Optional[CourseClaim]:
            txn.execute(_SELECT_SQL, (room_id,))
            row = txn.fetchone()
            if row is None:
                return None
            return CourseClaim(claimed_by=row[0])

        return await self._db_pool.runInteraction("pangea_course_claim_get", _select)

    async def claim(self, room_id: str, user_id: str, now_ms: int) -> bool:
        """Take the claim for ``user_id``. False when someone else holds it."""
        await self._ensure_table()

        def _claim(txn: Any) -> bool:
            txn.execute(_CLAIM_SQL, (user_id, now_ms, room_id, user_id))
            return txn.rowcount == 1

        return await self._db_pool.runInteraction("pangea_course_claim_claim", _claim)

    async def mark_promoted(self, room_id: str, user_id: str, now_ms: int) -> None:
        await self._ensure_table()

        def _mark(txn: Any) -> None:
            txn.execute(_PROMOTED_SQL, (now_ms, room_id, user_id))

        await self._db_pool.runInteraction("pangea_course_claim_promoted", _mark)

    async def reserve_notice(
        self, room_id: str, user_id: str, now_ms: int, lease_ms: int
    ) -> Optional[NoticeReservation]:
        """Take the lease on sending this claim's notice, if it is owed.

        None when it is not owed, or when another request or worker holds the
        lease. The lease is released by ``mark_notice_sent``, or by running
        out, which is what lets a failed send be retried.
        """
        await self._ensure_table()

        def _reserve(txn: Any) -> Optional[NoticeReservation]:
            txn.execute(
                _RESERVE_NOTICE_SQL,
                (
                    now_ms + lease_ms,
                    room_id,
                    user_id,
                    MAX_NOTICE_ATTEMPTS,
                    now_ms,
                ),
            )
            if txn.rowcount != 1:
                return None
            txn.execute(_SELECT_NOTICE_SQL, (room_id,))
            row = txn.fetchone()
            return NoticeReservation(requested_email=row[0], attempt=row[1])

        return await self._db_pool.runInteraction(
            "pangea_course_claim_reserve_notice", _reserve
        )

    async def mark_notice_sent(self, room_id: str, user_id: str, now_ms: int) -> None:
        await self._ensure_table()

        def _mark(txn: Any) -> None:
            txn.execute(_NOTICE_SENT_SQL, (now_ms, room_id, user_id))

        await self._db_pool.runInteraction("pangea_course_claim_notice_sent", _mark)

    async def outstanding_notices(self, now_ms: int) -> List[Tuple[str, str]]:
        """(room_id, claimer) for every owed notice whose lease has run out."""
        await self._ensure_table()

        def _select(txn: Any) -> List[Tuple[str, str]]:
            txn.execute(_OUTSTANDING_SQL, (MAX_NOTICE_ATTEMPTS, now_ms))
            return [(row[0], row[1]) for row in txn.fetchall()]

        return await self._db_pool.runInteraction(
            "pangea_course_claim_outstanding", _select
        )
