"""Unit tests for access-code resolution (issue #163).

Both paths run against the same in-memory SQLite fixture — a stand-in for
`current_state_events` / `events` / `event_json` plus the module's own index —
so the central claim of the change can actually be asserted: the indexed path
and the full scan return the same answer, and a stale index row cannot make
them differ.
"""

import json
import sqlite3
import types
import unittest
from typing import Any, List, Optional, Tuple

from synapse_pangea_chat.room_code import access_code_index
from synapse_pangea_chat.room_code.access_code_index import (
    LEASE_KEY,
    LEASE_TABLE,
    configure_access_code_index,
    ensure_schema,
    record_join_rules_event,
)
from synapse_pangea_chat.room_code.get_rooms_with_access_code import (
    RoomCodeMatch,
    get_rooms_with_access_code,
)

JOIN_RULES = "m.room.join_rules"


class FakeDbPool:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # get_rooms_with_access_code picks its scan dialect off this.
        self.engine = types.SimpleNamespace(module=types.SimpleNamespace())
        self.engine.module.__name__ = "sqlite3"

    async def runInteraction(self, desc: str, func: Any, *args: Any) -> Any:
        txn = self._conn.cursor()
        try:
            result = func(txn, *args)
            self._conn.commit()
            return result
        except Exception:
            self._conn.rollback()
            raise
        finally:
            txn.close()

    async def execute(self, desc: str, query: str, *args: Any) -> List[Tuple[Any, ...]]:
        txn = self._conn.cursor()
        try:
            txn.execute(query, args)
            return txn.fetchall()
        finally:
            txn.close()


class FakeStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.db_pool = FakeDbPool(conn)


class GetRoomsWithAccessCodeTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.executescript(
            """
            CREATE TABLE current_state_events (
                room_id TEXT, type TEXT, state_key TEXT, event_id TEXT
            );
            CREATE TABLE events (event_id TEXT);
            CREATE TABLE event_json (event_id TEXT, json TEXT);
            """
        )
        self.store = FakeStore(self._conn)
        access_code_index._schema_ready = False
        access_code_index._index_ready = False
        configure_access_code_index(True)

    def tearDown(self) -> None:
        self._conn.close()
        access_code_index._schema_ready = False
        access_code_index._index_ready = False
        configure_access_code_index(True)

    # -- fixture helpers ----------------------------------------------------

    def _set_join_rules(self, room_id: str, content: dict) -> None:
        """Write a room's current join-rules state, as Synapse would store it."""
        event_id = f"${room_id}:join_rules"
        cur = self._conn.cursor()
        cur.execute(
            "DELETE FROM current_state_events WHERE room_id = ? AND type = ?",
            (room_id, JOIN_RULES),
        )
        cur.execute("DELETE FROM event_json WHERE event_id = ?", (event_id,))
        cur.execute("DELETE FROM events WHERE event_id = ?", (event_id,))
        cur.execute(
            "INSERT INTO current_state_events (room_id, type, state_key, event_id) "
            "VALUES (?, ?, '', ?)",
            (room_id, JOIN_RULES, event_id),
        )
        cur.execute("INSERT INTO events (event_id) VALUES (?)", (event_id,))
        cur.execute(
            "INSERT INTO event_json (event_id, json) VALUES (?, ?)",
            (event_id, json.dumps({"type": JOIN_RULES, "content": content})),
        )
        self._conn.commit()
        cur.close()

    def _purge_room(self, room_id: str) -> None:
        cur = self._conn.cursor()
        cur.execute("DELETE FROM current_state_events WHERE room_id = ?", (room_id,))
        self._conn.commit()
        cur.close()

    async def _index_room(self, room_id: str, content: dict) -> None:
        """Drive the hook, exactly as the on_new_event callback does."""
        await record_join_rules_event(
            self.store, room_id=room_id, state_key="", content=content
        )

    async def _open_the_index(self) -> None:
        await ensure_schema(self.store)

        def _mark(txn: Any) -> None:
            txn.execute(
                f"""
                INSERT INTO {LEASE_TABLE}
                    (lease_key, claimed_by, heartbeat_ms, completed_at_ms)
                VALUES (?, 'test', 0, 1)
                ON CONFLICT (lease_key) DO UPDATE SET completed_at_ms = 1
                """,
                (LEASE_KEY,),
            )

        await self.store.db_pool.runInteraction("mark_complete", _mark)

    async def _resolve(self, code: str) -> List[RoomCodeMatch]:
        return sorted(await get_rooms_with_access_code(code, self.store))

    async def _set_and_index(
        self,
        room_id: str,
        content: dict,
        index_content: Optional[dict] = None,
    ) -> None:
        """Put a room in both the real state and the index.

        ``index_content`` differing from ``content`` is how the tests build a
        deliberately stale index row.
        """
        self._set_join_rules(room_id, content)
        await self._index_room(room_id, index_content or content)

    # -- the two paths agree ------------------------------------------------

    async def test_scan_path_resolves_join_and_admin_codes(self) -> None:
        self._set_join_rules(
            "!a:test", {"join_rule": "knock", "access_code": "join123"}
        )
        self._set_join_rules(
            "!b:test", {"join_rule": "knock", "admin_access_code": "admin12"}
        )

        self.assertEqual(
            await self._resolve("JOIN123"), [RoomCodeMatch("!a:test", False)]
        )
        self.assertEqual(
            await self._resolve("AdMiN12"), [RoomCodeMatch("!b:test", True)]
        )
        self.assertEqual(await self._resolve("nobody1"), [])

    async def test_indexed_path_matches_the_scan(self) -> None:
        await self._set_and_index(
            "!a:test", {"join_rule": "knock", "access_code": "join123"}
        )
        await self._set_and_index(
            "!b:test", {"join_rule": "knock", "admin_access_code": "admin12"}
        )

        from_scan = {
            code: await self._resolve(code)
            for code in ("JOIN123", "AdMiN12", "nobody1")
        }

        await self._open_the_index()
        from_index = {
            code: await self._resolve(code)
            for code in ("JOIN123", "AdMiN12", "nobody1")
        }

        self.assertEqual(from_index, from_scan)
        self.assertEqual(from_index["JOIN123"], [RoomCodeMatch("!a:test", False)])
        self.assertEqual(from_index["AdMiN12"], [RoomCodeMatch("!b:test", True)])
        self.assertEqual(from_index["nobody1"], [])

    async def test_admin_code_wins_when_a_room_uses_one_code_for_both(self) -> None:
        # Matches the old CASE expression: admin is checked first.
        await self._set_and_index(
            "!a:test",
            {"access_code": "same123", "admin_access_code": "same123"},
        )
        self.assertEqual(
            await self._resolve("same123"), [RoomCodeMatch("!a:test", True)]
        )

        await self._open_the_index()
        self.assertEqual(
            await self._resolve("same123"), [RoomCodeMatch("!a:test", True)]
        )

    async def test_one_code_shared_by_two_rooms_returns_both(self) -> None:
        await self._set_and_index("!a:test", {"access_code": "share12"})
        await self._set_and_index("!b:test", {"access_code": "share12"})
        await self._open_the_index()

        self.assertEqual(
            await self._resolve("share12"),
            [RoomCodeMatch("!a:test", False), RoomCodeMatch("!b:test", False)],
        )

    # -- a stale index row is harmless --------------------------------------

    async def test_a_room_whose_code_changed_is_not_returned_for_the_old_one(
        self,
    ) -> None:
        await self._set_and_index(
            "!a:test",
            {"access_code": "newcd12"},
            index_content={"access_code": "oldcd12"},
        )
        await self._open_the_index()

        self.assertEqual(await self._resolve("oldcd12"), [])

    async def test_a_burned_admin_code_left_in_the_index_is_not_returned(self) -> None:
        # burn_admin_code rewrites join rules; if that write never reached the
        # hook, the index still lists the burned code.
        await self._set_and_index(
            "!a:test",
            {"access_code": "join123"},
            index_content={"access_code": "join123", "admin_access_code": "admin12"},
        )
        await self._open_the_index()

        self.assertEqual(await self._resolve("admin12"), [])
        self.assertEqual(
            await self._resolve("join123"), [RoomCodeMatch("!a:test", False)]
        )

    async def test_a_purged_room_left_in_the_index_is_not_returned(self) -> None:
        await self._set_and_index("!a:test", {"access_code": "gone123"})
        await self._open_the_index()
        self._purge_room("!a:test")

        self.assertEqual(await self._resolve("gone123"), [])

    # -- kill switch --------------------------------------------------------

    async def test_a_broken_index_falls_back_to_the_scan(self) -> None:
        """A failing index must cost latency, not a failed code entry."""
        await self._set_and_index("!a:test", {"access_code": "join123"})
        await self._open_the_index()

        self._conn.execute(f"DROP TABLE {access_code_index.INDEX_TABLE}")
        self._conn.commit()

        self.assertEqual(
            await self._resolve("join123"), [RoomCodeMatch("!a:test", False)]
        )

    async def test_disabling_the_index_falls_back_to_the_scan(self) -> None:
        self._set_join_rules("!a:test", {"access_code": "join123"})
        # Nothing in the index at all, and it is switched off.
        configure_access_code_index(False)

        self.assertEqual(
            await self._resolve("join123"), [RoomCodeMatch("!a:test", False)]
        )


if __name__ == "__main__":
    unittest.main()
