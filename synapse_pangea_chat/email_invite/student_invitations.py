"""Student invitations: one code per invited address, so a join names the invitation it came from.

A course invite email (``invite_by_email``) carries the invitation's own seven-character code as
its link, ``<app>/<code>``, which the app joins with through ``knock_with_code`` like any class
code. The code is never spent: it keeps opening the course, like the class link, and each join
through it is recorded, the first as the invitee's and later ones as further uses of a forwarded
link. The invited address is stored with the invitation, as a requested course's requesting
address is, so a later reminder can be sent to it and the dashboard can mark who joined by
invitation id rather than by guessing from an address (invite-by-email.instructions.md).
Only a digest of the code is stored.

An account whose verified address is the invited one is invited into the course without the
link (``student_invitation_match``). Each invitation matches one account once, recorded in a
table of its own.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Optional
from uuid import uuid4

from synapse.util.threepids import canonicalise_email

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS pangea_student_invitation (
        invitation_id TEXT PRIMARY KEY, room_id TEXT NOT NULL, email TEXT NOT NULL,
        code_digest TEXT NOT NULL UNIQUE, created_by TEXT NOT NULL,
        created_at_ms BIGINT NOT NULL, first_used_by TEXT, first_used_at_ms BIGINT,
        uses INTEGER NOT NULL DEFAULT 0)""",
    """CREATE INDEX IF NOT EXISTS pangea_student_invitation_room
        ON pangea_student_invitation (room_id)""",
    """CREATE TABLE IF NOT EXISTS pangea_student_invitation_match (
        invitation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        matched_at_ms BIGINT NOT NULL)""",
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

    async def unmatched_for_emails(
        self, emails: Iterable[str], since_ms: int
    ) -> List[Dict[str, Any]]:
        """Invitations sent to any of ``emails`` since ``since_ms`` that no account has matched.

        Addresses are compared as Synapse stores verified ones (``canonicalise_email``), as
        ``CourseInvitationStore.prepared_for_emails`` does.
        """
        await self.ensure()
        wanted = {canonicalise_email(email) for email in emails}
        if not wanted:
            return []

        def select(txn):
            txn.execute(
                SELECT
                + """WHERE created_at_ms >= ? AND invitation_id NOT IN
                (SELECT invitation_id FROM pangea_student_invitation_match)
                ORDER BY created_at_ms, invitation_id""",
                (since_ms,),
            )
            return [self.row(r) for r in txn.fetchall()]

        rows = await self.db.runInteraction(
            "pangea_student_invitation_unmatched", select
        )
        return [r for r in rows if _canonical(r["email"]) in wanted]

    async def match(self, invitation_id: str, user_id: str, now: int) -> bool:
        """Takes the invitation's one match for ``user_id``; False when it was already taken."""
        await self.ensure()

        def insert(txn):
            txn.execute(
                """INSERT INTO pangea_student_invitation_match
                (invitation_id, user_id, matched_at_ms) VALUES (?, ?, ?)
                ON CONFLICT (invitation_id) DO NOTHING""",
                (invitation_id, user_id, now),
            )
            return txn.rowcount == 1

        return await self.db.runInteraction("pangea_student_invitation_match", insert)

    async def unmatch(self, invitation_id: str, user_id: str) -> None:
        """Releases a match ``user_id`` took, so a later sign-in tries again."""
        await self.ensure()

        def delete(txn):
            txn.execute(
                """DELETE FROM pangea_student_invitation_match
                WHERE invitation_id = ? AND user_id = ?""",
                (invitation_id, user_id),
            )

        await self.db.runInteraction("pangea_student_invitation_unmatch", delete)


def _canonical(email: str) -> Optional[str]:
    try:
        return canonicalise_email(email)
    # silent-ok: invite_by_email stores what the teacher typed; an address Synapse cannot parse is no account's verified address
    except ValueError:
        return None
