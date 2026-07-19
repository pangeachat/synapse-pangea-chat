"""One-time repair of existing ``pangea.course_plan`` state events.

Two repairs, both on the room's *current* course-plan state event:

* **Add ``l2``** — resolved once from the CMS by plan id. A room with no ``l2``
  is excluded from every language-filtered browse, so most of the existing
  catalog is invisible to a client that filters.
* **Normalise the plan id key to ``uuid``** — spaces created by
  ``create_course_space`` write ``course_plan_id``. Readers accept both; the
  stock should not stay split.

Operationally this is a switch, not a feature: ``public_courses_backfill_l2``
is off by default, the operator turns it on, deploys, watches the summary log,
and turns it off again. Nothing about the read path changes either way.

Design notes worth keeping:

* Eligibility is *not* restated here. Whether a room is a course, and what its
  plan id is, comes from ``extract_plan_id`` — the same rule the catalog query
  applies. A second, drifting copy of that rule is what this whole change set
  exists to eliminate.
* Rooms are scanned regardless of whether they are published. Restricting the
  scan to public rooms would skip exactly the rooms that carry the
  ``course_plan_id`` spelling, since ``create_course_space`` makes private
  spaces — and a room published later would then need a second backfill.
* A room whose plan id the CMS does not resolve is skipped and logged. There
  is no default language and no empty ``l2``: a wrong language is worse than a
  missing one, because a missing one merely hides the course from a filtered
  browse while a wrong one files it under a language nobody is learning it in.
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional, Tuple, cast

from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.module_api import ModuleApi

from synapse_pangea_chat.public_courses.course_plan_l2_lookup import (
    CoursePlanLookupError,
    fetch_plan_languages,
)
from synapse_pangea_chat.public_courses.get_public_courses import (
    DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE,
    PLAN_ID_CONTENT_KEYS,
    extract_l2,
    extract_plan_id,
)
from synapse_pangea_chat.public_courses.select_state_sender import select_state_sender

if TYPE_CHECKING:
    from synapse_pangea_chat.config import PangeaChatConfig

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.public_courses.backfill_l2"
)

LEASE_TABLE = "pangea_public_courses_backfill_l2_lease"
LEASE_KEY = "l2_backfill"

# The canonical plan id key. Everything else in PLAN_ID_CONTENT_KEYS is a
# spelling the backfill collapses into it.
CANONICAL_PLAN_ID_KEY = PLAN_ID_CONTENT_KEYS[0]
LEGACY_PLAN_ID_KEYS: Tuple[str, ...] = PLAN_ID_CONTENT_KEYS[1:]

# Rooms per batch, and the pause between batches. Each repaired room sends a
# state event that fans out to every member over /sync, so this runs slowly on
# purpose: the backfill is never the reason a homeserver falls behind.
BATCH_SIZE = 20
PAUSE_BETWEEN_BATCHES_SECONDS = 2.0

# Give the homeserver time to finish starting before adding write load.
START_DELAY_SECONDS = 60.0

# A lease older than this is assumed to belong to a worker that died mid-run,
# and may be taken over. Refreshed once per batch.
LEASE_STALE_AFTER_MS = 10 * 60 * 1000

_RUN_AS_BG_SUPPORTS_SERVER_NAME = (
    "server_name" in inspect.signature(run_as_background_process).parameters
)


def _background_process_args(homeserver: Any, desc: str, func: Any) -> Tuple[Any, ...]:
    # Synapse grew a server_name argument; support both signatures.
    if _RUN_AS_BG_SUPPORTS_SERVER_NAME:
        return (desc, homeserver.hostname, func)
    return (desc, func)


class CoursePlanRow(NamedTuple):
    """One room's current course-plan state event."""

    room_id: str
    state_key: str
    content: Dict[str, Any]


class BackfillSummary(NamedTuple):
    """What a run did. Logged at the end, and asserted on in tests."""

    scanned: int = 0
    repaired: int = 0
    already_ok: int = 0
    skipped_no_plan_id: int = 0
    skipped_no_cms: int = 0
    skipped_no_sender: int = 0
    failed: int = 0

    def plus(self, **deltas: int) -> "BackfillSummary":
        return self._replace(
            **{field: getattr(self, field) + delta for field, delta in deltas.items()}
        )


def needs_repair(content: Dict[str, Any]) -> bool:
    """True when the content is missing ``l2`` or spells the plan id legacy-style.

    The idempotency check: a room already carrying a non-empty ``l2`` and its
    plan id under ``uuid`` is left completely alone, so re-running the backfill
    writes nothing.
    """
    if extract_l2(content) is None:
        return True
    canonical = content.get(CANONICAL_PLAN_ID_KEY)
    if not isinstance(canonical, str) or canonical == "":
        return True
    return any(key in content for key in LEGACY_PLAN_ID_KEYS)


def repaired_content(
    content: Dict[str, Any],
    plan_id: str,
    l2: str,
) -> Dict[str, Any]:
    """The content to write: canonical plan id + ``l2``, other keys preserved.

    The legacy plan-id keys are dropped rather than left alongside ``uuid``:
    leaving both is what let two spellings diverge in the first place, and the
    value is preserved exactly, under the canonical key.
    """
    new_content = {
        key: value for key, value in content.items() if key not in LEGACY_PLAN_ID_KEYS
    }
    new_content[CANONICAL_PLAN_ID_KEY] = plan_id
    new_content["l2"] = l2
    return new_content


class PublicCoursesL2Backfill:
    """The one-shot task. Constructed only when the config flag is set."""

    def __init__(self, api: ModuleApi, config: "PangeaChatConfig") -> None:
        self._api = api
        self._config = config
        self._hs = api._hs
        self._clock = self._hs.get_clock()
        self._store = self._hs.get_datastores().main
        self._event_type = (
            config.course_plan_state_event_type
            or DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE
        )

    # -- scheduling ---------------------------------------------------------

    def schedule(self) -> None:
        """Arrange for exactly one run, shortly after startup."""
        self._clock.call_later(
            START_DELAY_SECONDS,
            cast(Any, run_as_background_process),
            *_background_process_args(
                self._hs,
                "pangea_public_courses_backfill_l2",
                self.run,
            ),
        )
        logger.info(
            "public_courses l2 backfill armed; starting in %.0fs",
            START_DELAY_SECONDS,
        )

    # -- lease --------------------------------------------------------------

    async def _ensure_lease_table(self) -> None:
        def _create(txn: Any) -> None:
            txn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {LEASE_TABLE} (
                    lease_key TEXT PRIMARY KEY,
                    claimed_by TEXT NOT NULL,
                    heartbeat_ms BIGINT NOT NULL
                )
                """
            )

        await self._store.db_pool.runInteraction(
            "pangea_public_courses_backfill_l2_create_lease_table",
            _create,
        )

    async def _claim_lease(self, claimed_by: str, now_ms: int) -> bool:
        """Claim the run, or report that another instance already holds it.

        The module is instantiated in every worker, so without this every
        worker would scan and write the same rooms. A row, rather than a
        session-level ``pg_advisory_lock``: Synapse hands out pooled
        connections per interaction, so a session lock would be taken on
        whichever connection happened to serve the claim and could not be
        reliably released by a later one.
        """
        stale_before_ms = now_ms - LEASE_STALE_AFTER_MS

        def _claim(txn: Any) -> bool:
            txn.execute(
                f"""
                INSERT INTO {LEASE_TABLE} (lease_key, claimed_by, heartbeat_ms)
                VALUES (%s, %s, %s)
                ON CONFLICT (lease_key) DO UPDATE
                    SET claimed_by = EXCLUDED.claimed_by,
                        heartbeat_ms = EXCLUDED.heartbeat_ms
                    WHERE {LEASE_TABLE}.heartbeat_ms < %s
                """,
                (LEASE_KEY, claimed_by, now_ms, stale_before_ms),
            )
            return bool(txn.rowcount)

        return await self._store.db_pool.runInteraction(
            "pangea_public_courses_backfill_l2_claim_lease",
            _claim,
        )

    async def _heartbeat_lease(self, claimed_by: str, now_ms: int) -> None:
        def _beat(txn: Any) -> None:
            txn.execute(
                f"""
                UPDATE {LEASE_TABLE}
                SET heartbeat_ms = %s
                WHERE lease_key = %s AND claimed_by = %s
                """,
                (now_ms, LEASE_KEY, claimed_by),
            )

        await self._store.db_pool.runInteraction(
            "pangea_public_courses_backfill_l2_heartbeat_lease",
            _beat,
        )

    async def _release_lease(self, claimed_by: str) -> None:
        """Drop the lease so a deliberate re-run is possible.

        The lease records who is running, not that the work is done. Finishing
        removes it; re-enabling the flag runs the backfill again, which is a
        no-op on rooms already repaired.
        """

        def _release(txn: Any) -> None:
            txn.execute(
                f"DELETE FROM {LEASE_TABLE} WHERE lease_key = %s AND claimed_by = %s",
                (LEASE_KEY, claimed_by),
            )

        await self._store.db_pool.runInteraction(
            "pangea_public_courses_backfill_l2_release_lease",
            _release,
        )

    # -- scan ---------------------------------------------------------------

    async def _fetch_batch(self, after_room_id: Optional[str]) -> List[CoursePlanRow]:
        """Up to ``BATCH_SIZE`` rooms with a current course-plan event.

        Keyset-paginated by room id. ``DISTINCT ON`` collapses a room carrying
        several state keys, preferring the empty one — the same preference the
        catalog query applies, so the backfill repairs the event the catalog
        reads.
        """
        room_predicate = "AND cse.room_id > ?" if after_room_id else ""
        sql = f"""
        SELECT DISTINCT ON (cse.room_id)
            cse.room_id, cse.state_key, ej.json
        FROM current_state_events cse
        INNER JOIN event_json ej ON ej.event_id = cse.event_id
        WHERE cse.type = ?
          {room_predicate}
        ORDER BY cse.room_id, (cse.state_key <> '') ASC, cse.state_key ASC
        LIMIT ?
        """

        params: List[Any] = [self._event_type]
        if after_room_id:
            params.append(after_room_id)
        params.append(BATCH_SIZE)

        rows = await self._store.db_pool.execute(
            "pangea_public_courses_backfill_l2_scan",
            sql,
            *params,
        )

        batch: List[CoursePlanRow] = []
        for room_id, state_key, event_json in rows or []:
            event = (
                json.loads(event_json) if isinstance(event_json, str) else event_json
            )
            content = event.get("content") if isinstance(event, dict) else None
            batch.append(
                CoursePlanRow(
                    room_id=room_id,
                    state_key=state_key or "",
                    content=content if isinstance(content, dict) else {},
                )
            )
        return batch

    # -- repair -------------------------------------------------------------

    async def _resolve_languages(self, plan_ids: List[str]) -> Optional[Dict[str, str]]:
        """CMS languages for *plan_ids*; ``None`` when the CMS could not answer.

        ``None`` is distinct from an empty mapping: an unreachable CMS means
        "ask again another day", not "these plans have no language".
        """
        if not plan_ids:
            return {}
        try:
            return await fetch_plan_languages(
                plan_ids,
                self._config.cms_base_url,
                self._config.cms_service_api_key,
            )
        except CoursePlanLookupError as e:
            logger.warning(
                "public_courses l2 backfill: CMS lookup failed for %d plan id(s), "
                "skipping this batch: %s",
                len(plan_ids),
                e,
            )
            return None

    async def _select_state_sender(self, room_id: str) -> Optional[str]:
        return await select_state_sender(self._api, room_id, self._event_type)

    async def _write_repair(
        self,
        row: CoursePlanRow,
        plan_id: str,
        l2: str,
    ) -> bool:
        """Send the repaired state event. False when no local sender qualifies."""
        sender = await self._select_state_sender(row.room_id)
        if sender is None:
            return False

        await self._api.create_and_send_event_into_room(
            {
                "type": self._event_type,
                "state_key": row.state_key,
                "room_id": row.room_id,
                "sender": sender,
                "content": repaired_content(row.content, plan_id, l2),
            }
        )
        return True

    async def _process_batch(
        self,
        batch: List[CoursePlanRow],
        summary: BackfillSummary,
    ) -> BackfillSummary:
        summary = summary.plus(scanned=len(batch))

        pending: List[Tuple[CoursePlanRow, str]] = []
        for row in batch:
            plan_id = extract_plan_id(row.content)
            if plan_id is None:
                # Not a course by the catalog's rule; nothing to repair.
                summary = summary.plus(skipped_no_plan_id=1)
                continue
            if not needs_repair(row.content):
                summary = summary.plus(already_ok=1)
                continue
            pending.append((row, plan_id))

        if not pending:
            return summary

        # Only rooms actually missing a language need the CMS. A room that
        # merely spells its plan id the old way keeps the l2 it already has.
        plan_ids_needing_language = [
            plan_id for row, plan_id in pending if extract_l2(row.content) is None
        ]
        languages = await self._resolve_languages(plan_ids_needing_language)
        if languages is None:
            return summary.plus(skipped_no_cms=len(pending))

        for row, plan_id in pending:
            l2 = extract_l2(row.content) or languages.get(plan_id)
            if l2 is None:
                logger.info(
                    "public_courses l2 backfill: no CMS language for plan %s "
                    "(room %s), skipping",
                    plan_id,
                    row.room_id,
                )
                summary = summary.plus(skipped_no_cms=1)
                continue

            try:
                written = await self._write_repair(row, plan_id, l2)
            except Exception as e:
                # One unwritable room must not end the run; the summary counts
                # it and the log names it.
                logger.warning(
                    "public_courses l2 backfill: failed to repair room %s: %s",
                    row.room_id,
                    e,
                )
                summary = summary.plus(failed=1)
                continue

            if not written:
                logger.info(
                    "public_courses l2 backfill: no local user with power to send "
                    "%s in room %s, skipping",
                    self._event_type,
                    row.room_id,
                )
                summary = summary.plus(skipped_no_sender=1)
                continue

            summary = summary.plus(repaired=1)

        return summary

    # -- run ----------------------------------------------------------------

    async def run(self) -> BackfillSummary:
        claimed_by = getattr(self._hs, "get_instance_name", lambda: "master")()
        await self._ensure_lease_table()

        if not await self._claim_lease(claimed_by, self._clock.time_msec()):
            logger.info(
                "public_courses l2 backfill: another instance holds the lease, "
                "not running here"
            )
            return BackfillSummary()

        logger.info(
            "public_courses l2 backfill: starting (instance=%s, event_type=%s)",
            claimed_by,
            self._event_type,
        )

        summary = BackfillSummary()
        after_room_id: Optional[str] = None
        try:
            while True:
                batch = await self._fetch_batch(after_room_id)
                if not batch:
                    break

                after_room_id = batch[-1].room_id
                summary = await self._process_batch(batch, summary)

                await self._heartbeat_lease(claimed_by, self._clock.time_msec())
                logger.info(
                    "public_courses l2 backfill: progress %s",
                    self._format_summary(summary),
                )

                if len(batch) < BATCH_SIZE:
                    break
                await self._clock.sleep(PAUSE_BETWEEN_BATCHES_SECONDS)
        finally:
            await self._release_lease(claimed_by)

        logger.info(
            "public_courses l2 backfill: finished %s", self._format_summary(summary)
        )
        return summary

    @staticmethod
    def _format_summary(summary: BackfillSummary) -> str:
        return (
            f"scanned={summary.scanned} repaired={summary.repaired} "
            f"already_ok={summary.already_ok} "
            f"skipped_no_plan_id={summary.skipped_no_plan_id} "
            f"skipped_no_cms={summary.skipped_no_cms} "
            f"skipped_no_sender={summary.skipped_no_sender} "
            f"failed={summary.failed}"
        )
