from __future__ import annotations

import json
import unittest
from typing import Any, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.activity_session_previews.get_activity_session_previews import (
    _activity_session_ids,
    _child_room_ids,
    _has_valid_via,
    _member_space_ids,
    get_activity_session_previews,
)

USER = "@learner:my.domain.name"
SPACE_A = "!spaceA:my.domain.name"
SPACE_B = "!spaceB:my.domain.name"
SESSION_1 = "!session1:my.domain.name"
SESSION_2 = "!session2:my.domain.name"
CHAT = "!chat:my.domain.name"


def _room_store(
    rows: Optional[List[Tuple[Any, ...]]] = None,
    membership: Any = ("join", "$event"),
) -> Any:
    store = MagicMock()
    engine_module = MagicMock()
    engine_module.__name__ = "sqlite3"
    store.db_pool.engine.module = engine_module
    store.db_pool.execute = AsyncMock(return_value=rows or [])
    store.get_local_current_membership_for_user_in_room = AsyncMock(
        return_value=membership
    )
    return store


def _child_row(space_id: str, child_id: str, content: Any) -> Tuple[str, str, str]:
    return (space_id, child_id, json.dumps({"content": content}))


def _plan_row(room_id: str, content: Any) -> Tuple[str, str]:
    return (room_id, json.dumps({"content": content}))


class TestHasValidVia(unittest.TestCase):
    def test_valid_via_list(self) -> None:
        self.assertTrue(_has_valid_via({"via": ["my.domain.name"]}))

    def test_removed_child_empty_content(self) -> None:
        self.assertFalse(_has_valid_via({}))

    def test_non_dict_content(self) -> None:
        self.assertFalse(_has_valid_via(None))

    def test_empty_via(self) -> None:
        self.assertFalse(_has_valid_via({"via": []}))

    def test_via_not_a_list(self) -> None:
        self.assertFalse(_has_valid_via({"via": "my.domain.name"}))

    def test_via_with_non_string_entry(self) -> None:
        self.assertFalse(_has_valid_via({"via": ["my.domain.name", 42]}))


class TestChildRoomIds(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_valid_children_drops_removed_and_self(self) -> None:
        store = _room_store(
            rows=[
                _child_row(SPACE_A, SESSION_1, {"via": ["my.domain.name"]}),
                # Removed child: empty content must not resurface.
                _child_row(SPACE_A, SESSION_2, {}),
                # A space listing itself is skipped.
                _child_row(SPACE_A, SPACE_A, {"via": ["my.domain.name"]}),
                _child_row(SPACE_A, CHAT, {"via": ["my.domain.name"]}),
            ]
        )
        result = await _child_room_ids([SPACE_A], store)
        self.assertEqual(result, [SESSION_1, CHAT])

    async def test_deduplicates_children_across_spaces(self) -> None:
        store = _room_store(
            rows=[
                _child_row(SPACE_A, SESSION_1, {"via": ["my.domain.name"]}),
                _child_row(SPACE_B, SESSION_1, {"via": ["my.domain.name"]}),
            ]
        )
        result = await _child_room_ids([SPACE_A, SPACE_B], store)
        self.assertEqual(result, [SESSION_1])

    async def test_no_spaces_no_query(self) -> None:
        store = _room_store()
        self.assertEqual(await _child_room_ids([], store), [])
        store.db_pool.execute.assert_not_awaited()


class TestActivitySessionIds(unittest.IsolatedAsyncioTestCase):
    async def test_unscoped_returns_every_room_with_a_plan(self) -> None:
        store = _room_store(
            rows=[
                _plan_row(SESSION_1, {"activity_id": "act-1"}),
                _plan_row(SESSION_2, {"activity_id": "act-2"}),
            ]
        )
        result = await _activity_session_ids([SESSION_1, SESSION_2, CHAT], None, store)
        self.assertEqual(result, [SESSION_1, SESSION_2])

    async def test_scoped_keeps_only_the_matching_activity(self) -> None:
        store = _room_store(
            rows=[
                _plan_row(SESSION_1, {"activity_id": "act-1"}),
                _plan_row(SESSION_2, {"activity_id": "act-2"}),
            ]
        )
        result = await _activity_session_ids([SESSION_1, SESSION_2], "act-2", store)
        self.assertEqual(result, [SESSION_2])

    async def test_scoped_skips_malformed_plan_content(self) -> None:
        store = _room_store(rows=[(SESSION_1, json.dumps({"content": "oops"}))])
        result = await _activity_session_ids([SESSION_1], "act-1", store)
        self.assertEqual(result, [])

    async def test_no_children_no_query(self) -> None:
        store = _room_store()
        self.assertEqual(await _activity_session_ids([], None, store), [])
        store.db_pool.execute.assert_not_awaited()


class TestMemberSpaceIds(unittest.IsolatedAsyncioTestCase):
    async def test_joined_space_kept(self) -> None:
        store = _room_store(membership=("join", "$event"))
        self.assertEqual(await _member_space_ids([SPACE_A], USER, store), [SPACE_A])

    async def test_non_member_space_dropped_silently(self) -> None:
        store = _room_store(membership=(None, None))
        self.assertEqual(await _member_space_ids([SPACE_A], USER, store), [])

    async def test_left_space_dropped(self) -> None:
        store = _room_store(membership=("leave", "$event"))
        self.assertEqual(await _member_space_ids([SPACE_A], USER, store), [])

    async def test_lookup_error_drops_that_space_only(self) -> None:
        store = _room_store()
        store.get_local_current_membership_for_user_in_room = AsyncMock(
            side_effect=[Exception("boom"), ("join", "$event")]
        )
        result = await _member_space_ids([SPACE_A, SPACE_B], USER, store)
        self.assertEqual(result, [SPACE_B])


class TestGetActivitySessionPreviews(unittest.IsolatedAsyncioTestCase):
    async def test_sessions_flow_into_room_preview(self) -> None:
        store = _room_store()
        store.db_pool.execute = AsyncMock(
            side_effect=[
                # children query
                [_child_row(SPACE_A, SESSION_1, {"via": ["my.domain.name"]})],
                # plan query
                [_plan_row(SESSION_1, {"activity_id": "act-1"})],
            ]
        )
        preview: dict[str, Any] = {SESSION_1: {"pangea.activity_plan": {}}}
        with patch(
            "synapse_pangea_chat.activity_session_previews."
            "get_activity_session_previews.get_room_preview",
            new=AsyncMock(return_value=preview),
        ) as mock_preview:
            result = await get_activity_session_previews(
                [SPACE_A], None, USER, MagicMock(), store, MagicMock()
            )
        self.assertEqual(result, preview)
        mock_preview.assert_awaited_once()
        self.assertEqual(mock_preview.await_args_list[0].args[0], [SESSION_1])

    async def test_no_sessions_skips_room_preview(self) -> None:
        store = _room_store(rows=[])
        with patch(
            "synapse_pangea_chat.activity_session_previews."
            "get_activity_session_previews.get_room_preview",
            new=AsyncMock(),
        ) as mock_preview:
            result = await get_activity_session_previews(
                [SPACE_A], None, USER, MagicMock(), store, MagicMock()
            )
        self.assertEqual(result, {})
        mock_preview.assert_not_awaited()
