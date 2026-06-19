from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from synapse_pangea_chat.direct_message.ensure_direct_message import (
    ACTION_CREATED_ROOM,
    ACTION_REPAIRED_ACCOUNT_DATA,
    ACTION_REPAIRED_MEMBERSHIP_OR_POWER,
    ACTION_VALID_EXISTING_NOOP,
    EnsureDirectMessage,
    ExistingDirectRoom,
)

ALICE = "@alice:my.domain.name"
BOB = "@bob:my.domain.name"
ROOM_ID = "!dm:my.domain.name"


def _handler() -> Any:
    handler = object.__new__(EnsureDirectMessage)
    handler._api = MagicMock()
    handler._api.account_data_manager = MagicMock()
    handler._api.account_data_manager.put_global = AsyncMock()
    return handler


def _existing_room(
    *,
    alice_direct: dict[str, Any] | None = None,
    bob_direct: dict[str, Any] | None = None,
) -> ExistingDirectRoom:
    return ExistingDirectRoom(
        room_id=ROOM_ID,
        first_user_direct=alice_direct
        if alice_direct is not None
        else {BOB: [ROOM_ID]},
        second_user_direct=bob_direct if bob_direct is not None else {ALICE: [ROOM_ID]},
    )


class TestEnsureDirectMessageFastPath(unittest.IsolatedAsyncioTestCase):
    async def test_valid_existing_dm_returns_noop_without_repair_calls(self) -> None:
        handler = _handler()
        handler._find_existing_direct_room = AsyncMock(return_value=_existing_room())
        handler._get_power_levels = AsyncMock(
            return_value=({"users": {ALICE: 100, BOB: 100}}, {ALICE: 100, BOB: 100})
        )
        handler._create_direct_room = AsyncMock()
        handler._ensure_admin_power_levels = AsyncMock()
        handler._ensure_direct_entry = AsyncMock()

        result = await handler._ensure_direct_message_room(
            requester=MagicMock(), user_ids=[ALICE, BOB]
        )

        self.assertEqual(result["action"], ACTION_VALID_EXISTING_NOOP)
        self.assertFalse(result["created"])
        self.assertTrue(result["reused"])
        self.assertEqual(result["room_id"], ROOM_ID)
        self.assertEqual(result["m_direct_updated_for"], [])
        self.assertFalse(result["power_levels_updated"])
        handler._create_direct_room.assert_not_awaited()
        handler._ensure_admin_power_levels.assert_not_awaited()
        handler._ensure_direct_entry.assert_not_awaited()
        handler._api.account_data_manager.put_global.assert_not_awaited()

    async def test_existing_dm_repairs_only_missing_m_direct(self) -> None:
        handler = _handler()
        handler._find_existing_direct_room = AsyncMock(
            return_value=_existing_room(bob_direct={})
        )
        handler._get_power_levels = AsyncMock(
            return_value=({"users": {ALICE: 100, BOB: 100}}, {ALICE: 100, BOB: 100})
        )
        handler._ensure_admin_power_levels = AsyncMock(return_value=False)
        handler._ensure_direct_entry = AsyncMock(side_effect=[False, True])

        result = await handler._ensure_direct_message_room(
            requester=MagicMock(), user_ids=[ALICE, BOB]
        )

        self.assertEqual(result["action"], ACTION_REPAIRED_ACCOUNT_DATA)
        self.assertEqual(result["m_direct_updated_for"], [BOB])
        self.assertFalse(result["power_levels_updated"])
        handler._ensure_admin_power_levels.assert_awaited_once()
        self.assertEqual(handler._ensure_direct_entry.await_count, 2)

    async def test_existing_dm_repairs_power_without_account_data_writes(self) -> None:
        handler = _handler()
        handler._find_existing_direct_room = AsyncMock(return_value=_existing_room())
        handler._get_power_levels = AsyncMock(
            return_value=({"users": {ALICE: 100}}, {ALICE: 100})
        )
        handler._ensure_admin_power_levels = AsyncMock(return_value=True)
        handler._ensure_direct_entry = AsyncMock(side_effect=[False, False])

        result = await handler._ensure_direct_message_room(
            requester=MagicMock(), user_ids=[ALICE, BOB]
        )

        self.assertEqual(result["action"], ACTION_REPAIRED_MEMBERSHIP_OR_POWER)
        self.assertEqual(result["m_direct_updated_for"], [])
        self.assertTrue(result["power_levels_updated"])
        self.assertEqual(handler._ensure_direct_entry.await_count, 2)
        handler._api.account_data_manager.put_global.assert_not_awaited()

    async def test_missing_dm_reports_created_room(self) -> None:
        handler = _handler()
        handler._find_existing_direct_room = AsyncMock(return_value=None)
        handler._create_direct_room = AsyncMock(return_value=ROOM_ID)
        handler._ensure_admin_power_levels = AsyncMock(return_value=False)
        handler._ensure_direct_entry = AsyncMock(side_effect=[True, True])

        result = await handler._ensure_direct_message_room(
            requester=MagicMock(), user_ids=[ALICE, BOB]
        )

        self.assertEqual(result["action"], ACTION_CREATED_ROOM)
        self.assertTrue(result["created"])
        self.assertFalse(result["reused"])
        self.assertEqual(result["m_direct_updated_for"], [ALICE, BOB])

    async def test_preloaded_m_direct_entry_does_not_reread_or_rewrite(self) -> None:
        handler = _handler()
        handler._get_direct_map = AsyncMock()

        updated = await handler._ensure_direct_entry(
            ALICE,
            BOB,
            ROOM_ID,
            direct_map={BOB: [ROOM_ID]},
        )

        self.assertFalse(updated)
        handler._get_direct_map.assert_not_awaited()
        handler._api.account_data_manager.put_global.assert_not_awaited()
