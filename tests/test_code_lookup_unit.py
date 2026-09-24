"""Code lookups see claim codes, which are not in join rules
(knock-with-code.instructions.md, "Claiming a course")."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.room_code.code_lookup import code_is_taken, rooms_for_code
from synapse_pangea_chat.room_code.get_rooms_with_access_code import RoomCodeMatch

LOOKUP = "synapse_pangea_chat.room_code.code_lookup"


def _store(claim_rooms: list[str], in_use: bool = False) -> MagicMock:
    store = MagicMock()
    store.rooms_for_admin_code = AsyncMock(return_value=claim_rooms)
    store.code_in_use = AsyncMock(return_value=in_use)
    return store


class TestRoomsForCode(unittest.IsolatedAsyncioTestCase):
    async def test_claim_rooms_join_the_state_matches_once(self) -> None:
        state = [RoomCodeMatch(room_id="!a:x", is_admin_code=False)]
        with patch(
            f"{LOOKUP}.get_rooms_with_access_code", AsyncMock(return_value=state)
        ):
            found = await rooms_for_code("c0de123", MagicMock(), _store(["!b:x"]))

        assert found is not None
        matches, claim_rooms = found
        self.assertEqual(
            matches,
            [
                RoomCodeMatch(room_id="!a:x", is_admin_code=False),
                RoomCodeMatch(room_id="!b:x", is_admin_code=True),
            ],
        )
        self.assertEqual(claim_rooms, {"!b:x"})

    async def test_state_lookup_failure_is_reported(self) -> None:
        with patch(
            f"{LOOKUP}.get_rooms_with_access_code", AsyncMock(return_value=None)
        ):
            self.assertIsNone(await rooms_for_code("c0de123", MagicMock(), _store([])))


class TestCodeIsTaken(unittest.IsolatedAsyncioTestCase):
    async def test_a_claim_code_is_taken(self) -> None:
        # Even spent: a generated class code must never equal a claim code.
        with patch(f"{LOOKUP}.get_rooms_with_access_code", AsyncMock(return_value=[])):
            self.assertTrue(
                await code_is_taken("c0de123", MagicMock(), _store([], in_use=True))
            )

    async def test_a_free_code_is_free(self) -> None:
        with patch(f"{LOOKUP}.get_rooms_with_access_code", AsyncMock(return_value=[])):
            self.assertFalse(await code_is_taken("c0de123", MagicMock(), _store([])))

    async def test_a_failed_lookup_counts_as_taken(self) -> None:
        with patch(
            f"{LOOKUP}.get_rooms_with_access_code", AsyncMock(return_value=None)
        ):
            self.assertTrue(await code_is_taken("c0de123", MagicMock(), _store([])))


if __name__ == "__main__":
    unittest.main()
