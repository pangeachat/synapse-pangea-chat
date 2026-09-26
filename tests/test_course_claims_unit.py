"""The claim store's statements, pinned to the table they name."""

import re
import unittest

from synapse_pangea_chat.email_invite.course_claims import (
    COURSE_CLAIM_CODE_TABLE,
    COURSE_CLAIM_TABLE,
    STATEMENTS,
)


class TestStatements(unittest.TestCase):
    def test_every_statement_names_the_table(self) -> None:
        for sql in STATEMENTS:
            with self.subTest(sql=sql.split()[0]):
                self.assertRegex(
                    sql,
                    rf"\b({re.escape(COURSE_CLAIM_TABLE)}|{re.escape(COURSE_CLAIM_CODE_TABLE)})\b",
                )

    def test_a_claim_only_takes_an_open_or_own_row(self) -> None:
        from synapse_pangea_chat.email_invite import course_claims

        self.assertIn("claimed_by IS NULL OR claimed_by = ?", course_claims._CLAIM_SQL)

    def test_sending_the_notice_clears_the_address(self) -> None:
        from synapse_pangea_chat.email_invite import course_claims

        self.assertIn("requested_email = NULL", course_claims._NOTICE_SENT_SQL)


if __name__ == "__main__":
    unittest.main()


class TestStoreAgainstADatabase(unittest.IsolatedAsyncioTestCase):
    """The statements run against a real SQL engine (SQLite, through the
    moderation tests' pool double), because the lease and the attempt count
    live in their WHERE clauses."""

    ROOM = "!course:x"
    CODE = "adm1nab"
    TEACHER = "@teacher:x"
    LEASE = 600_000

    async def asyncSetUp(self) -> None:
        from unittest.mock import MagicMock

        from synapse_pangea_chat.email_invite.course_claims import CourseClaimStore
        from tests.moderation_doubles import DbPoolDouble

        hs = MagicMock()
        hs.get_datastores.return_value.main.db_pool = DbPoolDouble()
        self.store = CourseClaimStore(hs)
        await self.store.record(self.ROOM, "t@school.example", self.CODE, 0)

    async def _claim_and_promote(self, user: str = TEACHER) -> None:
        self.assertTrue(await self.store.claim(self.ROOM, user, 1))
        await self.store.mark_promoted(self.ROOM, user, 2)

    async def test_a_reminder_code_opens_the_same_course_alongside_the_first(
        self,
    ) -> None:
        await self.store.add_code(self.ROOM, "r3mind1", 5)

        self.assertEqual(await self.store.rooms_for_admin_code(self.CODE), [self.ROOM])
        self.assertEqual(await self.store.rooms_for_admin_code("R3MIND1"), [self.ROOM])
        self.assertTrue(await self.store.code_in_use("r3mind1"))

    async def test_the_claim_spends_every_code_the_course_has(self) -> None:
        await self.store.add_code(self.ROOM, "r3mind1", 5)
        await self.store.add_code(self.ROOM, "r3mind2", 6)

        await self._claim_and_promote()

        for code in (self.CODE, "r3mind1", "r3mind2"):
            with self.subTest(code=code):
                self.assertEqual(await self.store.rooms_for_admin_code(code), [])
                self.assertTrue(await self.store.code_in_use(code))

    async def test_a_withdrawn_code_opens_nothing(self) -> None:
        await self.store.add_code(self.ROOM, "r3mind1", 5)
        await self.store.remove_code("r3mind1")

        self.assertEqual(await self.store.rooms_for_admin_code("r3mind1"), [])
        self.assertFalse(await self.store.code_in_use("r3mind1"))
        self.assertEqual(await self.store.rooms_for_admin_code(self.CODE), [self.ROOM])

    async def test_reminder_target_reads_the_address_and_whether_it_is_claimed(
        self,
    ) -> None:
        target = await self.store.reminder_target(self.ROOM)
        assert target is not None
        self.assertEqual(target.requested_email, "t@school.example")
        self.assertFalse(target.claimed)
        self.assertIsNone(await self.store.reminder_target("!other:x"))

        self.assertTrue(await self.store.claim(self.ROOM, self.TEACHER, 1))
        target = await self.store.reminder_target(self.ROOM)
        assert target is not None
        self.assertTrue(target.claimed)

    async def test_the_claim_code_finds_its_room_until_it_is_spent(self) -> None:
        self.assertEqual(await self.store.rooms_for_admin_code("ADM1NAB"), [self.ROOM])
        self.assertEqual(await self.store.rooms_for_admin_code("0ther1c"), [])
        self.assertTrue(await self.store.code_in_use(self.CODE))

        await self._claim_and_promote()

        self.assertEqual(await self.store.rooms_for_admin_code(self.CODE), [])
        # Spent, but still taken: a new course must not reuse it.
        self.assertTrue(await self.store.code_in_use(self.CODE))

    async def test_a_course_with_no_address_owes_no_notice(self) -> None:
        await self.store.record("!quiet:x", None, "qu1etab", 0)
        self.assertTrue(await self.store.claim("!quiet:x", self.TEACHER, 1))
        await self.store.mark_promoted("!quiet:x", self.TEACHER, 2)

        self.assertIsNone(
            await self.store.reserve_notice("!quiet:x", self.TEACHER, 10, self.LEASE)
        )
        self.assertEqual(await self.store.outstanding_notices(10), [])

    async def test_one_account_takes_the_claim_and_may_retake_it(self) -> None:
        self.assertTrue(await self.store.claim(self.ROOM, self.TEACHER, 1))
        self.assertFalse(await self.store.claim(self.ROOM, "@student:x", 2))
        self.assertTrue(await self.store.claim(self.ROOM, self.TEACHER, 3))

    async def test_no_notice_is_owed_before_promotion(self) -> None:
        self.assertTrue(await self.store.claim(self.ROOM, self.TEACHER, 1))

        self.assertIsNone(
            await self.store.reserve_notice(self.ROOM, self.TEACHER, 10, self.LEASE)
        )
        self.assertEqual(await self.store.outstanding_notices(10), [])

    async def test_the_lease_admits_one_sender(self) -> None:
        await self._claim_and_promote()

        first = await self.store.reserve_notice(self.ROOM, self.TEACHER, 10, self.LEASE)
        second = await self.store.reserve_notice(
            self.ROOM, self.TEACHER, 11, self.LEASE
        )

        assert first is not None
        self.assertEqual(first.requested_email, "t@school.example")
        self.assertEqual(first.attempt, 1)
        self.assertIsNone(second)
        # Not handed to the retry while the lease holds.
        self.assertEqual(await self.store.outstanding_notices(11), [])

    async def test_an_expired_lease_is_retried_and_a_sent_notice_is_done(
        self,
    ) -> None:
        await self._claim_and_promote()
        await self.store.reserve_notice(self.ROOM, self.TEACHER, 10, self.LEASE)
        after_lease = 10 + self.LEASE

        self.assertEqual(
            await self.store.outstanding_notices(after_lease),
            [(self.ROOM, self.TEACHER)],
        )
        retry = await self.store.reserve_notice(
            self.ROOM, self.TEACHER, after_lease, self.LEASE
        )
        assert retry is not None
        self.assertEqual(retry.attempt, 2)

        await self.store.mark_notice_sent(self.ROOM, self.TEACHER, after_lease + 1)

        far_later = after_lease + 10 * self.LEASE
        self.assertEqual(await self.store.outstanding_notices(far_later), [])
        self.assertIsNone(
            await self.store.reserve_notice(
                self.ROOM, self.TEACHER, far_later, self.LEASE
            )
        )

    async def test_attempts_run_out(self) -> None:
        from synapse_pangea_chat.email_invite.course_claims import (
            MAX_NOTICE_ATTEMPTS,
        )

        await self._claim_and_promote()
        now = 10
        for _ in range(MAX_NOTICE_ATTEMPTS):
            self.assertIsNotNone(
                await self.store.reserve_notice(self.ROOM, self.TEACHER, now, 1)
            )
            now += 1

        self.assertIsNone(
            await self.store.reserve_notice(self.ROOM, self.TEACHER, now, 1)
        )
        self.assertEqual(await self.store.outstanding_notices(now), [])


class TestBackfillFromTheOneCodeColumn(unittest.IsolatedAsyncioTestCase):
    """Courses created before the code table carry their one code in the claim
    row; the table is filled from it, so their first link keeps working."""

    async def test_a_legacy_course_is_found_by_its_code(self) -> None:
        from unittest.mock import MagicMock

        from synapse_pangea_chat.email_invite.course_claims import (
            CourseClaimStore,
            admin_code_digest,
        )
        from tests.moderation_doubles import DbPoolDouble

        pool = DbPoolDouble()
        # The claim table as it was before the code table existed.
        pool.connection.execute(
            """
            CREATE TABLE pangea_course_claim (
                room_id TEXT PRIMARY KEY, requested_email TEXT,
                admin_code_sha256 TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
                claimed_by TEXT, claimed_at_ms BIGINT, promoted_at_ms BIGINT,
                notice_attempts INTEGER NOT NULL DEFAULT 0,
                notice_leased_until_ms BIGINT, notice_sent_at_ms BIGINT
            )
            """
        )
        pool.connection.execute(
            "INSERT INTO pangea_course_claim (room_id, requested_email, admin_code_sha256, created_at_ms) VALUES (?, ?, ?, ?)",
            ("!legacy:x", "t@school.example", admin_code_digest("l3gacy1"), 0),
        )
        pool.connection.commit()
        hs = MagicMock()
        hs.get_datastores.return_value.main.db_pool = pool
        store = CourseClaimStore(hs)

        self.assertEqual(await store.rooms_for_admin_code("l3gacy1"), ["!legacy:x"])
        self.assertTrue(await store.code_in_use("l3gacy1"))

        # The backfill runs on every start and adds nothing the second time.
        second = CourseClaimStore(hs)
        await second.add_code("!legacy:x", "r3mind1", 5)
        rows = pool.connection.execute(
            "SELECT COUNT(*) FROM pangea_course_claim_code WHERE room_id = ?",
            ("!legacy:x",),
        ).fetchone()
        self.assertEqual(rows[0], 2)
