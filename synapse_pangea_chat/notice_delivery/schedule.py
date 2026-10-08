"""Durable whole-notice scheduling. Claim before any external side effect."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, cast

from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.storage.engines import PostgresEngine

from synapse_pangea_chat.moderation.compat import (
    background_process_args,
    looping_call_interval,
)
from synapse_pangea_chat.notice_delivery.delivery_log import DeliveryConflict
from synapse_pangea_chat.notice_delivery.request import NoticeRequest

logger = logging.getLogger(__name__)
TABLE = "pangea_notice_schedule"


class NoticeSchedule:
    def __init__(
        self, api: Any, execute: Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]
    ):
        self._api = api
        self._db = api._hs.get_datastores().main.db_pool
        self._clock = api._hs.get_clock()
        self._execute = execute
        self._ready = False
        self._running = False

    def start(self) -> None:
        self._clock.looping_call(
            cast(Any, run_as_background_process),
            cast(Any, looping_call_interval(5)),
            *background_process_args(
                self._api._hs, "pangea_notice_schedule", self.run_due
            ),
        )

    async def _ensure_table(self) -> None:
        if self._ready:
            return

        def create(txn):
            if isinstance(self._db.engine, PostgresEngine):
                # IF NOT EXISTS alone can race PostgreSQL's catalog creation
                # when several processes initialize a newly deployed module.
                txn.execute("SELECT pg_advisory_xact_lock(741882936)")
            txn.execute(
                f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                schedule_id TEXT PRIMARY KEY,
                decision_key TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                scheduled_at_ms BIGINT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT,
                result TEXT
            )"""
            )
            txn.execute(
                f"CREATE INDEX IF NOT EXISTS {TABLE}_due ON {TABLE}(status, scheduled_at_ms)"
            )

        await self._db.runInteraction("notice_schedule_schema", create)
        self._ready = True

    async def enqueue(self, body: Dict[str, Any]) -> Dict[str, Any]:
        req = NoticeRequest.parse(body)
        timestamp = datetime.fromisoformat(body["scheduled_at"].replace("Z", "+00:00"))
        due = int(timestamp.timestamp() * 1000)
        payload = json.dumps(
            body, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if len(payload.encode()) > 768_000:
            raise ValueError("Scheduled request is too large")
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        key = json.dumps([req.log.run["run_id"], req.user_id], separators=(",", ":"))
        await self._ensure_table()

        def insert(txn):
            # ON CONFLICT plus the unique key serializes concurrent submissions.
            # Keep the fingerprint after deleting content so completed retries can
            # still distinguish the same decision from changed content.
            schedule_id = str(uuid.uuid4())
            txn.execute(
                f"INSERT INTO {TABLE} VALUES (?, ?, ?, ?, 'queued', ?, NULL) "
                f"ON CONFLICT (decision_key) DO UPDATE SET decision_key = excluded.decision_key "
                "RETURNING schedule_id, fingerprint, scheduled_at_ms, status, result",
                (schedule_id, key, fingerprint, due, payload),
            )
            row = txn.fetchone()
            if row[1] != fingerprint:
                raise DeliveryConflict(
                    "Run/person already has a different scheduled notice"
                )
            return self._response(row), row[0] != schedule_id

        result, duplicate = await self._db.runInteraction(
            "notice_schedule_enqueue", insert
        )
        return {**result, "duplicate": duplicate}

    @staticmethod
    def _response(row) -> Dict[str, Any]:
        return {
            "schedule_id": row[0],
            "scheduled_at_ms": row[2],
            "status": row[3],
            "result": json.loads(row[4]) if row[4] else None,
        }

    async def get(self, schedule_id: str) -> Dict[str, Any]:
        await self._ensure_table()

        def read(txn):
            txn.execute(
                f"SELECT schedule_id, fingerprint, scheduled_at_ms, status, result FROM {TABLE} WHERE schedule_id = ?",
                (schedule_id,),
            )
            row = txn.fetchone()
            if row is None:
                raise ValueError("Unknown schedule_id")
            return self._response(row)

        return await self._db.runInteraction("notice_schedule_read", read)

    async def run_due(self) -> None:
        # The timer starts background processes; it does not await the previous
        # tick. Slow CMS/SMTP responses must not grow concurrent drain loops.
        if self._running:
            return
        self._running = True
        try:
            await self._ensure_table()
            # Claim one at a time: a crash does not strand an unstarted batch.
            for _ in range(100):
                job = await self._db.runInteraction(
                    "notice_schedule_claim", self._claim
                )
                if job is None:
                    return
                schedule_id, payload = job
                try:
                    body = json.loads(payload)
                    body["_schedule_id"] = schedule_id
                    result = await self._execute(body)
                    status = "complete"
                    if result.get("log_status") == "pending_reconciliation" or (
                        result.get("reason") or ""
                    ).endswith("send_failed"):
                        status = "pending_reconciliation"
                    # Don't retain device tokens from the push transport.
                    push = result.get("push")
                    if push:
                        result["push"] = {
                            k: push[k] for k in ("attempted", "sent", "failed")
                        }
                    await self._finish(schedule_id, status, result)
                except Exception:
                    # The durable claim is already pending_reconciliation. Never
                    # requeue: event creation or transport may have succeeded.
                    logger.exception(
                        "Scheduled notice requires reconciliation: %s", schedule_id
                    )
        except Exception:
            logger.exception("Notice schedule polling failed")
        finally:
            self._running = False

    async def cancel(self, schedule_id: str) -> Dict[str, Any]:
        """Compete with _claim in SQL; never promise to retract a claimed send."""
        await self._ensure_table()

        def cancel(txn):
            txn.execute(
                f"UPDATE {TABLE} SET status = 'cancelled', payload = NULL "
                "WHERE schedule_id = ? AND status = 'queued'",
                (schedule_id,),
            )
            txn.execute(
                f"SELECT schedule_id, fingerprint, scheduled_at_ms, status, result FROM {TABLE} WHERE schedule_id = ?",
                (schedule_id,),
            )
            row = txn.fetchone()
            if row is None:
                raise ValueError("Unknown schedule_id")
            if row[3] != "cancelled":
                raise DeliveryConflict("Notice already claimed; cannot cancel delivery")
            return self._response(row)

        return await self._db.runInteraction("notice_schedule_cancel", cancel)

    def _claim(self, txn):
        txn.execute(
            f"SELECT schedule_id, payload FROM {TABLE} WHERE status = 'queued' AND scheduled_at_ms <= ? ORDER BY scheduled_at_ms LIMIT 1",
            (self._clock.time_msec(),),
        )
        row = txn.fetchone()
        if row is None:
            return None
        txn.execute(
            f"UPDATE {TABLE} SET status = 'pending_reconciliation' WHERE schedule_id = ? AND status = 'queued'",
            (row[0],),
        )
        return row if txn.rowcount == 1 else None

    async def _finish(
        self, schedule_id: str, status: str, result: Dict[str, Any]
    ) -> None:
        def finish(txn):
            txn.execute(
                f"UPDATE {TABLE} SET status = ?, result = ?, payload = NULL WHERE schedule_id = ?",
                (status, json.dumps(result), schedule_id),
            )

        await self._db.runInteraction("notice_schedule_finish", finish)
