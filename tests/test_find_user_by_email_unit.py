from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.find_user_by_email.find_user_by_email import (
    MAX_EMAIL_ADDRESS_LENGTH,
    normalise_address,
)
from synapse_pangea_chat.find_user_by_email.is_rate_limited import (
    is_rate_limited,
    request_log,
)
from synapse_pangea_chat.find_user_by_email.lookup import (
    EMAIL_MEDIUM,
    MAX_RESULTS,
    find_users_by_email_db,
)


class TestNormaliseAddress(unittest.TestCase):
    def test_accepts_plain_address(self):
        self.assertEqual(
            normalise_address("person@school.edu"),
            "person@school.edu",
        )

    def test_trims_surrounding_whitespace(self):
        self.assertEqual(
            normalise_address("  person@school.edu\n"),
            "person@school.edu",
        )

    def test_preserves_casing(self):
        # The lookup compares case-insensitively; echoing the caller's spelling
        # back lets them see exactly what was asked for.
        self.assertEqual(
            normalise_address("Person@School.edu"),
            "Person@School.edu",
        )

    def test_rejects_non_string(self):
        for value in (None, 42, ["a@b.com"], {"address": "a@b.com"}):
            with self.subTest(value=value):
                self.assertIsNone(normalise_address(value))

    def test_rejects_empty_or_whitespace(self):
        for value in ("", "   ", "\t\n"):
            with self.subTest(value=value):
                self.assertIsNone(normalise_address(value))

    def test_rejects_addresses_without_exactly_one_at(self):
        for value in (
            "person",
            "person@@school.edu",
            "a@b@c",
            "@school.edu",
            "person@",
        ):
            with self.subTest(value=value):
                self.assertIsNone(normalise_address(value))

    def test_rejects_overlong_address(self):
        too_long = "a" * MAX_EMAIL_ADDRESS_LENGTH + "@school.edu"
        self.assertIsNone(normalise_address(too_long))


class TestFindUsersByEmailDb(unittest.IsolatedAsyncioTestCase):
    async def test_maps_rows_to_result_entries(self):
        db_pool = AsyncMock()
        db_pool.execute.return_value = [
            ("@jdoe:my.domain.name", "Person@School.edu", "Jane Doe", 0),
            ("@jdoe2:my.domain.name", "person@school.edu", None, 1),
        ]

        results = await find_users_by_email_db(db_pool, "person@school.edu")

        self.assertEqual(
            results,
            [
                {
                    "user_id": "@jdoe:my.domain.name",
                    "address": "Person@School.edu",
                    "display_name": "Jane Doe",
                    "deactivated": False,
                },
                {
                    "user_id": "@jdoe2:my.domain.name",
                    "address": "person@school.edu",
                    "display_name": None,
                    "deactivated": True,
                },
            ],
        )

    async def test_no_rows_yields_empty_list(self):
        db_pool = AsyncMock()
        db_pool.execute.return_value = []

        self.assertEqual(await find_users_by_email_db(db_pool, "nobody@school.edu"), [])

    async def test_queries_email_medium_with_result_cap(self):
        db_pool = AsyncMock()
        db_pool.execute.return_value = []

        await find_users_by_email_db(db_pool, "person@school.edu")

        args = db_pool.execute.await_args.args
        self.assertEqual(args[2], EMAIL_MEDIUM)
        self.assertEqual(args[3], "person@school.edu")
        self.assertEqual(args[4], MAX_RESULTS)


class TestIsRateLimited(unittest.TestCase):
    def setUp(self) -> None:
        request_log.clear()
        self.addCleanup(request_log.clear)

    def test_allows_up_to_the_burst_then_blocks(self):
        config = PangeaChatConfig(
            find_user_by_email_requests_per_burst=3,
            find_user_by_email_burst_duration_seconds=60,
        )
        caller = "@admin:my.domain.name"

        for _ in range(3):
            self.assertFalse(is_rate_limited(caller, config))
        self.assertTrue(is_rate_limited(caller, config))

    def test_limit_is_per_caller(self):
        config = PangeaChatConfig(
            find_user_by_email_requests_per_burst=1,
            find_user_by_email_burst_duration_seconds=60,
        )

        self.assertFalse(is_rate_limited("@admin_a:my.domain.name", config))
        self.assertFalse(is_rate_limited("@admin_b:my.domain.name", config))
        self.assertTrue(is_rate_limited("@admin_a:my.domain.name", config))


if __name__ == "__main__":
    unittest.main()
