"""Per-user activity the module keeps for the ``user_activity`` route (user-activity.instructions.md,
"DB query strategy").

The route used to aggregate the whole ``user_ips`` table on every call and scan ``events`` (which has no
sender index) for the page's last messages: on production that was a minute per call and the database's
memory with it. Now the module keeps one small table, ``pangea_user_activity``, one row per user with
their last login and last message time, and brings it up to date incrementally: logins from the
``user_ips`` rows seen since the last refresh (``user_ips_last_seen_only`` index), messages from the
events with a stream ordering past the last refresh (the primary key), in bounded chunks. The route
reads only that table. A refresh runs on a loop, on demand through the refresh endpoint, and in full
(a rebuild) when asked; the first refresh after the table is created is the one full scan.

Postgres only (``GREATEST``, ``ON CONFLICT``), as the route already was.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple, overload

logger = logging.getLogger("synapse_pangea_chat.user_activity.activity_cache")

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS pangea_user_activity (
        user_id TEXT PRIMARY KEY,
        last_login_ts BIGINT NOT NULL DEFAULT 0,
        last_message_ts BIGINT NOT NULL DEFAULT 0,
        updated_at_ms BIGINT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS pangea_user_activity_login
        ON pangea_user_activity (last_login_ts)""",
    """CREATE INDEX IF NOT EXISTS pangea_user_activity_message
        ON pangea_user_activity (last_message_ts)""",
    """CREATE TABLE IF NOT EXISTS pangea_user_activity_watermark (
        name TEXT PRIMARY KEY,
        value BIGINT NOT NULL)""",
)

#: The watermarks: the newest ``user_ips.last_seen`` folded in, the highest ``events.stream_ordering``
#: folded in, and when the last refresh finished.
WM_LOGINS = "user_ips_last_seen"
WM_EVENTS = "events_stream_ordering"
WM_REFRESHED = "refreshed_at_ms"

#: Stream orderings per events scan: one index range, one bounded transaction, so a rebuild on a
#: large server is many short transactions rather than one long one.
EVENTS_CHUNK = 200_000

UPSERT = """
INSERT INTO pangea_user_activity (user_id, last_login_ts, last_message_ts, updated_at_ms)
VALUES (?, ?, ?, ?)
ON CONFLICT (user_id) DO UPDATE SET
    last_login_ts = GREATEST(pangea_user_activity.last_login_ts, EXCLUDED.last_login_ts),
    last_message_ts = GREATEST(pangea_user_activity.last_message_ts, EXCLUDED.last_message_ts),
    updated_at_ms = EXCLUDED.updated_at_ms
"""

SET_WATERMARK = """
INSERT INTO pangea_user_activity_watermark (name, value) VALUES (?, ?)
ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value
"""


class UserActivityCache:
    """The table and its refresh. One refresh at a time per process (``_lock``)."""

    def __init__(self, db_pool: Any) -> None:
        self.db = db_pool
        self.ready = False
        self._lock = asyncio.Lock()

    async def ensure(self) -> None:
        if self.ready:
            return

        def create(txn: Any) -> None:
            for sql in SCHEMA:
                txn.execute(sql)

        await self.db.runInteraction("pangea_user_activity_schema", create)
        self.ready = True

    async def refreshed_at(self) -> Optional[int]:
        """When the last refresh finished (epoch ms), or None before the first one."""
        await self.ensure()
        return await self._watermark(WM_REFRESHED, default=None)

    async def refresh(self, *, full: bool = False) -> Dict[str, Any]:
        """Bring the table up to date: incrementally, or from scratch with ``full``. Answers with what
        moved and how long it took; the counts are rows upserted, not users whose times changed.
        """
        async with self._lock:
            await self.ensure()
            started = time.monotonic()
            now_ms = int(time.time() * 1000)
            if full:
                await self.db.runInteraction(
                    "pangea_user_activity_reset", self._reset_txn
                )
            logins = await self._refresh_logins(now_ms)
            messages, chunks = await self._refresh_messages(now_ms)
            await self.db.runInteraction(
                "pangea_user_activity_refreshed",
                self._set_watermark_txn,
                WM_REFRESHED,
                now_ms,
            )
            out = {
                "full": full,
                "refreshed_at": now_ms,
                "logins_upserted": logins,
                "messages_upserted": messages,
                "event_chunks": chunks,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
            logger.info("user activity refreshed: %s", out)
            return out

    # -- the two reads ------------------------------------------------------------------------

    async def _refresh_logins(self, now_ms: int) -> int:
        since = await self._watermark(WM_LOGINS, default=0)

        def fold(txn: Any) -> int:
            txn.execute(
                "SELECT user_id, MAX(last_seen) FROM user_ips WHERE last_seen > ? GROUP BY user_id",
                (since,),
            )
            rows: List[Tuple[str, int]] = [
                (r[0], int(r[1])) for r in txn.fetchall() if r[1] is not None
            ]
            if rows:
                txn.execute_batch(UPSERT, [(uid, ts, 0, now_ms) for uid, ts in rows])
                self._set_watermark_txn(txn, WM_LOGINS, max(ts for _, ts in rows))
            return len(rows)

        return await self.db.runInteraction("pangea_user_activity_logins", fold)

    async def _refresh_messages(self, now_ms: int) -> Tuple[int, int]:
        since = await self._watermark(WM_EVENTS, default=0)
        top_rows = await self.db.execute(
            "pangea_user_activity_top", "SELECT MAX(stream_ordering) FROM events"
        )
        top = int(top_rows[0][0] or 0) if top_rows else 0
        upserted = chunks = 0
        while since < top:
            upto = min(since + EVENTS_CHUNK, top)

            def fold(txn: Any, lo: int = since, hi: int = upto) -> int:
                txn.execute(
                    """
                    SELECT sender, MAX(origin_server_ts)
                    FROM events
                    WHERE stream_ordering > ? AND stream_ordering <= ? AND type = 'm.room.message'
                    GROUP BY sender
                    """,
                    (lo, hi),
                )
                rows = [(r[0], int(r[1])) for r in txn.fetchall() if r[1] is not None]
                if rows:
                    txn.execute_batch(
                        UPSERT, [(uid, 0, ts, now_ms) for uid, ts in rows]
                    )
                self._set_watermark_txn(txn, WM_EVENTS, hi)
                return len(rows)

            upserted += await self.db.runInteraction(
                "pangea_user_activity_messages", fold
            )
            chunks += 1
            since = upto
        return upserted, chunks

    # -- watermarks ---------------------------------------------------------------------------

    @overload
    async def _watermark(self, name: str, *, default: int) -> int:
        ...

    @overload
    async def _watermark(self, name: str, *, default: None) -> Optional[int]:
        ...

    async def _watermark(self, name: str, *, default: Optional[int]) -> Optional[int]:
        rows = await self.db.execute(
            "pangea_user_activity_watermark",
            "SELECT value FROM pangea_user_activity_watermark WHERE name = ?",
            name,
        )
        return int(rows[0][0]) if rows else default

    @staticmethod
    def _set_watermark_txn(txn: Any, name: str, value: int) -> None:
        txn.execute(SET_WATERMARK, (name, int(value)))

    @staticmethod
    def _reset_txn(txn: Any) -> None:
        txn.execute("DELETE FROM pangea_user_activity")
        txn.execute("DELETE FROM pangea_user_activity_watermark")
