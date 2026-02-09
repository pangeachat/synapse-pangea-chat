"""
Tests for the reactive cache functionality in synapse_room_preview.

This module tests that cache invalidation works correctly when state events are received.
"""

import unittest
from typing import Any, Dict

from synapse_pangea_chat.room_preview.get_room_preview import (
    _cache_room_data,
    _get_cached_room,
    _room_cache,
    invalidate_room_cache,
)


class TestReactiveCache(unittest.TestCase):
    def setUp(self) -> None:
        """Clear the cache before each test."""
        _room_cache.clear()

    def test_invalidate_room_cache(self) -> None:
        """Test that cache invalidation removes the specified room from cache."""
        room_id = "!test:example.com"
        test_data: Dict[str, Dict[str, Any]] = {
            "p.room_summary": {"default": {"content": {"name": "Test Room"}}}
        }

        # Cache some data
        _cache_room_data(room_id, test_data)

        # Verify it's cached
        cached_data = _get_cached_room(room_id)
        self.assertIsNotNone(cached_data)
        self.assertEqual(cached_data, test_data)

        # Invalidate the cache
        invalidate_room_cache(room_id)

        # Verify it's no longer cached
        cached_data = _get_cached_room(room_id)
        self.assertIsNone(cached_data)

    def test_invalidate_room_cache_nonexistent(self) -> None:
        """Test that invalidating a non-existent room doesn't cause errors."""
        room_id = "!nonexistent:example.com"

        # This should not raise an exception
        invalidate_room_cache(room_id)

        # Cache should still be empty
        self.assertEqual(len(_room_cache), 0)

    def test_invalidate_multiple_rooms(self) -> None:
        """Test that invalidating one room doesn't affect other cached rooms."""
        room_id_1 = "!test1:example.com"
        room_id_2 = "!test2:example.com"

        test_data_1: Dict[str, Dict[str, Any]] = {
            "p.room_summary": {"default": {"content": {"name": "Test Room 1"}}}
        }
        test_data_2: Dict[str, Dict[str, Any]] = {
            "p.room_summary": {"default": {"content": {"name": "Test Room 2"}}}
        }

        # Cache data for both rooms
        _cache_room_data(room_id_1, test_data_1)
        _cache_room_data(room_id_2, test_data_2)

        # Verify both are cached
        self.assertIsNotNone(_get_cached_room(room_id_1))
        self.assertIsNotNone(_get_cached_room(room_id_2))

        # Invalidate only the first room
        invalidate_room_cache(room_id_1)

        # Verify only the first room is invalidated
        self.assertIsNone(_get_cached_room(room_id_1))
        self.assertIsNotNone(_get_cached_room(room_id_2))
        self.assertEqual(_get_cached_room(room_id_2), test_data_2)

    def tearDown(self) -> None:
        """Clear the cache after each test."""
        _room_cache.clear()


if __name__ == "__main__":
    unittest.main()
