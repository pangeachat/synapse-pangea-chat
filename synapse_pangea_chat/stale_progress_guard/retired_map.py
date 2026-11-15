"""The environment's retired Can-Do map: old Can-Do row id to merged row id.

Design: stale-progress-guard.instructions.md. Kept in a table of its own in
the homeserver's database, so every worker reads the same map and it outlives
the deletion of the old Can-Dos. Each process keeps a copy and reads the
table again at most once a minute; a change made through this process is read
at once.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from synapse.storage.engines import PostgresEngine

TABLE = "pangea_retired_can_do"
TTL_MS = 60_000
# An arbitrary key for the advisory lock that serialises the table's creation.
_SCHEMA_LOCK = 583104227


class RetiredCanDoMap:
    def __init__(self, db_pool: Any, clock: Any, ttl_ms: int = TTL_MS):
        self._db = db_pool
        self._clock = clock
        self._ttl_ms = ttl_ms
        self._value: Dict[str, str] = {}
        self._read_at: Optional[int] = None
        self._ready = False

    async def current(self) -> Mapping[str, str]:
        """The map, empty when none is loaded."""
        now = self._clock.time_msec()
        if self._read_at is None or now - self._read_at >= self._ttl_ms:
            self._value = await self._load()
            self._read_at = now
        return self._value

    def invalidate(self) -> None:
        self._read_at = None

    async def replace(self, can_do_map: Mapping[str, str]) -> None:
        """Make ``can_do_map`` the whole map, in one transaction."""
        await self._ensure_table()

        def write(txn: Any) -> None:
            txn.execute(f"DELETE FROM {TABLE}")
            txn.execute_batch(
                f"INSERT INTO {TABLE} (old_id, merged_id) VALUES (?, ?)",
                list(can_do_map.items()),
            )

        await self._db.runInteraction("pangea_retired_can_do_replace", write)
        self.invalidate()

    async def clear(self) -> None:
        await self._ensure_table()

        def delete(txn: Any) -> None:
            txn.execute(f"DELETE FROM {TABLE}")

        await self._db.runInteraction("pangea_retired_can_do_clear", delete)
        self.invalidate()

    async def _load(self) -> Dict[str, str]:
        await self._ensure_table()
        rows = await self._db.execute(
            "pangea_retired_can_do_read", f"SELECT old_id, merged_id FROM {TABLE}"
        )
        return dict(rows)

    async def _ensure_table(self) -> None:
        if self._ready:
            return

        def create(txn: Any) -> None:
            if isinstance(self._db.engine, PostgresEngine):
                # IF NOT EXISTS alone can race PostgreSQL's catalog creation
                # when several processes start a newly deployed module.
                txn.execute(f"SELECT pg_advisory_xact_lock({_SCHEMA_LOCK})")
            txn.execute(
                f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                old_id TEXT PRIMARY KEY,
                merged_id TEXT NOT NULL
            )"""
            )

        await self._db.runInteraction("pangea_retired_can_do_schema", create)
        self._ready = True
