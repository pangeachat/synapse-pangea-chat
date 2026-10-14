"""Student invitations: one code per invited address, so a join names the invitation it came from.

A course invite email (``invite_by_email``) carries the invitation's own seven-character code as
its link, ``<app>/<code>``, which the app joins with through ``knock_with_code`` like any class
code. The code is never spent: it keeps opening the course, like the class link, and each join
through it is recorded, the first as the invitee's and later ones as further uses of a forwarded
link. The invited address is stored with the invitation, as a requested course's requesting
address is, so a later reminder can be sent to it and the dashboard can mark who joined by
invitation id rather than by guessing from an address (invite-by-email.instructions.md).
Only a digest of the code is stored.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional
from uuid import uuid4

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS pangea_student_invitation (
        invitation_id TEXT PRIMARY KEY, room_id TEXT NOT NULL, email TEXT NOT NULL,
        code_digest TEXT NOT NULL UNIQUE, created_by TEXT NOT NULL,
        created_at_ms BIGINT NOT NULL, first_used_by TEXT, first_used_at_ms BIGINT,
        uses INTEGER NOT NULL DEFAULT 0)""",
    """CREATE INDEX IF NOT EXISTS pangea_student_invitation_room
        ON pangea_student_invitation (room_id)""",
)
COLUMNS = (
    "invitation_id",
    "room_id",
    "email",
    "created_by",
    "created_at_ms",
    "first_used_by",
    "first_used_at_ms",
    "uses",
)
SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM pangea_student_invitation "


def code_digest(code: str) -> str:
    # Codes match case-insensitively (get_rooms_with_access_code).
    return hashlib.sha256(code.lower().encode()).hexdigest()


class StudentInvitationStore:
    def __init__(self, homeserver: Any):
        self.db = homeserver.get_datastores().main.db_pool
        self.ready = False

    async def ensure(self) -> None:
        if self.ready:
            return

        def create(txn):
            for sql in SCHEMA:
                txn.execute(sql)

        await self.db.runInteraction("pangea_student_invitation_schema", create)
        self.ready = True

    @staticmethod
    def row(row) -> Optional[Dict[str, Any]]:
        return None if row is None else dict(zip(COLUMNS, row))

    def public(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """The invitation as a read returns it: never the address."""
        return {k: v for k, v in row.items() if k != "email"}

    async def create(
        self, room_id: str, email: str, code: str, created_by: str, now: int
    ) -> str:
        await self.ensure()
        invitation_id = str(uuid4())

        def insert(txn):
            txn.execute(
                """INSERT INTO pangea_student_invitation
                (invitation_id, room_id, email, code_digest, created_by, created_at_ms)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (invitation_id, room_id, email, code_digest(code), created_by, now),
            )

        await self.db.runInteraction("pangea_student_invitation_create", insert)
        return invitation_id

    async def for_code(self, code: str) -> Optional[Dict[str, Any]]:
        await self.ensure()

        def select(txn):
            txn.execute(SELECT + "WHERE code_digest = ?", (code_digest(code),))
            return self.row(txn.fetchone())

        return await self.db.runInteraction("pangea_student_invitation_code", select)

    async def code_in_use(self, code: str) -> bool:
        return await self.for_code(code) is not None

    async def record_use(self, invitation_id: str, user_id: str, now: int) -> None:
        """A join through the invitation's code: the first names the invitee, every one counts."""
        await self.ensure()

        def update(txn):
            txn.execute(
                """UPDATE pangea_student_invitation
                SET uses = uses + 1,
                    first_used_by = COALESCE(first_used_by, ?),
                    first_used_at_ms = COALESCE(first_used_at_ms, ?)
                WHERE invitation_id = ?""",
                (user_id, now, invitation_id),
            )

        await self.db.runInteraction("pangea_student_invitation_use", update)

    async def for_room(self, room_id: str) -> List[Dict[str, Any]]:
        await self.ensure()

        def select(txn):
            txn.execute(SELECT + "WHERE room_id = ? ORDER BY created_at_ms", (room_id,))
            return [self.row(r) for r in txn.fetchall()]

        return await self.db.runInteraction("pangea_student_invitation_room", select)
