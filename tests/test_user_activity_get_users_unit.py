from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

from synapse_pangea_chat.user_activity.get_users import get_users

BOT_USER_ID = "@bot:my.domain.name"
NOTIFIED_ROOM_ID = "!notified:my.domain.name"
QUIET_ROOM_ID = "!quiet:my.domain.name"


class _FakeDbPool:
    """Records every query issued so tests can assert on query *count*.

    The cooldown filter previously ran one account-data lookup plus one events
    query per candidate user. These tests exist to keep that from coming back:
    the number of queries must not grow with the number of users.
    """

    def __init__(self, responses: Dict[str, List[Tuple[Any, ...]]]) -> None:
        self.calls: List[Tuple[str, str, Tuple[Any, ...]]] = []
        self._responses = responses

    async def execute(self, desc: str, query: str, *args: Any) -> List[Tuple[Any, ...]]:
        self.calls.append((desc, query, args))
        return self._responses.get(desc, [])

    def descs(self) -> List[str]:
        return [desc for desc, _query, _args in self.calls]

    def call_for(self, desc: str) -> Tuple[str, str, Tuple[Any, ...]]:
        for call in self.calls:
            if call[0] == desc:
                return call
        raise AssertionError(f"no query issued with desc={desc!r}")


class _FakeRoomStore:
    def __init__(self, responses: Dict[str, List[Tuple[Any, ...]]]) -> None:
        self.db_pool = _FakeDbPool(responses)


def _direct_account_data_rows(user_count: int) -> List[Tuple[Any, ...]]:
    """One m.direct row per user; the first user is in the notified room."""
    rows: List[Tuple[Any, ...]] = [
        (
            "@notified:my.domain.name",
            json.dumps({BOT_USER_ID: [NOTIFIED_ROOM_ID]}),
        )
    ]
    for index in range(user_count - 1):
        rows.append(
            (
                f"@quiet{index}:my.domain.name",
                json.dumps({BOT_USER_ID: [f"{QUIET_ROOM_ID}{index}"]}),
            )
        )
    return rows


def _responses(user_count: int) -> Dict[str, List[Tuple[Any, ...]]]:
    return {
        "get_users_recent_bot_notice_rooms": [(NOTIFIED_ROOM_ID,)],
        "get_users_direct_account_data": _direct_account_data_rows(user_count),
        "get_users_count": [(1,)],
        "get_users_page": [("@quiet0:my.domain.name", "Quiet", 5)],
        "get_users_last_message": [],
    }


async def _run_with_cooldown(store: _FakeRoomStore) -> Dict[str, Any]:
    return await get_users(
        store,  # type: ignore[arg-type]
        notification_cooldown_ms=60_000,
        bot_user_id=BOT_USER_ID,
        api=MagicMock(),
    )


class TestGetUsersCooldownQueryCount(unittest.IsolatedAsyncioTestCase):
    async def test_query_count_is_independent_of_user_count(self):
        """The cooldown filter must not issue per-user queries."""
        small = _FakeRoomStore(_responses(3))
        large = _FakeRoomStore(_responses(2000))

        await _run_with_cooldown(small)
        await _run_with_cooldown(large)

        self.assertEqual(small.db_pool.descs(), large.db_pool.descs())
        # 2 cooldown queries + count + page + page-scoped last-message lookup
        self.assertEqual(len(large.db_pool.calls), 5)

    async def test_notice_lookup_is_scoped_by_sender(self):
        """The events query must be sender-scoped, never a full-table scan."""
        store = _FakeRoomStore(_responses(3))

        await _run_with_cooldown(store)

        _desc, query, args = store.db_pool.call_for("get_users_recent_bot_notice_rooms")
        self.assertIn("sender = ?", query)
        self.assertIn("origin_server_ts > ?", query)
        self.assertNotIn("GROUP BY", query)
        self.assertEqual(args[0], BOT_USER_ID)

    async def test_recently_notified_user_excluded_in_sql(self):
        """Exclusions are applied as a SQL predicate, not by slicing in Python."""
        store = _FakeRoomStore(_responses(3))

        await _run_with_cooldown(store)

        _desc, query, args = store.db_pool.call_for("get_users_count")
        self.assertIn("u.name NOT IN", query)
        self.assertIn("@notified:my.domain.name", args)
        self.assertNotIn("@quiet0:my.domain.name", args)

    async def test_page_query_is_limited(self):
        """Pagination stays in SQL so a page never fetches every candidate."""
        store = _FakeRoomStore(_responses(3))

        await _run_with_cooldown(store)

        _desc, query, _args = store.db_pool.call_for("get_users_page")
        self.assertIn("LIMIT ? OFFSET ?", query)

    async def test_no_recent_notices_skips_account_data_read(self):
        """An empty notice result makes the account-data query unnecessary."""
        responses = _responses(3)
        responses["get_users_recent_bot_notice_rooms"] = []
        store = _FakeRoomStore(responses)

        await _run_with_cooldown(store)

        self.assertNotIn("get_users_direct_account_data", store.db_pool.descs())

    async def test_without_cooldown_no_notification_queries(self):
        """Callers that omit the cooldown pay for none of its lookups."""
        store = _FakeRoomStore(_responses(3))

        await get_users(store)  # type: ignore[arg-type]

        descs = store.db_pool.descs()
        self.assertNotIn("get_users_recent_bot_notice_rooms", descs)
        self.assertNotIn("get_users_direct_account_data", descs)

    async def test_cooldown_without_bot_user_id_excludes_nobody(self):
        """Missing bot config must not silently filter users out."""
        store = _FakeRoomStore(_responses(3))

        await get_users(
            store,  # type: ignore[arg-type]
            notification_cooldown_ms=60_000,
            bot_user_id=None,
            api=MagicMock(),
        )

        _desc, query, _args = store.db_pool.call_for("get_users_count")
        self.assertNotIn("NOT IN", query)

    async def test_unparsable_account_data_does_not_exclude_user(self):
        """A user whose m.direct cannot be read is treated as not notified."""
        responses = _responses(3)
        responses["get_users_direct_account_data"] = [
            ("@broken:my.domain.name", "{not valid json"),
            ("@notified:my.domain.name", json.dumps({BOT_USER_ID: [NOTIFIED_ROOM_ID]})),
        ]
        store = _FakeRoomStore(responses)

        await _run_with_cooldown(store)

        _desc, _query, args = store.db_pool.call_for("get_users_count")
        self.assertIn("@notified:my.domain.name", args)
        self.assertNotIn("@broken:my.domain.name", args)


if __name__ == "__main__":
    unittest.main()
