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
    # The caller-rendered share kit sent at claim, with the class link as a slot.
    """CREATE TABLE IF NOT EXISTS pangea_course_invitation_claimed_email (
        invitation_id TEXT PRIMARY KEY, subject TEXT NOT NULL, html TEXT NOT NULL,
        text TEXT NOT NULL, receiving_reason TEXT NOT NULL,
        stored_at_ms BIGINT NOT NULL)""",
    # Each course plan replacement, for status reads (create-course-space,
    # "Replacing the course plan").
    """CREATE TABLE IF NOT EXISTS pangea_course_invitation_replacement (
        invitation_id TEXT NOT NULL, replaced_at_ms BIGINT NOT NULL,
        previous_course_plan_id TEXT, course_plan_id TEXT NOT NULL,
        operator_id TEXT NOT NULL)""",
)
REPLACED_FIELDS = ("course_plan_id", "title", "description")
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


def invitation_kind(invitation) -> str:
    """``instructor`` for an additional-instructor invitation into an existing
    course, else ``course`` (the first claim, which creates the room)."""
    spec = invitation.get("specification") or {}
    return "instructor" if spec.get("kind") == "instructor" else "course"


def replacement_refusal(invitation) -> SynapseError | None:
    """Why this invitation's course plan cannot be replaced, or None. A
    completed course invitation is replaced in its room, not here."""
    if invitation["status"] == "revoked":
        reason = "A revoked invitation cannot be replaced"
    elif invitation_kind(invitation) == "instructor":
        reason = (
            "An additional-instructor invitation has no course plan of its own; "
            "change its course by room"
        )
    elif invitation["status"] == "provisioning":
        # Provisioning checks the room against the preparation it started from.
        reason = "A claim is creating this course; send the replacement again once it completes"
    elif invitation["status"] != "prepared":
        reason = "Only a prepared invitation's preparation can be replaced"
    else:
        return None
    return SynapseError(409, reason, additional_fields={"status": invitation["status"]})


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

    async def prepare(self, operator, key, spec, email, code, now, room_id=None):
        """Insert a prepared invitation, or find the one this operator's key
        already names. With ``room_id`` the invitation targets that existing
        course (an instructor invitation) and is identified by room and
        address; without it, by the course specification and address."""
        await self.ensure()
        invitation_id = str(uuid4())
        identity = (
            {"room_id": room_id, "email": email}
            if room_id
            else {"spec": spec, "email": email}
        )
        canonical = json.dumps(identity, sort_keys=True)
        digest = hashlib.sha256(canonical.encode()).hexdigest()

        def insert(txn):
            txn.execute(
                """INSERT INTO pangea_course_invitation
                (invitation_id, operator_id, request_key, input_digest,
                 specification, requested_email, status, room_id, created_at_ms)
                VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                ON CONFLICT (operator_id, request_key) DO NOTHING""",
                (
                    invitation_id,
                    operator,
                    key,
                    digest,
                    json.dumps(spec),
                    email,
                    room_id,
                    now,
                ),
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

    async def complete_instructor(self, invitation_id, user, room_id, now):
        """Completion of an additional-instructor invitation: the grant is
        recorded and the requesting address cleared. No claim row is written
        and nothing owes a share kit — the instructor claim transfers no
        ownership (knock-with-code.instructions.md, "Codes, share kit and
        existing courses")."""

        def update(txn):
            txn.execute(
                """UPDATE pangea_course_invitation
                SET status = 'completed', completed_at_ms = ?, requested_email = NULL
                WHERE invitation_id = ? AND claimant = ? AND room_id = ? AND status = 'provisioning'""",
                (now, invitation_id, user, room_id),
            )
            if txn.rowcount == 1:
                return
            txn.execute(
                "SELECT status FROM pangea_course_invitation WHERE invitation_id = ? AND claimant = ?",
                (invitation_id, user),
            )
            row = txn.fetchone()
            if not row or row[0] != "completed":
                raise recovery_required()

        await self.db.runInteraction("pangea_invitation_complete_instructor", update)

    async def list_instructor(self, status):
        """Instructor invitations in ``status``, newest first, as ``status()``
        reports them. Never the address."""
        await self.ensure()

        def select(txn):
            txn.execute(
                SELECT + "WHERE status = ? ORDER BY created_at_ms DESC, invitation_id",
                (status,),
            )
            return [self.row(r) for r in txn.fetchall()]

        rows = await self.db.runInteraction("pangea_invitation_list", select)
        return [
            await self.status(row["invitation_id"])
            for row in rows
            if invitation_kind(row) == "instructor"
        ]

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

    async def store_claimed_email(self, invitation_id, email, now):
        """Keep the caller-rendered email to send when this invitation is claimed (the latest
        one the caller sent replaces any earlier)."""
        await self.ensure()
        await self.db.simple_upsert(
            "pangea_course_invitation_claimed_email",
            keyvalues={"invitation_id": invitation_id},
            values={
                **{
                    k: email[k] for k in ("subject", "html", "text", "receiving_reason")
                },
                "stored_at_ms": now,
            },
            desc="pangea_store_claimed_email",
        )

    async def claimed_email_for_room(self, room_id):
        """The stored claimed email of the invitation whose course is this room, or None."""
        await self.ensure()

        def read(txn):
            txn.execute(
                """SELECT c.subject, c.html, c.text, c.receiving_reason
                FROM pangea_course_invitation_claimed_email c
                JOIN pangea_course_invitation i ON i.invitation_id = c.invitation_id
                WHERE i.room_id = ?""",
                (room_id,),
            )
            row = txn.fetchone()
            return (
                dict(zip(("subject", "html", "text", "receiving_reason"), row))
                if row
                else None
            )

        return await self.db.runInteraction("pangea_claimed_email_for_room", read)

    async def replace_preparation(
        self, invitation_id, expected, replacement, operator, now, dry_run=False
    ):
        """Replace a prepared invitation's quest, title and description, so the
        claim provisions from them. Checked and written in one transaction: it
        lands before a claim reserves the invitation, or it is refused.
        ``replacement`` holds the new ``REPLACED_FIELDS``."""
        await self.ensure()

        def update(txn):
            txn.execute(
                SELECT.replace("SELECT ", "SELECT specification AS raw, ", 1)
                + "WHERE invitation_id = ?",
                (invitation_id,),
            )
            found = txn.fetchone()
            if found is None:
                raise SynapseError(404, "Invitation not found")
            raw, row = found[0], self.row(found[1:])
            refusal = replacement_refusal(row)
            if refusal:
                raise refusal
            spec = row["specification"]
            current = spec.get("course_plan_id")
            if current != expected:
                raise SynapseError(
                    409,
                    "Invitation holds a different quest than expected",
                    additional_fields={"current_course_plan_id": current},
                )
            changes = [f for f in REPLACED_FIELDS if spec.get(f) != replacement[f]]
            if changes and not dry_run:
                # Guarded by status and by the stored text, so a claim or another
                # replacement that committed since the read is never overwritten:
                # the update finds no row, or fails to serialize and is retried.
                txn.execute(
                    """UPDATE pangea_course_invitation SET specification = ?
                    WHERE invitation_id = ? AND status = 'prepared' AND specification = ?""",
                    (json.dumps({**spec, **replacement}), invitation_id, raw),
                )
                if txn.rowcount != 1:
                    raise SynapseError(
                        409, "Invitation changed while it was replaced; read it again"
                    )
                if current != replacement["course_plan_id"]:
                    self._record(
                        txn, invitation_id, current, replacement, operator, now
                    )
            return {
                "previous_course_plan_id": current,
                "course_plan_id": replacement["course_plan_id"],
                "changes": changes,
            }

        return await self.db.runInteraction("pangea_invitation_replace", update)

    async def record_replacement(
        self, invitation_id, previous, replacement, operator, now
    ):
        """Record a replacement the claimed course's room accepted, and keep the
        preparation's quest in step with it. One already recorded is not recorded
        again, so two requests that both passed the room check make one record.
        True when this call recorded it."""
        await self.ensure()

        def update(txn):
            txn.execute(
                "SELECT specification FROM pangea_course_invitation WHERE invitation_id = ? AND status = 'completed'",
                (invitation_id,),
            )
            found = txn.fetchone()
            if found is None:
                raise SynapseError(
                    409, "Invitation changed while its course was replaced"
                )
            spec = json.loads(found[0])
            if spec.get("course_plan_id") == replacement["course_plan_id"]:
                return False
            spec.update(replacement)
            txn.execute(
                "UPDATE pangea_course_invitation SET specification = ? WHERE invitation_id = ?",
                (json.dumps(spec), invitation_id),
            )
            self._record(txn, invitation_id, previous, replacement, operator, now)
            return True

        return await self.db.runInteraction(
            "pangea_invitation_record_replacement", update
        )

    @staticmethod
    def _record(txn, invitation_id, previous, replacement, operator, now):
        txn.execute(
            """INSERT INTO pangea_course_invitation_replacement
            (invitation_id, replaced_at_ms, previous_course_plan_id, course_plan_id, operator_id)
            VALUES (?, ?, ?, ?, ?)""",
            (invitation_id, now, previous, replacement["course_plan_id"], operator),
        )

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

        def replacements(txn):
            txn.execute(
                """SELECT replaced_at_ms, previous_course_plan_id, course_plan_id
                FROM pangea_course_invitation_replacement WHERE invitation_id = ?
                ORDER BY replaced_at_ms""",
                (invitation_id,),
            )
            return [
                dict(
                    zip(
                        ("replaced_at_ms", "previous_course_plan_id", "course_plan_id"),
                        r,
                    )
                )
                for r in txn.fetchall()
            ]

        result["kind"] = invitation_kind(row)
        # An instructor invitation joins an existing course; its quest is the room's.
        result["course_plan_id"] = (
            row["specification"].get("course_plan_id")
            if result["kind"] == "course"
            else None
        )
        result["replacements"] = await self.db.runInteraction(
            "pangea_invitation_replacements", replacements
        )
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
