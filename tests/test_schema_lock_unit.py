"""The module's table creation takes one advisory lock first on PostgreSQL, so
two Synapse workers creating the same tables run one after the other instead
of deadlocking (``synapse_pangea_chat.schema_lock``)."""

import asyncio
import re
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, List, Optional
from unittest.mock import MagicMock

import psycopg2
import testing.postgresql
from synapse.storage.engines import PostgresEngine, Sqlite3Engine

from synapse_pangea_chat.email_invite.course_invitations import CourseInvitationStore
from synapse_pangea_chat.email_invite.student_invitations import (
    StudentInvitationStore as EmailStudentInvitationStore,
)
from synapse_pangea_chat.lti.link_store import LtiLinkStore
from synapse_pangea_chat.lti.store import LtiStore
from synapse_pangea_chat.schema_lock import SCHEMA_LOCK_KEY, lock_schema
from synapse_pangea_chat.student_invitations.store import StudentInvitationStore
from synapse_pangea_chat.user_activity.activity_cache import UserActivityCache

LOCK_SQL = f"SELECT pg_advisory_xact_lock({SCHEMA_LOCK_KEY})"


class _Stop(Exception):
    pass


class _RecordingTxn:
    """Records statements; stops the transaction after ``stop_after`` of them."""

    def __init__(self, engine: Any, stop_after: Optional[int] = None) -> None:
        self.database_engine = engine
        self.statements: List[str] = []
        self._stop_after = stop_after

    def execute(self, sql: str, args: Any = ()) -> None:
        self.statements.append(sql)
        if self._stop_after is not None and len(self.statements) >= self._stop_after:
            raise _Stop()


def _homeserver(pool: Any) -> Any:
    main = SimpleNamespace(db_pool=pool)
    return SimpleNamespace(get_datastores=lambda: SimpleNamespace(main=main))


def _stores(pool: Any) -> List[Any]:
    hs = _homeserver(pool)
    return [
        StudentInvitationStore(hs),
        EmailStudentInvitationStore(hs),
        CourseInvitationStore(hs),
        LtiLinkStore(pool),
        LtiStore(pool),
        UserActivityCache(pool),
    ]


class TestLockSchema(unittest.TestCase):
    def test_postgres_takes_the_lock(self) -> None:
        txn = _RecordingTxn(MagicMock(spec=PostgresEngine))
        lock_schema(txn)
        self.assertEqual(txn.statements, [LOCK_SQL])

    def test_sqlite_and_unknown_engines_take_nothing(self) -> None:
        for engine in (MagicMock(spec=Sqlite3Engine), None):
            txn = _RecordingTxn(engine)
            lock_schema(txn)
            self.assertEqual(txn.statements, [])


class TestEveryStoreLocksFirst(unittest.IsolatedAsyncioTestCase):
    async def test_the_lock_is_the_first_statement_of_each_schema_transaction(
        self,
    ) -> None:
        seen: List[str] = []

        class Pool:
            async def runInteraction(
                self, desc: str, func: Callable[..., Any], *a: Any, **kw: Any
            ) -> Any:
                txn = _RecordingTxn(MagicMock(spec=PostgresEngine), stop_after=1)
                try:
                    func(txn, *a, **kw)
                except _Stop:
                    pass
                seen.append(desc)
                self_test.assertEqual(txn.statements, [LOCK_SQL], desc)

        self_test = self
        for store in _stores(Pool()):
            await store.ensure()
        self.assertEqual(len(seen), 6)


class TestNoUnlockedSchema(unittest.TestCase):
    def test_every_module_that_creates_tables_takes_a_lock(self) -> None:
        """A new store that creates tables without the lock fails here."""
        root = Path(__file__).resolve().parent.parent / "synapse_pangea_chat"
        unlocked = [
            str(path.relative_to(root))
            for path in sorted(root.rglob("*.py"))
            if re.search(
                r"CREATE (UNIQUE )?(TABLE|INDEX) IF NOT EXISTS",
                text := path.read_text(),
            )
            and "lock_schema(txn)" not in text
            and "pg_advisory_xact_lock" not in text
            # Statement lists only; their store's transaction takes the lock.
            and path.name not in ("events.py", "managed_events.py")
        ]
        self.assertEqual(unlocked, [])


class _PostgresPool:
    """``db_pool.runInteraction`` over a real PostgreSQL, one connection per
    transaction (as two Synapse workers would have), run off the event loop
    so a transaction waiting on a lock does not block the test."""

    def __init__(self, url: str) -> None:
        self.url = url

    async def runInteraction(
        self, desc: str, func: Callable[..., Any], *a: Any, **kw: Any
    ) -> Any:
        return await asyncio.to_thread(self._run, func, *a, **kw)

    def _run(self, func: Callable[..., Any], *a: Any, **kw: Any) -> Any:
        conn = psycopg2.connect(self.url)
        try:
            cur = conn.cursor()
            engine = MagicMock(spec=PostgresEngine)

            class Txn:
                database_engine = engine

                def execute(self, sql: str, args: Any = ()) -> None:
                    cur.execute(sql.replace("?", "%s"), args or None)

                def fetchall(self) -> Any:
                    return cur.fetchall()

                def fetchone(self) -> Any:
                    return cur.fetchone()

            result = func(Txn(), *a, **kw)
            conn.commit()
            return result
        finally:
            conn.close()


class TestOnPostgres(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pg = testing.postgresql.Postgresql()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.pg.stop()

    async def test_schema_creation_waits_for_the_lock_holder(self) -> None:
        url = self.pg.url()
        holder = psycopg2.connect(url)
        try:
            holder.cursor().execute(LOCK_SQL)  # held until commit
            store = StudentInvitationStore(_homeserver(_PostgresPool(url)))
            task = asyncio.ensure_future(store.ensure())
            await asyncio.sleep(0.5)
            self.assertFalse(task.done(), "created the schema without the lock")
            holder.commit()
            await asyncio.wait_for(task, 10)
        finally:
            holder.close()
        conn = psycopg2.connect(url)
        try:
            cur = conn.cursor()
            cur.execute("SELECT to_regclass('pangea_seat_invitation')")
            self.assertIsNotNone(cur.fetchone()[0])
        finally:
            conn.close()

    async def test_many_workers_creating_every_schema_at_once_all_succeed(
        self,
    ) -> None:
        # A fresh database, so every worker races the first creation. The
        # deadlock is timing-dependent and this does not reliably reproduce it
        # without the lock; it checks that serialised creation still lets every
        # worker through. The ordering is proven by the tests above.
        admin = psycopg2.connect(self.pg.url())
        admin.autocommit = True
        admin.cursor().execute("CREATE DATABASE race")
        admin.close()
        url = self.pg.url().rsplit("/", 1)[0] + "/race"
        barrier = threading.Barrier(6)
        errors: List[BaseException] = []

        async def worker() -> None:
            await asyncio.to_thread(barrier.wait)
            for store in _stores(_PostgresPool(url)):
                try:
                    await store.ensure()
                except BaseException as e:  # noqa: BLE001 - collected and asserted
                    errors.append(e)

        start = time.monotonic()
        await asyncio.wait_for(asyncio.gather(*(worker() for _ in range(6))), 60)
        self.assertEqual(errors, [], f"after {time.monotonic() - start:.1f}s")


if __name__ == "__main__":
    unittest.main()
