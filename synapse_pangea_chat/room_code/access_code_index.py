"""Module-owned lookup table for room access codes.

Why this exists
---------------
``get_rooms_with_access_code`` used to answer "which room has code X?" by
casting *every* room's ``m.room.join_rules`` JSON to ``jsonb`` and comparing.
That is O(total rooms) per call, on Synapse's shared database, and four
endpoints share it (``knock_with_code``, ``preview_with_code``,
``request_room_code``, ``create_course_space``). At the staging room count it
measured ~12 s median at 50 VU and broke at 150 — issue #163.

The index is a small table with one row per room that carries a join-rules
event, and a plain btree on each code column. A lookup becomes an index probe
whose cost does not grow with the room count.

Two properties keep it honest
-----------------------------
* **Synapse state remains the source of truth.** A hit here only narrows the
  candidate set; ``get_rooms_with_access_code`` then re-reads
  ``current_state_events`` for those few rooms and compares the code itself.
  A stale row — purged room, a burn that never reached the hook — costs one
  wasted index probe and can never produce a wrong answer.
* **The index is not consulted until it is complete.** Until the startup
  backfill marks itself done, every lookup falls back to the old full scan.
  The deploy is therefore a speedup, never a window in which class codes stop
  resolving.

Maintenance is the ``on_new_event`` third-party-rules hook the module already
registers for ``room_preview``, plus the one-time backfill in
``access_code_backfill``. The hook always writes the freshest truth, so the
backfill inserts only rooms it has not already seen (``DO NOTHING`` on
conflict) and the two cannot race into a stale value.
"""

from __future__ import annotations

import logging
from typing import Any, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from synapse_pangea_chat.room_code.constants import (
    ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY,
    EVENT_TYPE_M_ROOM_JOIN_RULES,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.room_code.access_code_index"
)

INDEX_TABLE = "pangea_room_access_code"
LEASE_TABLE = "pangea_room_access_code_backfill"
LEASE_KEY = "room_code_index"

# Set from PangeaChat.__init__ so the read path can be switched back to the
# full scan without a code change, the same way configure_delayed_push works.
_enabled = True

# Both are one-way latches within a process: the schema only ever comes into
# existence, and a completed backfill is never un-completed under a running
# module. There is no in-place rebuild path precisely so that this stays true —
# see the module docstring in access_code_backfill.
_schema_ready = False
_index_ready = False


class RoomCodes(NamedTuple):
    """The two codes carried by one join-rules event, lowercased."""

    access_code_lower: Optional[str]
    admin_access_code_lower: Optional[str]

    @property
    def is_empty(self) -> bool:
        return self.access_code_lower is None and self.admin_access_code_lower is None


def configure_access_code_index(enabled: bool) -> None:
    """Arm or disarm the index for this process."""
    global _enabled
    _enabled = enabled
    if not enabled:
        logger.warning(
            "room code index disabled by config; access code lookups will scan "
            "every room's join rules"
        )


def is_enabled() -> bool:
    return _enabled


def code_text(value: Any) -> Optional[str]:
    """The comparable text of a code field, or None when it isn't one.

    Mirrors what PostgreSQL's ``->>`` did for the old scan: a JSON string comes
    through as itself, a JSON number as its text. ``true``/``false``/``null``
    and containers are not codes. Index writes and verification both go through
    here so the two can never disagree about what a stored code *is*.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def extract_codes(content: Any) -> RoomCodes:
    """The access/admin codes in a join-rules event content, lowercased.

    Takes any Mapping, not just ``dict``: an event's content is an
    ``immutabledict`` when the homeserver runs with frozen dicts.
    """
    if not isinstance(content, Mapping):
        return RoomCodes(None, None)

    access = code_text(content.get(ACCESS_CODE_JOIN_RULE_CONTENT_KEY))
    admin = code_text(content.get(ADMIN_ACCESS_CODE_JOIN_RULE_CONTENT_KEY))
    return RoomCodes(
        access_code_lower=access.lower() if access is not None else None,
        admin_access_code_lower=admin.lower() if admin is not None else None,
    )


async def ensure_schema(store: Any) -> None:
    """Create the index and lease tables if they aren't there yet.

    Idempotent and memoized per process. Called from every entry point rather
    than once at startup because a request can arrive before the backfill's
    start delay has elapsed.
    """
    global _schema_ready
    if _schema_ready:
        return

    def _create(txn: Any) -> None:
        txn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {INDEX_TABLE} (
                room_id TEXT NOT NULL,
                state_key TEXT NOT NULL,
                access_code_lower TEXT,
                admin_access_code_lower TEXT,
                PRIMARY KEY (room_id, state_key)
            )
            """
        )
        # Partial, because most rooms carry a join code and almost none carry
        # an admin code: the admin index then holds only the handful of rows
        # that could ever match. `= ?` implies NOT NULL, so both probes still
        # match their index.
        txn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {INDEX_TABLE}_access_code_idx
            ON {INDEX_TABLE} (access_code_lower)
            WHERE access_code_lower IS NOT NULL
            """
        )
        txn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {INDEX_TABLE}_admin_access_code_idx
            ON {INDEX_TABLE} (admin_access_code_lower)
            WHERE admin_access_code_lower IS NOT NULL
            """
        )
        txn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {LEASE_TABLE} (
                lease_key TEXT PRIMARY KEY,
                claimed_by TEXT NOT NULL,
                heartbeat_ms BIGINT NOT NULL,
                completed_at_ms BIGINT
            )
            """
        )

    await store.db_pool.runInteraction(
        "pangea_room_code_index_ensure_schema",
        _create,
    )
    _schema_ready = True


async def index_is_ready(store: Any) -> bool:
    """True once the backfill has finished and the index may be trusted.

    Before that, callers must keep scanning: a partially populated index would
    answer "no such code" for a room it simply hasn't reached yet, which is the
    one failure this feature cannot afford.

    Never raises. Every way of failing to establish readiness — including the
    schema races several workers can hit creating these tables at once — means
    "not ready", which costs a slow lookup rather than a failed one.
    """
    global _index_ready
    if not _enabled:
        return False
    if _index_ready:
        return True

    try:
        await ensure_schema(store)

        rows = await store.db_pool.execute(
            "pangea_room_code_index_is_ready",
            f"SELECT completed_at_ms FROM {LEASE_TABLE} WHERE lease_key = ?",
            LEASE_KEY,
        )
    except Exception as e:
        logger.warning("room code index: readiness check failed: %s", e)
        return False

    for row in rows or []:
        if row[0] is not None:
            _index_ready = True
            return True
    return False


async def lookup_candidate_room_ids(store: Any, access_code: str) -> List[str]:
    """Rooms the index believes carry *access_code*, in either field.

    Two single-column probes unioned rather than one ``OR`` across two columns:
    the ``OR`` shape is exactly what stops the planner from using either index.
    """
    code_lower = access_code.lower()
    rows = await store.db_pool.execute(
        "pangea_room_code_index_lookup",
        f"""
        SELECT room_id FROM {INDEX_TABLE} WHERE access_code_lower = ?
        UNION
        SELECT room_id FROM {INDEX_TABLE} WHERE admin_access_code_lower = ?
        """,
        code_lower,
        code_lower,
    )
    return [row[0] for row in rows or []]


def upsert_row_txn(
    txn: Any,
    room_id: str,
    state_key: str,
    codes: RoomCodes,
) -> None:
    """Write one room's codes, replacing whatever was there.

    Used by the event hook, which by definition holds the newest state for the
    room it was called about.
    """
    if codes.is_empty:
        txn.execute(
            f"DELETE FROM {INDEX_TABLE} WHERE room_id = ? AND state_key = ?",
            (room_id, state_key),
        )
        return

    txn.execute(
        f"""
        INSERT INTO {INDEX_TABLE}
            (room_id, state_key, access_code_lower, admin_access_code_lower)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (room_id, state_key) DO UPDATE SET
            access_code_lower = EXCLUDED.access_code_lower,
            admin_access_code_lower = EXCLUDED.admin_access_code_lower
        """,
        (
            room_id,
            state_key,
            codes.access_code_lower,
            codes.admin_access_code_lower,
        ),
    )


def insert_missing_rows_txn(
    txn: Any,
    rows: Sequence[Tuple[str, str, RoomCodes]],
) -> None:
    """Insert backfilled rooms, leaving any the hook already wrote alone.

    ``DO NOTHING`` is what makes the backfill safe to run alongside live
    traffic: a row present at this point came from the hook and is therefore
    at least as fresh as the state this batch scanned.
    """
    for room_id, state_key, codes in rows:
        if codes.is_empty:
            continue
        txn.execute(
            f"""
            INSERT INTO {INDEX_TABLE}
                (room_id, state_key, access_code_lower, admin_access_code_lower)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (room_id, state_key) DO NOTHING
            """,
            (
                room_id,
                state_key,
                codes.access_code_lower,
                codes.admin_access_code_lower,
            ),
        )


async def record_join_rules_event(
    store: Any,
    room_id: str,
    state_key: str,
    content: Mapping[str, Any],
) -> None:
    """Keep the index in step with a join-rules event that just landed.

    Failures are logged and swallowed: this runs inside Synapse's event
    notification path, and a broken index must never stop an event from being
    delivered. A dropped write is recoverable (rebuild the index); a wedged
    event stream is not.
    """
    if not _enabled:
        return

    try:
        await ensure_schema(store)
        codes = extract_codes(content)
        await store.db_pool.runInteraction(
            "pangea_room_code_index_record",
            upsert_row_txn,
            room_id,
            state_key,
            codes,
        )
    except Exception as e:
        logger.warning(
            "room code index: failed to record join rules for room %s: %s",
            room_id,
            e,
        )


def is_join_rules_event(event_type: str) -> bool:
    return event_type == EVENT_TYPE_M_ROOM_JOIN_RULES
