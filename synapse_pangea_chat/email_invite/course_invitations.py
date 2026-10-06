"""Durable, roomless course preparations and provisioning reservations.

A permanent creation reservation is intentional: lease expiry is not evidence
that Synapse did not create a room. Recovery locates the immutable create-event
marker, and never starts a second creation on an uncertain operation.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from synapse.api.errors import SynapseError
from synapse.util.threepids import canonicalise_email

from synapse_pangea_chat.email_invite.course_claims import admin_code_digest

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS pangea_course_invitation (
        invitation_id TEXT PRIMARY KEY, operator_id TEXT NOT NULL,
        request_key TEXT NOT NULL, input_digest TEXT NOT NULL,
        specification TEXT NOT NULL, requested_email TEXT,
        status TEXT NOT NULL, claimant TEXT, room_id TEXT,
        created_at_ms BIGINT NOT NULL, completed_at_ms BIGINT,
        UNIQUE(operator_id, request_key))""",
    """CREATE TABLE IF NOT EXISTS pangea_course_invitation_code (
        digest TEXT PRIMARY KEY, invitation_id TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS pangea_course_invitation_delivery (
        attempt_id TEXT PRIMARY KEY, invitation_id TEXT NOT NULL,
        created_at_ms BIGINT NOT NULL, finished_at_ms BIGINT,
        outcome TEXT NOT NULL)""",
)
COLUMNS = (
    "invitation_id",
    "status",
    "claimant",
    "room_id",
    "specification",
    "requested_email",
    "created_at_ms",
    "completed_at_ms",
)
SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM pangea_course_invitation "


def unavailable() -> SynapseError:
    return SynapseError(
        404, "No rooms found with the access code", "ORG.PANGEA.CODE_NOT_FOUND"
    )


def recovery_required() -> SynapseError:
    return SynapseError(
        503,
        "Course provisioning requires recovery; retry this invitation",
        "ORG.PANGEA.CLAIM_RECOVERY_REQUIRED",
    )


class CourseInvitationStore:
    def __init__(self, homeserver: Any):
        self.db = homeserver.get_datastores().main.db_pool
        self.ready = False

    async def ensure(self):
        if self.ready:
            return

        def create(txn):
            for sql in SCHEMA:
                txn.execute(sql)

        await self.db.runInteraction("pangea_invitation_schema", create)
        self.ready = True

    @staticmethod
    def row(row):
        if row is None:
            return None
        result = dict(zip(COLUMNS, row))
        result["specification"] = json.loads(result["specification"])
        return result

    async def prepare(self, operator, key, spec, email, code, now):
        await self.ensure()
        invitation_id = str(uuid4())
        canonical = json.dumps({"spec": spec, "email": email}, sort_keys=True)
        digest = hashlib.sha256(canonical.encode()).hexdigest()

        def insert(txn):
            txn.execute(
                """INSERT INTO pangea_course_invitation
                (invitation_id, operator_id, request_key, input_digest,
                 specification, requested_email, status, created_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?)
                ON CONFLICT (operator_id, request_key) DO NOTHING""",
                (invitation_id, operator, key, digest, json.dumps(spec), email, now),
            )
            created = txn.rowcount == 1
            txn.execute(
                "SELECT invitation_id, input_digest FROM pangea_course_invitation WHERE operator_id = ? AND request_key = ?",
                (operator, key),
            )
            found, old_digest = txn.fetchone()
            if old_digest != digest:
                raise SynapseError(409, "Request key already used with different input")
            if created:
                txn.execute(
                    "INSERT INTO pangea_course_invitation_code VALUES (?, ?)",
                    (admin_code_digest(code), found),
                )
            return found, created

        return await self.db.runInteraction("pangea_invitation_prepare", insert)

    async def get(self, invitation_id):
        await self.ensure()

        def select(txn):
            txn.execute(SELECT + "WHERE invitation_id = ?", (invitation_id,))
            return self.row(txn.fetchone())

        return await self.db.runInteraction("pangea_invitation_get", select)

    async def for_code(self, code):
        await self.ensure()

        def select(txn):
            txn.execute(
                "SELECT invitation_id FROM pangea_course_invitation_code WHERE digest = ?",
                (admin_code_digest(code),),
            )
            row = txn.fetchone()
            return row[0] if row else None

        ident = await self.db.runInteraction("pangea_invitation_code", select)
        return await self.get(ident) if ident else None

    async def code_in_use(self, code):
        return await self.for_code(code) is not None

    async def prepared_for_emails(self, emails):
        """Prepared invitations requested from any of ``emails``.

        Addresses are compared as Synapse stores verified ones
        (``canonicalise_email``): SQL ``LOWER`` folds only ASCII, so a
        requested ``JÖRG@schule.de`` would miss the stored ``jörg@schule.de``.
        Completed invitations have no address left to match, and revoked ones
        are excluded by status.
        """
        await self.ensure()
        wanted = {canonicalise_email(email) for email in emails}
        if not wanted:
            return []

        def select(txn):
            txn.execute(
                SELECT
                + "WHERE status = 'prepared' AND requested_email IS NOT NULL"
                + " ORDER BY created_at_ms, invitation_id"
            )
            return [self.row(r) for r in txn.fetchall()]

        rows = await self.db.runInteraction("pangea_invitation_for_emails", select)
        return [r for r in rows if canonicalise_email(r["requested_email"]) in wanted]

    async def reserve_creation(self, invitation_id, user):
        await self.ensure()

        def reserve(txn):
            txn.execute(
                """UPDATE pangea_course_invitation SET status = 'provisioning', claimant = ?
                WHERE invitation_id = ? AND status = 'prepared' AND claimant IS NULL""",
                (user, invitation_id),
            )
            created = txn.rowcount == 1
            txn.execute(SELECT + "WHERE invitation_id = ?", (invitation_id,))
            row = self.row(txn.fetchone())
            if not row or row["status"] == "revoked" or row["claimant"] != user:
                raise unavailable()
            return row, created

        return await self.db.runInteraction("pangea_invitation_reserve", reserve)

    async def associate(self, invitation_id, user, room_id):
        def update(txn):
            txn.execute(
                """UPDATE pangea_course_invitation SET room_id = ?
                WHERE invitation_id = ? AND claimant = ? AND status = 'provisioning'
                AND (room_id IS NULL OR room_id = ?)""",
                (room_id, invitation_id, user, room_id),
            )
            if txn.rowcount != 1:
                raise recovery_required()

        await self.db.runInteraction("pangea_invitation_associate", update)

    async def complete(self, invitation_id, user, room_id, now):
        # Share-kit debt and completion commit together. The legacy notifier
        # owns delivery/retries and clearing the requesting address thereafter.
        def update(txn):
            txn.execute(
                """UPDATE pangea_course_invitation SET status = 'completed', completed_at_ms = ?
                WHERE invitation_id = ? AND claimant = ? AND room_id = ? AND status = 'provisioning'""",
                (now, invitation_id, user, room_id),
            )
            if txn.rowcount != 1:
                txn.execute(
                    "SELECT status FROM pangea_course_invitation WHERE invitation_id = ? AND claimant = ?",
                    (invitation_id, user),
                )
                row = txn.fetchone()
                if not row or row[0] != "completed":
                    raise recovery_required()
                return
            txn.execute(
                "SELECT requested_email FROM pangea_course_invitation WHERE invitation_id = ?",
                (invitation_id,),
            )
            email = txn.fetchone()[0]
            # An unguessable non-code fingerprint prevents the legacy backfill
            # from making a second usable grant for this completed invitation.
            txn.execute(
                """INSERT INTO pangea_course_claim
                (room_id, requested_email, admin_code_sha256, created_at_ms, claimed_by, claimed_at_ms, promoted_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (room_id, email, "invitation:" + invitation_id, now, user, now, now),
            )
            txn.execute(
                "UPDATE pangea_course_invitation SET requested_email = NULL WHERE invitation_id = ?",
                (invitation_id,),
            )

        await self.db.runInteraction("pangea_invitation_complete", update)

    async def begin_delivery(self, invitation_id, code, now):
        attempt = str(uuid4())

        def reserve(txn):
            # Serialize against claimant reservation and revocation.
            txn.execute(
                "UPDATE pangea_course_invitation SET status = status WHERE invitation_id = ? AND status = 'prepared'",
                (invitation_id,),
            )
            if txn.rowcount != 1:
                raise SynapseError(409, "Invitation is not available for reminders")
            txn.execute(
                "INSERT INTO pangea_course_invitation_code VALUES (?, ?) ON CONFLICT (digest) DO NOTHING",
                (admin_code_digest(code), invitation_id),
            )
            txn.execute(
                "SELECT invitation_id FROM pangea_course_invitation_code WHERE digest = ?",
                (admin_code_digest(code),),
            )
            if txn.fetchone()[0] != invitation_id:
                raise SynapseError(503, "Claim code collision; retry delivery")
            txn.execute(
                "INSERT INTO pangea_course_invitation_delivery VALUES (?, ?, ?, NULL, 'uncertain')",
                (attempt, invitation_id, now),
            )

        await self.db.runInteraction("pangea_invitation_delivery_begin", reserve)
        return attempt

    async def finish_delivery(self, attempt, outcome, now):
        def update(txn):
            txn.execute(
                "UPDATE pangea_course_invitation_delivery SET outcome = ?, finished_at_ms = ? WHERE attempt_id = ?",
                (outcome, now, attempt),
            )

        await self.db.runInteraction("pangea_invitation_delivery_finish", update)

    async def status(self, invitation_id):
        row = await self.get(invitation_id)
        if row is None:
            raise SynapseError(404, "Invitation not found")

        def deliveries(txn):
            txn.execute(
                """SELECT attempt_id, created_at_ms, finished_at_ms, outcome
                FROM pangea_course_invitation_delivery WHERE invitation_id = ? ORDER BY created_at_ms, attempt_id""",
                (invitation_id,),
            )
            return [
                dict(
                    zip(("attempt_id", "created_at_ms", "finished_at_ms", "outcome"), r)
                )
                for r in txn.fetchall()
            ]

        result = {
            k: row[k]
            for k in (
                "invitation_id",
                "status",
                "room_id",
                "claimant",
                "created_at_ms",
                "completed_at_ms",
            )
        }
        result["deliveries"] = await self.db.runInteraction(
            "pangea_invitation_deliveries", deliveries
        )
        result["delivery_outcome"] = (
            result["deliveries"][-1]["outcome"] if result["deliveries"] else "unsent"
        )
        return result

    async def revoke(self, invitation_id):
        await self.ensure()

        def update(txn):
            txn.execute(
                "UPDATE pangea_course_invitation SET status = 'revoked' WHERE invitation_id = ?",
                (invitation_id,),
            )
            if txn.rowcount != 1:
                raise SynapseError(404, "Invitation not found")

        await self.db.runInteraction("pangea_invitation_revoke", update)
