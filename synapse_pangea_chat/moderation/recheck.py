"""Messages Tier 2 could not check, waiting to be checked again.

One row per message that ended with no usable verdict - the service was
down, timed out, answered something unreadable, or the handler raised. The
Tier-2 instance re-checks the least-tried, oldest rows on a timer through the ordinary
single-message path, and deletes a row once that path has a verdict, once the
message is gone, or once the row is a week old.
"""

from typing import Any, List, Tuple

#: A row this old is given up on: the message has been readable, unchecked,
#: for a week, and a check now protects nobody it would have protected then.
MAX_AGE_SECONDS = 7 * 24 * 60 * 60

# Written out rather than formatted, as in `moderation.disposition`: a query
# built by formatting reads, to bandit and to a reviewer, like one built out
# of user input.
_CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS pangea_moderation_recheck (
        event_id TEXT PRIMARY KEY,
        room_id TEXT NOT NULL,
        queued_at BIGINT NOT NULL,
        attempts INTEGER NOT NULL
    )
"""

_ADD_SQL = """
    INSERT INTO pangea_moderation_recheck (event_id, room_id, queued_at, attempts)
    VALUES (?, ?, ?, 0)
    ON CONFLICT (event_id) DO NOTHING
"""

_OLDEST_SQL = """
    SELECT event_id, room_id, queued_at FROM pangea_moderation_recheck
    ORDER BY attempts, queued_at, event_id LIMIT ?
"""

_DELETE_SQL = "DELETE FROM pangea_moderation_recheck WHERE event_id = ?"

_BUMP_SQL = """
    UPDATE pangea_moderation_recheck SET attempts = attempts + 1
    WHERE event_id = ?
"""


class RecheckStore:
    """Reads and writes `pangea_moderation_recheck`. Every method raises on a
    database failure; the caller decides what that costs."""

    def __init__(self, homeserver: Any) -> None:
        self._hs = homeserver
        self._table_ready = False

    async def _run(
        self, desc: str, sql: str, args: Tuple[Any, ...], fetch: bool = False
    ) -> Any:
        pool = self._hs.get_datastores().main.db_pool
        if not self._table_ready:

            def _create(txn: Any) -> None:
                txn.execute(_CREATE_TABLE_SQL)

            await pool.runInteraction("pangea_moderation_create_recheck_table", _create)
            self._table_ready = True

        def _execute(txn: Any) -> Any:
            txn.execute(sql, args)
            return txn.fetchall() if fetch else None

        return await pool.runInteraction(desc, _execute)

    async def add(self, event_id: str, room_id: str, queued_at_ms: int) -> None:
        await self._run(
            "pangea_moderation_recheck_add", _ADD_SQL, (event_id, room_id, queued_at_ms)
        )

    async def oldest(self, limit: int) -> List[Tuple[str, str, int]]:
        rows = await self._run(
            "pangea_moderation_recheck_oldest", _OLDEST_SQL, (limit,), fetch=True
        )
        return [(str(row[0]), str(row[1]), int(row[2])) for row in rows]

    async def delete(self, event_id: str) -> None:
        await self._run("pangea_moderation_recheck_delete", _DELETE_SQL, (event_id,))

    async def bump(self, event_id: str) -> None:
        await self._run("pangea_moderation_recheck_bump", _BUMP_SQL, (event_id,))
