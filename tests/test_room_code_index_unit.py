"""Unit tests for the room access-code index (issue #163).

The SQL is exercised against a real in-memory SQLite database through a minimal
stand-in for Synapse's ``db_pool``. That is deliberate: the interesting parts of
this feature *are* the statements — the conflict clauses in particular — and a
mock that only records calls would assert nothing about them.

Placeholders are written ``?`` throughout the module because Synapse's
``LoggingTransaction`` rewrites them to ``%s`` for PostgreSQL
(``PostgresEngine.convert_param_style``), so the same statements run unmodified
here and in production.
"""

import sqlite3
import unittest
from typing import Any, List, Tuple

from synapse_pangea_chat.room_code import access_code_index
from synapse_pangea_chat.room_code.access_code_index import (
    INDEX_TABLE,
    LEASE_KEY,
    LEASE_TABLE,
    RoomCodes,
    code_text,
    configure_access_code_index,
    ensure_schema,
    extract_codes,
    index_is_ready,
    insert_missing_rows_txn,
    lookup_candidate_room_ids,
    record_join_rules_event,
    upsert_row_txn,
)


class FakeDbPool:
    """The slice of Synapse's DatabasePool this feature actually uses."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

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


class RoomCodeIndexTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self.store = FakeStore(self._conn)
        # The module memoizes both latches per process; every test starts from
        # a fresh database, so they have to be cleared alongside it.
        access_code_index._schema_ready = False
        access_code_index._index_ready = False
        configure_access_code_index(True)

    def tearDown(self) -> None:
        self._conn.close()
        access_code_index._schema_ready = False
        access_code_index._index_ready = False
        configure_access_code_index(True)

    # -- helpers ------------------------------------------------------------

    async def _mark_backfill_complete(self) -> None:
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

    def _rows(self) -> List[Tuple[Any, ...]]:
        cur = self._conn.cursor()
        cur.execute(
            f"SELECT room_id, state_key, access_code_lower, admin_access_code_lower "
            f"FROM {INDEX_TABLE} ORDER BY room_id, state_key"
        )
        rows = cur.fetchall()
        cur.close()
        return rows

    # -- code extraction ----------------------------------------------------

    def test_code_text_matches_postgres_text_coercion(self) -> None:
        # ->> renders a JSON string as itself and a JSON number as its text.
        self.assertEqual(code_text("abc123d"), "abc123d")
        self.assertEqual(code_text(1234567), "1234567")
        # Booleans and containers are not codes, whatever ->> would print.
        self.assertIsNone(code_text(True))
        self.assertIsNone(code_text(None))
        self.assertIsNone(code_text({"a": 1}))
        self.assertIsNone(code_text(["a"]))

    def test_extract_codes_lowercases_both_fields(self) -> None:
        codes = extract_codes(
            {
                "join_rule": "knock",
                "access_code": "AbC123d",
                "admin_access_code": "ZZZ999x",
            }
        )
        self.assertEqual(codes.access_code_lower, "abc123d")
        self.assertEqual(codes.admin_access_code_lower, "zzz999x")
        self.assertFalse(codes.is_empty)

    def test_extract_codes_of_codeless_join_rules(self) -> None:
        codes = extract_codes({"join_rule": "public"})
        self.assertTrue(codes.is_empty)
        self.assertTrue(extract_codes(None).is_empty)
        self.assertTrue(extract_codes("nonsense").is_empty)

    # -- readiness gate -----------------------------------------------------

    async def test_index_is_not_ready_until_backfill_marks_completion(self) -> None:
        self.assertFalse(await index_is_ready(self.store))
        await self._mark_backfill_complete()
        self.assertTrue(await index_is_ready(self.store))

    async def test_index_is_never_ready_while_disabled(self) -> None:
        await ensure_schema(self.store)
        await self._mark_backfill_complete()
        configure_access_code_index(False)
        self.assertFalse(await index_is_ready(self.store))

    # -- hook maintenance ---------------------------------------------------

    async def test_hook_records_and_then_replaces_a_rooms_codes(self) -> None:
        await record_join_rules_event(
            self.store,
            room_id="!a:test",
            state_key="",
            content={"join_rule": "knock", "access_code": "OldCd1"},
        )
        self.assertEqual(self._rows(), [("!a:test", "", "oldcd1", None)])

        await record_join_rules_event(
            self.store,
            room_id="!a:test",
            state_key="",
            content={
                "join_rule": "knock",
                "access_code": "NewCd2",
                "admin_access_code": "Adm1nCd",
            },
        )
        self.assertEqual(self._rows(), [("!a:test", "", "newcd2", "adm1ncd")])

    async def test_hook_drops_the_row_when_the_last_code_goes_away(self) -> None:
        await record_join_rules_event(
            self.store,
            room_id="!a:test",
            state_key="",
            content={"access_code": "code123"},
        )
        self.assertEqual(len(self._rows()), 1)

        await record_join_rules_event(
            self.store,
            room_id="!a:test",
            state_key="",
            content={"join_rule": "invite"},
        )
        self.assertEqual(self._rows(), [])

    async def test_hook_burning_an_admin_code_keeps_the_join_code(self) -> None:
        await record_join_rules_event(
            self.store,
            room_id="!a:test",
            state_key="",
            content={"access_code": "join123", "admin_access_code": "admin12"},
        )
        await record_join_rules_event(
            self.store,
            room_id="!a:test",
            state_key="",
            content={"access_code": "join123"},
        )
        self.assertEqual(self._rows(), [("!a:test", "", "join123", None)])

    async def test_hook_writes_nothing_while_disabled(self) -> None:
        await ensure_schema(self.store)
        configure_access_code_index(False)
        await record_join_rules_event(
            self.store,
            room_id="!a:test",
            state_key="",
            content={"access_code": "code123"},
        )
        self.assertEqual(self._rows(), [])

    # -- backfill semantics -------------------------------------------------

    async def test_backfill_does_not_clobber_a_row_the_hook_already_wrote(self) -> None:
        """The property that makes the backfill safe to run under live traffic.

        The hook always holds newer state than a scan that is still in flight,
        so a row already present must win.
        """
        await record_join_rules_event(
            self.store,
            room_id="!a:test",
            state_key="",
            content={"access_code": "fresh12"},
        )

        await self.store.db_pool.runInteraction(
            "backfill",
            insert_missing_rows_txn,
            [
                ("!a:test", "", RoomCodes("stale12", None)),
                ("!b:test", "", RoomCodes("other34", None)),
            ],
        )

        self.assertEqual(
            self._rows(),
            [
                ("!a:test", "", "fresh12", None),
                ("!b:test", "", "other34", None),
            ],
        )

    async def test_backfill_skips_rooms_without_codes(self) -> None:
        await ensure_schema(self.store)
        await self.store.db_pool.runInteraction(
            "backfill",
            insert_missing_rows_txn,
            [
                ("!a:test", "", RoomCodes(None, None)),
                ("!b:test", "", RoomCodes(None, "admin12")),
            ],
        )
        self.assertEqual(self._rows(), [("!b:test", "", None, "admin12")])

    # -- lookup -------------------------------------------------------------

    async def test_lookup_finds_join_and_admin_codes_case_insensitively(self) -> None:
        await record_join_rules_event(
            self.store,
            room_id="!join:test",
            state_key="",
            content={"access_code": "join123"},
        )
        await record_join_rules_event(
            self.store,
            room_id="!admin:test",
            state_key="",
            content={"admin_access_code": "admin12"},
        )

        self.assertEqual(
            await lookup_candidate_room_ids(self.store, "JOIN123"), ["!join:test"]
        )
        self.assertEqual(
            await lookup_candidate_room_ids(self.store, "AdMiN12"), ["!admin:test"]
        )
        self.assertEqual(await lookup_candidate_room_ids(self.store, "nobody1"), [])

    async def test_lookup_reports_a_room_once_when_both_codes_match(self) -> None:
        await record_join_rules_event(
            self.store,
            room_id="!a:test",
            state_key="",
            content={"access_code": "same123", "admin_access_code": "same123"},
        )
        self.assertEqual(
            await lookup_candidate_room_ids(self.store, "same123"), ["!a:test"]
        )

    async def test_rooms_are_indexed_per_state_key(self) -> None:
        await record_join_rules_event(
            self.store,
            room_id="!a:test",
            state_key="",
            content={"access_code": "first12"},
        )
        await record_join_rules_event(
            self.store,
            room_id="!a:test",
            state_key="odd",
            content={"access_code": "second3"},
        )
        self.assertEqual(
            self._rows(),
            [
                ("!a:test", "", "first12", None),
                ("!a:test", "odd", "second3", None),
            ],
        )

    # -- schema -------------------------------------------------------------

    async def test_ensure_schema_is_idempotent(self) -> None:
        await ensure_schema(self.store)
        access_code_index._schema_ready = False
        await ensure_schema(self.store)

        cur = self._conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        tables = [row[0] for row in cur.fetchall()]
        cur.close()
        self.assertIn(INDEX_TABLE, tables)
        self.assertIn(LEASE_TABLE, tables)

    async def test_upsert_row_txn_is_a_no_op_delete_for_an_unknown_room(self) -> None:
        await ensure_schema(self.store)
        await self.store.db_pool.runInteraction(
            "delete_unknown",
            upsert_row_txn,
            "!never-seen:test",
            "",
            RoomCodes(None, None),
        )
        self.assertEqual(self._rows(), [])


if __name__ == "__main__":
    unittest.main()
