"""A durable transport claim for a caller-owned record.

When the caller owns the Notification_Log row, this module writes nothing to the ledger, but
one delivery call per decision still has to mean one transport. The claim is the module's own
small table, keyed by the caller's row: the first request claims it before any transport, a
repeat of the same request after a definite result answers with that result and no second
transport, a repeat while the result is uncertain is refused until reconciled, and a different
payload under the same row is a conflict. The phase and the notice identity are persisted before
transport so an interrupted delivery leaves evidence, not a guess.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from synapse.storage.engines import PostgresEngine

from synapse_pangea_chat.notice_delivery.delivery_log import DeliveryConflict

TABLE = "pangea_notice_transport_claim"

PHASE_CLAIMED = "claimed"
PHASE_COMPLETE = "complete"
PHASE_PENDING = "pending_reconciliation"


class TransportClaims:
    def __init__(self, api: Any):
        self._db = api._hs.get_datastores().main.db_pool
        self._clock = api._hs.get_clock()
        self._ready = False

    async def _ensure_table(self) -> None:
        if self._ready:
            return

        def create(txn):
            if isinstance(self._db.engine, PostgresEngine):
                txn.execute("SELECT pg_advisory_xact_lock(741882937)")
            txn.execute(
                f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                record_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                phase TEXT NOT NULL,
                notice_event_id TEXT,
                claimed_at_ms BIGINT NOT NULL,
                result TEXT
            )"""
            )

        await self._db.runInteraction("notice_transport_claim_schema", create)
        self._ready = True

    @staticmethod
    def fingerprint(body: Dict[str, Any]) -> str:
        import hashlib

        payload = json.dumps(
            {k: v for k, v in body.items() if not k.startswith("_")},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    async def claim(
        self, record_id: str, user_id: str, body: Dict[str, Any]
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Claim the row for this request.

        Returns ``(claimed, previous)``: claimed with no previous result on the first request;
        not claimed with the stored result when the same request already completed. Raises
        :class:`DeliveryConflict` for a different payload under the same row, or a repeat
        while the earlier attempt is still uncertain.
        """
        await self._ensure_table()
        fingerprint = self.fingerprint(body)
        now = self._clock.time_msec()

        def insert(txn):
            txn.execute(
                f"SELECT fingerprint, phase, result FROM {TABLE} WHERE record_id = ?",
                (record_id,),
            )
            row = txn.fetchone()
            if row is None:
                txn.execute(
                    f"INSERT INTO {TABLE} (record_id, user_id, fingerprint, phase, notice_event_id, claimed_at_ms, result) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    (
                        record_id,
                        user_id,
                        fingerprint,
                        PHASE_CLAIMED,
                        body.get("notice_event_id"),
                        now,
                    ),
                )
                return True, None
            if row[0] != fingerprint:
                raise DeliveryConflict(
                    "This Notification_Log row already carries a different notice"
                )
            if row[1] == PHASE_COMPLETE and row[2]:
                return False, json.loads(row[2])
            raise DeliveryConflict(
                "Delivery for this row is pending or uncertain; reconcile before any resend"
            )

        return await self._db.runInteraction("notice_transport_claim", insert)

    async def record_event(
        self, record_id: str, notice_event_id: Optional[str]
    ) -> None:
        if not notice_event_id:
            return

        def update(txn):
            txn.execute(
                f"UPDATE {TABLE} SET notice_event_id = ? WHERE record_id = ?",
                (notice_event_id, record_id),
            )

        await self._db.runInteraction("notice_transport_claim_event", update)

    async def finish(self, record_id: str, phase: str, result: Dict[str, Any]) -> None:
        def update(txn):
            txn.execute(
                f"UPDATE {TABLE} SET phase = ?, result = ? WHERE record_id = ?",
                (phase, json.dumps(result), record_id),
            )

        await self._db.runInteraction("notice_transport_claim_finish", update)

    async def mark_uncertain(self, record_id: str, detail: str) -> None:
        """An exception after the claim: keep the row pending with a sanitized reason."""

        def update(txn):
            txn.execute(
                f"UPDATE {TABLE} SET phase = ?, result = ? WHERE record_id = ? AND phase = ?",
                (
                    PHASE_PENDING,
                    json.dumps({"error": detail}),
                    record_id,
                    PHASE_CLAIMED,
                ),
            )

        await self._db.runInteraction("notice_transport_claim_uncertain", update)
