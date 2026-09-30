"""In-memory index from class/admin code to rooms.

Design: knock-with-code.instructions.md, "Code Lookup". The index only
suggests rooms; each room's current join rules decide.
"""

from __future__ import annotations

import json
import weakref
from typing import Any, Collection, Dict, List, NamedTuple, Optional, Set, Tuple

from synapse.storage.database import make_in_list_sql_clause
from synapse.util.async_helpers import Linearizer


class RoomCodeMatch(NamedTuple):
    room_id: str
    is_admin_code: bool


_JOIN_RULES_SQL = """
    SELECT cse.room_id, ej.json
    FROM current_state_events AS cse
    JOIN event_json AS ej USING (event_id)
    WHERE cse.type = 'm.room.join_rules' AND cse.state_key = ''
"""


def _codes_in(event_json: str) -> Tuple[Optional[str], Optional[str]]:
    """The (access_code, admin_access_code) a join-rules event carries, lowercased."""
    content = json.loads(event_json).get("content")
    if not isinstance(content, dict):
        return None, None
    access, admin = content.get("access_code"), content.get("admin_access_code")
    return (
        access.lower() if isinstance(access, str) else None,
        admin.lower() if isinstance(admin, str) else None,
    )


class CodeIndex:
    def __init__(self, store: Any) -> None:
        self._store = store
        self._rooms_by_code: Dict[str, Set[str]] = {}
        self._codes_by_room: Dict[str, Set[str]] = {}
        # Every join-rules change at or below this stream position is in the
        # index. None until the first lookup loads everything.
        self._position: Optional[int] = None
        self._refreshes_started = 0
        self._refresh_lock = Linearizer(
            name="pangea_code_index", clock=store.hs.get_clock()
        )

    async def lookup(self, access_code: str) -> List[RoomCodeMatch]:
        code = access_code.lower()
        matches = await self._confirmed(code)
        if matches:
            return matches
        # A miss, or every candidate was stale: catch up and check again.
        await self._refresh_started_after_now()
        return await self._confirmed(code)

    async def _confirmed(self, code: str) -> List[RoomCodeMatch]:
        candidates = self._rooms_by_code.get(code)
        if not candidates:
            return []
        clause, args = make_in_list_sql_clause(
            self._store.database_engine, "cse.room_id", sorted(candidates)
        )
        rows = await self._store.db_pool.execute(
            "pangea_code_index_confirm", f"{_JOIN_RULES_SQL} AND {clause}", *args
        )
        matches = []
        for room_id, event_json in rows:
            access, admin = _codes_in(event_json)
            if admin == code:
                matches.append(RoomCodeMatch(room_id=room_id, is_admin_code=True))
            elif access == code:
                matches.append(RoomCodeMatch(room_id=room_id, is_admin_code=False))
        return matches

    async def _refresh_started_after_now(self) -> None:
        # A refresh already running may have read its position before this
        # lookup arrived, so it cannot answer for it. Wait for it, then share
        # the next refresh with every lookup that queued behind it: a burst
        # of misses costs at most two queries.
        wanted = self._refreshes_started + 1
        async with self._refresh_lock.queue(None):
            if self._refreshes_started >= wanted:
                return
            self._refreshes_started += 1
            try:
                await self._refresh()
            except BaseException:
                # A failed refresh answers for nobody: the next waiter must
                # run its own rather than trust the stale index.
                self._refreshes_started -= 1
                raise

    async def _refresh(self) -> None:
        # Read the committed position before the rows. Events can commit out of
        # stream order, so recording the highest position seen could skip a
        # row that commits late; this way a row is at worst read twice.
        position = self._store.get_room_max_stream_ordering()
        args: Tuple[Any, ...] = ()
        if self._position is None:
            # Unfiltered: rows written before Synapse tracked stream ordering
            # on current state have none.
            sql = _JOIN_RULES_SQL
        else:
            sql = f"{_JOIN_RULES_SQL} AND cse.event_stream_ordering > ?"
            args = (self._position,)
        rows = await self._store.db_pool.execute(
            "pangea_code_index_refresh", sql, *args
        )
        for room_id, event_json in rows:
            self._index_room(room_id, _codes_in(event_json))
        self._position = position

    def _index_room(self, room_id: str, codes: Collection[Optional[str]]) -> None:
        for old in self._codes_by_room.pop(room_id, ()):
            rooms = self._rooms_by_code[old]
            rooms.discard(room_id)
            if not rooms:
                del self._rooms_by_code[old]
        new = {code for code in codes if code}
        if new:
            self._codes_by_room[room_id] = new
            for code in new:
                self._rooms_by_code.setdefault(code, set()).add(room_id)


_indexes: "weakref.WeakKeyDictionary[Any, CodeIndex]" = weakref.WeakKeyDictionary()


def code_index_for(store: Any) -> CodeIndex:
    """The process's one index for this datastore, created on first use."""
    index = _indexes.get(store)
    if index is None:
        index = _indexes[store] = CodeIndex(store)
    return index
