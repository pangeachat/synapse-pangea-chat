"""The forwarder's two tables and its reads of Synapse's own.

* ``pangea_analytics_forward_cursor``: the stream position delivered through.
  Written only after the ingest acknowledged a batch, so a restart resumes
  from the last batch the ingest committed.
* ``pangea_analytics_profile_outbox``: one row per user whose profile changed
  and may not have been delivered. ``revision`` grows on every change; a
  delivery clears ``dirty`` only for the revision it sent, so a change that
  lands while a send is in flight is sent again. ``dirty_since`` is the
  earliest undelivered change (milliseconds; 0 for "unknown, as old as
  anything").

PostgreSQL only: the startup re-send reads custom profile fields as JSONB.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from synapse_pangea_chat.analytics_forward.consent import PROFILE_KEY
from synapse_pangea_chat.analytics_forward.wire import FORWARDED_TYPES

_CURSOR_NAME = "analytics_forward"
#: The pending_accepts revision, kept in the cursor table as a second row.
_PENDING_REV_NAME = "pending_accepts_rev"
#: The profiles_clean_before of the last acknowledged batch, a third row: what
#: the ingest holds, so the head can tell a Refresh it need not wait.
_DELIVERED_CLEAN_NAME = "profiles_clean_before"
_UPSERT_GREATEST_SQL = """
    INSERT INTO pangea_analytics_forward_cursor (name, stream_pos) VALUES (?, ?)
    ON CONFLICT (name) DO UPDATE SET stream_pos = GREATEST(
        pangea_analytics_forward_cursor.stream_pos, EXCLUDED.stream_pos
    )
"""
_SELECT_FORWARDED_HEAD_SQL = """
    SELECT COALESCE(MAX(stream_ordering), 0) FROM events
    WHERE stream_ordering > 0 AND stream_ordering <= ?
        AND NOT outlier AND rejection_reason IS NULL
        AND type = ANY(?)
"""

_CREATE_CURSOR_SQL = """
    CREATE TABLE IF NOT EXISTS pangea_analytics_forward_cursor (
        name TEXT PRIMARY KEY,
        stream_pos BIGINT NOT NULL
    )
"""

_CREATE_OUTBOX_SQL = """
    CREATE TABLE IF NOT EXISTS pangea_analytics_profile_outbox (
        user_id TEXT PRIMARY KEY,
        revision BIGINT NOT NULL,
        dirty BOOLEAN NOT NULL,
        dirty_since BIGINT
    )
"""

_CREATE_OUTBOX_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS pangea_analytics_profile_outbox_dirty
    ON pangea_analytics_profile_outbox (dirty_since) WHERE dirty
"""

# A change: one more revision, dirty, and the EARLIEST undelivered change kept.
_MARK_DIRTY_SQL = """
    INSERT INTO pangea_analytics_profile_outbox
        (user_id, revision, dirty, dirty_since)
    VALUES (?, 1, TRUE, ?)
    ON CONFLICT (user_id) DO UPDATE SET
        revision = pangea_analytics_profile_outbox.revision + 1,
        dirty = TRUE,
        dirty_since = COALESCE(
            pangea_analytics_profile_outbox.dirty_since, EXCLUDED.dirty_since
        )
"""

# Startup: every user already in the outbox (a profile since removed is a
# removal still to deliver) and every user with an analytics profile is sent
# again, newer than anything sent before, and dirty since "always".
_RESEND_EXISTING_SQL = """
    UPDATE pangea_analytics_profile_outbox
    SET revision = revision + 1, dirty = TRUE, dirty_since = 0
"""
_RESEND_PROFILED_SQL = """
    INSERT INTO pangea_analytics_profile_outbox
        (user_id, revision, dirty, dirty_since)
    SELECT full_user_id, 1, TRUE, 0 FROM profiles
    WHERE full_user_id IS NOT NULL AND fields -> ?::text IS NOT NULL
    ON CONFLICT (user_id) DO NOTHING
"""

_SELECT_DIRTY_SQL = """
    SELECT user_id, revision, COALESCE(dirty_since, 0)
    FROM pangea_analytics_profile_outbox
    WHERE dirty ORDER BY COALESCE(dirty_since, 0), user_id LIMIT ?
"""
_SELECT_OLDEST_DIRTY_SQL = """
    SELECT MIN(COALESCE(dirty_since, 0)) FROM pangea_analytics_profile_outbox
    WHERE dirty
"""
_ACK_PROFILE_SQL = """
    UPDATE pangea_analytics_profile_outbox
    SET dirty = FALSE, dirty_since = NULL
    WHERE user_id = ? AND revision = ?
"""
_BUMP_REVISION_SQL = """
    INSERT INTO pangea_analytics_profile_outbox
        (user_id, revision, dirty, dirty_since)
    VALUES (?, 1, FALSE, NULL)
    ON CONFLICT (user_id) DO UPDATE SET
        revision = pangea_analytics_profile_outbox.revision + 1
    RETURNING revision
"""

_SELECT_CURSOR_SQL = (
    "SELECT stream_pos FROM pangea_analytics_forward_cursor WHERE name = ?"
)
_INSERT_CURSOR_SQL = """
    INSERT INTO pangea_analytics_forward_cursor (name, stream_pos) VALUES (?, ?)
    ON CONFLICT (name) DO NOTHING
"""
# Never backwards: two misconfigured instances, or a late ack, cannot rewind it.
_ADVANCE_CURSOR_SQL = """
    UPDATE pangea_analytics_forward_cursor
    SET stream_pos = GREATEST(stream_pos, ?) WHERE name = ?
"""

_SELECT_EVENTS_SQL = """
    SELECT stream_ordering, event_id FROM events
    WHERE stream_ordering > ? AND stream_ordering <= ?
        AND NOT outlier AND rejection_reason IS NULL
        AND type = ANY(?)
    ORDER BY stream_ordering LIMIT ?
"""
_FORWARDED_TYPES = sorted(FORWARDED_TYPES)

_SELECT_PENDING_INVITES_SQL = """
    SELECT lcm.room_id, lcm.user_id
    FROM local_current_membership AS lcm
    JOIN current_state_events AS cse
        ON cse.room_id = lcm.room_id
        AND cse.type = 'm.room.create' AND cse.state_key = ''
    JOIN event_json AS ej ON ej.event_id = cse.event_id
    WHERE lcm.membership = 'invite'
        AND ej.json::jsonb -> 'content' ->> 'type' = 'p.analytics'
    ORDER BY lcm.room_id, lcm.user_id
"""


_SELECT_UNDELIVERED_JOIN_SQL = """
    SELECT COALESCE(MAX(e.stream_ordering), 0)
    FROM events AS e
    JOIN room_memberships AS m ON m.event_id = e.event_id
    JOIN current_state_events AS cse
        ON cse.room_id = e.room_id
        AND cse.type = 'm.room.create' AND cse.state_key = ''
    JOIN event_json AS ej ON ej.event_id = cse.event_id
    WHERE e.stream_ordering > ? AND e.type = 'm.room.member'
        AND m.membership = 'join' AND m.sender = m.user_id
        AND ej.json::jsonb -> 'content' ->> 'type' = 'p.analytics'
"""


class ForwardStore:
    def __init__(self, homeserver: Any) -> None:
        self._hs = homeserver
        self._main = homeserver.get_datastores().main
        self._pool = self._main.db_pool
        self._ready = False

    async def ensure_tables(self) -> None:
        if self._ready:
            return
        from synapse.storage.engines import PostgresEngine

        if not isinstance(self._pool.engine, PostgresEngine):
            raise RuntimeError("analytics_forward requires a PostgreSQL database")

        def _create(txn: Any) -> None:
            txn.execute(_CREATE_CURSOR_SQL)
            txn.execute(_CREATE_OUTBOX_SQL)
            txn.execute(_CREATE_OUTBOX_INDEX_SQL)

        await self._pool.runInteraction("pangea_analytics_forward_create", _create)
        self._ready = True

    # -- the event stream ---------------------------------------------------

    def head(self) -> int:
        """The position every event at or below which is persisted."""
        return int(self._main.get_room_max_stream_ordering())

    async def load_cursor(self, initial: int) -> int:
        """The stored cursor, storing ``initial`` first when there is none."""
        await self.ensure_tables()

        def _load(txn: Any) -> int:
            txn.execute(_INSERT_CURSOR_SQL, (_CURSOR_NAME, initial))
            txn.execute(_SELECT_CURSOR_SQL, (_CURSOR_NAME,))
            row = txn.fetchone()
            return int(row[0])

        return await self._pool.runInteraction("pangea_analytics_forward_cursor", _load)

    async def read_events(
        self, after: int, upto: int, limit: int
    ) -> Tuple[List[Any], int]:
        """Forwarded events in ``(after, upto]``, oldest first, at most ``limit``.

        Returns them and the position delivery reaches once they are
        delivered: the last row's when the read was full (more may follow),
        else ``upto`` (nothing more of interest below it).
        """
        if upto <= after:
            return [], after

        def _select(txn: Any) -> List[Tuple[int, str]]:
            txn.execute(_SELECT_EVENTS_SQL, (after, upto, _FORWARDED_TYPES, limit))
            return [(int(pos), str(eid)) for pos, eid in txn.fetchall()]

        rows = await self._pool.runInteraction("pangea_analytics_forward_read", _select)
        reached = rows[-1][0] if len(rows) >= limit else upto
        # Rejected events are dropped here too; a dropped one is never sent.
        events = await self._main.get_events_as_list([eid for _, eid in rows])
        return events, reached

    async def load_pending_rev(self) -> int:
        def _load(txn: Any) -> int:
            txn.execute(_INSERT_CURSOR_SQL, (_PENDING_REV_NAME, 0))
            txn.execute(_SELECT_CURSOR_SQL, (_PENDING_REV_NAME,))
            return int(txn.fetchone()[0])

        return await self._pool.runInteraction("pangea_analytics_pending_rev", _load)

    async def save_pending_rev(self, rev: int) -> None:
        await self._pool.runInteraction(
            "pangea_analytics_pending_rev_save",
            lambda txn: txn.execute(_ADVANCE_CURSOR_SQL, (rev, _PENDING_REV_NAME)),
        )

    async def forwarded_head(self, upto: int) -> int:
        """The newest forwarded event at or below ``upto``, or 0."""

        def _select(txn: Any) -> int:
            txn.execute(_SELECT_FORWARDED_HEAD_SQL, (upto, _FORWARDED_TYPES))
            return int(txn.fetchone()[0])

        return await self._pool.runInteraction(
            "pangea_analytics_forwarded_head", _select
        )

    async def oldest_dirty(self) -> Optional[int]:
        def _select(txn: Any) -> Optional[int]:
            txn.execute(_SELECT_OLDEST_DIRTY_SQL)
            value = txn.fetchone()[0]
            return int(value) if value is not None else None

        return await self._pool.runInteraction("pangea_analytics_oldest_dirty", _select)

    async def delivered_clean_before(self) -> Optional[int]:
        def _select(txn: Any) -> Optional[int]:
            txn.execute(_SELECT_CURSOR_SQL, (_DELIVERED_CLEAN_NAME,))
            row = txn.fetchone()
            return int(row[0]) if row is not None else None

        return await self._pool.runInteraction(
            "pangea_analytics_delivered_clean", _select
        )

    async def advance(
        self,
        cursor: int,
        acked: Sequence[Tuple[str, int]],
        clean_before: Optional[int] = None,
    ) -> None:
        """After an acknowledgement: the new cursor and the delivered profiles,
        in one transaction."""

        def _ack(txn: Any) -> None:
            txn.execute(_ADVANCE_CURSOR_SQL, (cursor, _CURSOR_NAME))
            for user_id, revision in acked:
                txn.execute(_ACK_PROFILE_SQL, (user_id, revision))
            if clean_before is not None:
                txn.execute(_UPSERT_GREATEST_SQL, (_DELIVERED_CLEAN_NAME, clean_before))

        await self._pool.runInteraction("pangea_analytics_forward_ack", _ack)

    # -- the profile outbox -------------------------------------------------

    async def mark_profile_dirty(self, user_id: str, now_ms: int) -> None:
        await self.ensure_tables()
        await self._pool.runInteraction(
            "pangea_analytics_profile_dirty",
            lambda txn: txn.execute(_MARK_DIRTY_SQL, (user_id, now_ms)),
        )

    async def mark_all_for_resend(self) -> None:
        await self.ensure_tables()

        def _resend(txn: Any) -> None:
            txn.execute(_RESEND_EXISTING_SQL)
            txn.execute(_RESEND_PROFILED_SQL, (PROFILE_KEY,))

        await self._pool.runInteraction("pangea_analytics_profile_resend", _resend)

    async def dirty_profiles(
        self, limit: int
    ) -> Tuple[List[Tuple[str, int]], Optional[int]]:
        """The ``limit`` oldest dirty rows, and the oldest ``dirty_since`` of
        the dirty rows NOT among them (``None`` when there are none).

        Once a batch carrying those rows is acknowledged they are delivered at
        the revision read, and a change made since bumped the revision after
        this read, so the rest is what holds ``profiles_clean_before`` back.
        """

        def _select(txn: Any) -> Tuple[List[Tuple[str, int]], Optional[int]]:
            txn.execute(_SELECT_DIRTY_SQL, (limit + 1,))
            found = [
                (str(uid), int(rev), int(since)) for uid, rev, since in txn.fetchall()
            ]
            rest = found[limit][2] if len(found) > limit else None
            return [(uid, rev) for uid, rev, _ in found[:limit]], rest

        return await self._pool.runInteraction(
            "pangea_analytics_profile_dirty_read", _select
        )

    async def bump_revisions(self, user_ids: Iterable[str]) -> Dict[str, int]:
        """A fresh revision for each user, for a snapshot read after it."""
        await self.ensure_tables()

        def _bump(txn: Any) -> Dict[str, int]:
            out: Dict[str, int] = {}
            for user_id in user_ids:
                txn.execute(_BUMP_REVISION_SQL, (user_id,))
                out[user_id] = int(txn.fetchone()[0])
            return out

        return await self._pool.runInteraction("pangea_analytics_profile_bump", _bump)

    async def profiles(self, user_ids: Sequence[str]) -> Dict[str, Any]:
        """Each user's whole current profile (local users only)."""
        return dict(await self._main.get_profile_data_for_users(list(user_ids)))

    # -- consent ------------------------------------------------------------

    async def undelivered_analytics_join(self, after: int) -> int:
        """The position of the newest self-join into an analytics room above
        ``after``, or 0."""

        def _select(txn: Any) -> int:
            txn.execute(_SELECT_UNDELIVERED_JOIN_SQL, (after,))
            return int(txn.fetchone()[0])

        return await self._pool.runInteraction(
            "pangea_analytics_undelivered_join", _select
        )

    async def pending_analytics_invites(self) -> List[Tuple[str, str]]:
        """Every local user's current invite into an analytics room."""

        def _select(txn: Any) -> List[Tuple[str, str]]:
            txn.execute(_SELECT_PENDING_INVITES_SQL)
            return [(str(room), str(user)) for room, user in txn.fetchall()]

        return await self._pool.runInteraction(
            "pangea_analytics_pending_invites", _select
        )
