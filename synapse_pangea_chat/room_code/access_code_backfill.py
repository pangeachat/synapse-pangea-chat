"""One-time population of the room access-code index.

The index in ``access_code_index`` is maintained forward in time by the
``on_new_event`` hook. Rooms whose join rules were written before the module
shipped have no such event to react to, so exactly one pass over
``current_state_events`` seeds them, and a marker row records that the pass
finished. Until that marker exists, every lookup keeps scanning — see the
readiness gate in ``access_code_index.index_is_ready``.

The scan runs once per homeserver, not once per worker: the module is
instantiated in every worker, so a leased row decides which one does the work.
The lease pattern is the one ``public_courses/backfill_l2.py`` established.

This pass is read-mostly — it reads join-rules state and writes at most one
small row per room. It sends no events and wakes no ``/sync``, so it needs
nothing like the deliberate slow pacing the l2 backfill uses.

There is deliberately no "rebuild" switch. Rewriting a live index in place
would race the hook (scan reads the old code, hook writes the new one, scan
writes the old one back), and emptying it first would make codes stop
resolving for the duration. To repair a suspected-bad index, set
``room_code_index_enabled: false`` — every endpoint reverts to the scan it used
before this feature — then drop the two tables and turn it back on.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import TYPE_CHECKING, Any, List, NamedTuple, Optional, Tuple, cast

from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.module_api import ModuleApi

from synapse_pangea_chat.room_code.access_code_index import (
    LEASE_KEY,
    LEASE_TABLE,
    RoomCodes,
    ensure_schema,
    extract_codes,
    insert_missing_rows_txn,
    is_enabled,
)
from synapse_pangea_chat.room_code.constants import EVENT_TYPE_M_ROOM_JOIN_RULES

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.room_code.access_code_backfill"
)

# Rooms per batch. Larger than the l2 backfill's because a batch here is a
# state read plus a handful of tiny inserts, not a fan-out of state events.
BATCH_SIZE = 500

# Let the homeserver finish starting before adding scan load.
START_DELAY_SECONDS = 30.0

# A lease older than this belongs to a worker that died mid-run and may be
# taken over. Refreshed once per batch.
LEASE_STALE_AFTER_MS = 10 * 60 * 1000

# Re-check until the index is complete. Covers the two ways a single attempt
# ends without finishing: another worker holds the lease (this one has nothing
# to do but confirm the outcome), and a run that died partway (the lease has to
# go stale before anyone may take it over).
RETRY_DELAY_SECONDS = 120.0


_RUN_AS_BG_SUPPORTS_SERVER_NAME = (
    "server_name" in inspect.signature(run_as_background_process).parameters
)


def _background_process_args(homeserver: Any, desc: str, func: Any) -> Tuple[Any, ...]:
    # Synapse grew a server_name argument; support both signatures.
    if _RUN_AS_BG_SUPPORTS_SERVER_NAME:
        return (desc, homeserver.hostname, func)
    return (desc, func)


class BackfillSummary(NamedTuple):
    """What a run did. Logged at the end, and asserted on in tests."""

    complete: bool = False
    scanned: int = 0
    indexed: int = 0
    skipped_no_code: int = 0

    def plus(self, **deltas: int) -> "BackfillSummary":
        return self._replace(
            **{field: getattr(self, field) + delta for field, delta in deltas.items()}
        )


class JoinRulesRow(NamedTuple):
    room_id: str
    state_key: str
    codes: RoomCodes


class RoomAccessCodeBackfill:
    """The one-shot seeding pass, armed at module construction."""

    def __init__(self, api: ModuleApi, config: "PangeaChatConfig") -> None:
        self._api = api
        self._config = config
        self._hs = api._hs
        self._clock = self._hs.get_clock()
        self._store = self._hs.get_datastores().main

    # -- scheduling ---------------------------------------------------------

    def schedule(self) -> None:
        """Arrange for a run shortly after startup, retrying until complete."""
        if not is_enabled():
            return
        self._arm(START_DELAY_SECONDS)
        logger.info(
            "room code index backfill armed; starting in %.0fs",
            START_DELAY_SECONDS,
        )

    def _arm(self, delay_seconds: float) -> None:
        self._clock.call_later(
            delay_seconds,
            cast(Any, run_as_background_process),
            *_background_process_args(
                self._hs,
                "pangea_room_code_index_backfill",
                self._tick,
            ),
        )

    async def _tick(self) -> BackfillSummary:
        """One attempt; re-arms itself unless the index is now complete."""
        try:
            summary = await self.run()
        except Exception as e:
            logger.warning("room code index backfill: run failed: %s", e)
            summary = BackfillSummary()

        if not summary.complete:
            self._arm(RETRY_DELAY_SECONDS)
        return summary

    # -- lease --------------------------------------------------------------

    async def _already_complete(self) -> bool:
        rows = await self._store.db_pool.execute(
            "pangea_room_code_index_backfill_check_complete",
            f"SELECT completed_at_ms FROM {LEASE_TABLE} WHERE lease_key = ?",
            LEASE_KEY,
        )
        return any(row[0] is not None for row in rows or [])

    async def _claim_lease(self, claimed_by: str, now_ms: int) -> bool:
        """Claim the run, or report that another instance already holds it.

        A row rather than a session-level ``pg_advisory_lock``: Synapse hands
        out pooled connections per interaction, so a session lock would be
        taken on whichever connection served the claim and could not be
        reliably released by a later one.
        """
        stale_before_ms = now_ms - LEASE_STALE_AFTER_MS

        def _claim(txn: Any) -> bool:
            txn.execute(
                f"""
                INSERT INTO {LEASE_TABLE}
                    (lease_key, claimed_by, heartbeat_ms, completed_at_ms)
                VALUES (?, ?, ?, NULL)
                ON CONFLICT (lease_key) DO UPDATE
                    SET claimed_by = EXCLUDED.claimed_by,
                        heartbeat_ms = EXCLUDED.heartbeat_ms
                    WHERE {LEASE_TABLE}.completed_at_ms IS NULL
                      AND {LEASE_TABLE}.heartbeat_ms < ?
                """,
                (LEASE_KEY, claimed_by, now_ms, stale_before_ms),
            )
            return bool(txn.rowcount)

        return await self._store.db_pool.runInteraction(
            "pangea_room_code_index_backfill_claim_lease",
            _claim,
        )

    async def _heartbeat_lease(self, claimed_by: str, now_ms: int) -> None:
        def _beat(txn: Any) -> None:
            txn.execute(
                f"""
                UPDATE {LEASE_TABLE}
                SET heartbeat_ms = ?
                WHERE lease_key = ? AND claimed_by = ?
                """,
                (now_ms, LEASE_KEY, claimed_by),
            )

        await self._store.db_pool.runInteraction(
            "pangea_room_code_index_backfill_heartbeat_lease",
            _beat,
        )

    async def _mark_complete(self, claimed_by: str, now_ms: int) -> None:
        """Open the index to readers.

        Written only on a clean finish. A run that dies partway leaves the
        marker unset, so lookups keep scanning and a later attempt retries —
        the index is never half-trusted.
        """

        def _complete(txn: Any) -> None:
            txn.execute(
                f"""
                UPDATE {LEASE_TABLE}
                SET completed_at_ms = ?
                WHERE lease_key = ? AND claimed_by = ?
                """,
                (now_ms, LEASE_KEY, claimed_by),
            )

        await self._store.db_pool.runInteraction(
            "pangea_room_code_index_backfill_mark_complete",
            _complete,
        )

    # -- scan ---------------------------------------------------------------

    async def _fetch_batch(self, after_room_id: Optional[str]) -> List[JoinRulesRow]:
        """Up to ``BATCH_SIZE`` rooms' current join-rules state, by room id.

        The JSON is parsed in Python rather than in SQL on purpose: the same
        ``extract_codes`` the hook and the verification step use decides what
        counts as a code, so the three cannot drift apart.
        """
        room_predicate = "AND cse.room_id > ?" if after_room_id else ""
        sql = f"""
        SELECT cse.room_id, cse.state_key, ej.json
        FROM current_state_events cse
        INNER JOIN event_json ej ON ej.event_id = cse.event_id
        WHERE cse.type = ?
          {room_predicate}
        ORDER BY cse.room_id
        LIMIT ?
        """

        params: List[Any] = [EVENT_TYPE_M_ROOM_JOIN_RULES]
        if after_room_id:
            params.append(after_room_id)
        params.append(BATCH_SIZE)

        rows = await self._store.db_pool.execute(
            "pangea_room_code_index_backfill_scan",
            sql,
            *params,
        )

        batch: List[JoinRulesRow] = []
        for room_id, state_key, event_json in rows or []:
            try:
                event = (
                    json.loads(event_json)
                    if isinstance(event_json, str)
                    else event_json
                )
            except ValueError:
                logger.warning(
                    "room code index backfill: unparseable join rules for room %s",
                    room_id,
                )
                event = None
            content = event.get("content") if isinstance(event, dict) else None
            batch.append(
                JoinRulesRow(
                    room_id=room_id,
                    state_key=state_key or "",
                    codes=extract_codes(content),
                )
            )
        return batch

    async def _write_batch(self, batch: List[JoinRulesRow]) -> int:
        with_codes = [
            (row.room_id, row.state_key, row.codes)
            for row in batch
            if not row.codes.is_empty
        ]
        if not with_codes:
            return 0

        await self._store.db_pool.runInteraction(
            "pangea_room_code_index_backfill_write",
            insert_missing_rows_txn,
            with_codes,
        )
        return len(with_codes)

    # -- run ----------------------------------------------------------------

    async def run(self) -> BackfillSummary:
        if not is_enabled():
            return BackfillSummary(complete=True)

        await ensure_schema(self._store)

        if await self._already_complete():
            return BackfillSummary(complete=True)

        claimed_by = getattr(self._hs, "get_instance_name", lambda: "master")()
        if not await self._claim_lease(claimed_by, self._clock.time_msec()):
            logger.info(
                "room code index backfill: another instance holds the lease, "
                "not running here"
            )
            return BackfillSummary()

        logger.info("room code index backfill: starting (instance=%s)", claimed_by)

        summary = BackfillSummary()
        after_room_id: Optional[str] = None
        while True:
            batch = await self._fetch_batch(after_room_id)
            if not batch:
                break

            after_room_id = batch[-1].room_id
            indexed = await self._write_batch(batch)
            summary = summary.plus(
                scanned=len(batch),
                indexed=indexed,
                skipped_no_code=len(batch) - indexed,
            )

            await self._heartbeat_lease(claimed_by, self._clock.time_msec())

            if len(batch) < BATCH_SIZE:
                break

        await self._mark_complete(claimed_by, self._clock.time_msec())
        logger.info(
            "room code index backfill: finished scanned=%d indexed=%d "
            "skipped_no_code=%d; index is now serving lookups",
            summary.scanned,
            summary.indexed,
            summary.skipped_no_code,
        )
        return summary._replace(complete=True)
