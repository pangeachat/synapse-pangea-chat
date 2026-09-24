"""The address a requested course was created for, and whether it is claimed.

A course created by ``create_course_space`` has no human member until the
teacher uses the claim link, so the address it was requested from is recorded
here: the claim notice goes to that address, not to whichever account used the
link (knock-with-code.instructions.md, "Claiming a course").

It lives in a module table rather than in room state because every member of a
course can read its room state, and the address must not be visible to the
students who join (create-course-space.instructions.md).

The row is also what makes the claim single use under concurrency. The admin
code is burned from join rules only after the claimer is promoted, so two
requests that both read the code before the burn would otherwise both be
promoted; the conditional update in ``claim`` lets exactly one of them through.
"""

from __future__ import annotations

from typing import Any, Optional

import attr

COURSE_CLAIM_TABLE = "pangea_course_claim"

# Literal statements rather than f-strings over the table name: see
# moderation/disposition.py for why, and `STATEMENTS` for the drift test.
_CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS pangea_course_claim (
        room_id TEXT PRIMARY KEY,
        requested_email TEXT,
        created_at_ms BIGINT NOT NULL,
        claimed_by TEXT,
        claimed_at_ms BIGINT,
        notice_sent_at_ms BIGINT
    )
"""

_INSERT_SQL = """
    INSERT INTO pangea_course_claim (room_id, requested_email, created_at_ms)
    VALUES (?, ?, ?)
"""

_SELECT_SQL = """
    SELECT requested_email, claimed_by, notice_sent_at_ms
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

# Once the notice is sent the address has done its job, so it is cleared: a
# claimed course keeps who claimed it and when, and no longer holds anybody's
# email address.
_NOTICE_SENT_SQL = """
    UPDATE pangea_course_claim
    SET notice_sent_at_ms = ?, requested_email = NULL
    WHERE room_id = ? AND claimed_by = ?
"""

#: Every statement above, for the drift test.
STATEMENTS = (
    _CREATE_TABLE_SQL,
    _INSERT_SQL,
    _SELECT_SQL,
    _CLAIM_SQL,
    _NOTICE_SENT_SQL,
)


@attr.s(frozen=True, auto_attribs=True)
class CourseClaim:
    requested_email: Optional[str]
    claimed_by: Optional[str]
    notice_sent: bool


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

        await self._db_pool.runInteraction("pangea_course_claim_create", _create)
        self._table_ready = True

    async def record(self, room_id: str, requested_email: str, now_ms: int) -> None:
        await self._ensure_table()

        def _insert(txn: Any) -> None:
            txn.execute(_INSERT_SQL, (room_id, requested_email, now_ms))

        await self._db_pool.runInteraction("pangea_course_claim_record", _insert)

    async def get(self, room_id: str) -> Optional[CourseClaim]:
        """The claim record for a room, or None when the room has none.

        A room has none unless ``create_course_space`` made it for a requested
        address: courses teachers create in the client carry an admin code too,
        and their claims send no notice.
        """
        await self._ensure_table()

        def _select(txn: Any) -> Optional[CourseClaim]:
            txn.execute(_SELECT_SQL, (room_id,))
            row = txn.fetchone()
            if row is None:
                return None
            return CourseClaim(
                requested_email=row[0],
                claimed_by=row[1],
                notice_sent=row[2] is not None,
            )

        return await self._db_pool.runInteraction("pangea_course_claim_get", _select)

    async def claim(self, room_id: str, user_id: str, now_ms: int) -> bool:
        """Take the claim for ``user_id``. False when someone else holds it."""
        await self._ensure_table()

        def _claim(txn: Any) -> bool:
            txn.execute(_CLAIM_SQL, (user_id, now_ms, room_id, user_id))
            return txn.rowcount == 1

        return await self._db_pool.runInteraction("pangea_course_claim_claim", _claim)

    async def mark_notice_sent(self, room_id: str, user_id: str, now_ms: int) -> None:
        await self._ensure_table()

        def _mark(txn: Any) -> None:
            txn.execute(_NOTICE_SENT_SQL, (now_ms, room_id, user_id))

        await self._db_pool.runInteraction("pangea_course_claim_notice_sent", _mark)
