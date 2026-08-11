from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.delete_room import is_rate_limited as rate_limit_module
from synapse_pangea_chat.delete_room.is_rate_limited import is_rate_limited
from synapse_pangea_chat.delete_room.user_has_highest_power_level import (
    user_has_highest_power_level,
)

USER = "@teacher:my.domain.name"
OTHER = "@student:my.domain.name"
ROOM = "!room:my.domain.name"


def _api_with_power_levels(content: dict | None) -> MagicMock:
    api = MagicMock()
    state = {}
    if content is not None:
        state[("m.room.power_levels", "")] = SimpleNamespace(
            type="m.room.power_levels", content=content
        )
    api.get_room_state = AsyncMock(return_value=state)
    return api


class TestUserHasHighestPowerLevel(unittest.IsolatedAsyncioTestCase):
    async def test_no_power_levels_event_denies(self):
        api = _api_with_power_levels(None)
        self.assertFalse(await user_has_highest_power_level(api, USER, ROOM))

    async def test_empty_users_map_everyone_at_default_qualifies(self):
        api = _api_with_power_levels({"users_default": 0, "users": {}})
        self.assertTrue(await user_has_highest_power_level(api, USER, ROOM))

    async def test_tie_at_top_qualifies(self):
        api = _api_with_power_levels({"users": {USER: 100, OTHER: 100}})
        self.assertTrue(await user_has_highest_power_level(api, USER, ROOM))

    async def test_below_top_denies(self):
        api = _api_with_power_levels({"users": {USER: 50, OTHER: 100}})
        self.assertFalse(await user_has_highest_power_level(api, USER, ROOM))

    async def test_unlisted_requester_below_listed_admin_denies(self):
        api = _api_with_power_levels({"users_default": 0, "users": {OTHER: 100}})
        self.assertFalse(await user_has_highest_power_level(api, USER, ROOM))

    async def test_listed_requester_below_users_default_denies(self):
        api = _api_with_power_levels({"users_default": 100, "users": {USER: 50}})
        self.assertFalse(await user_has_highest_power_level(api, USER, ROOM))


class TestIsRateLimited(unittest.TestCase):
    def setUp(self):
        rate_limit_module.request_log.clear()
        self.config = PangeaChatConfig(
            delete_room_requests_per_burst=3,
            delete_room_burst_duration_seconds=60,
        )

    def test_allows_up_to_burst_then_limits(self):
        with patch.object(rate_limit_module.time, "time", return_value=1000.0):
            for _ in range(3):
                self.assertFalse(is_rate_limited(USER, self.config))
            self.assertTrue(is_rate_limited(USER, self.config))

    def test_limit_is_per_user(self):
        with patch.object(rate_limit_module.time, "time", return_value=1000.0):
            for _ in range(3):
                is_rate_limited(USER, self.config)
            self.assertFalse(is_rate_limited(OTHER, self.config))

    def test_window_expiry_allows_again(self):
        with patch.object(rate_limit_module.time, "time", return_value=1000.0):
            for _ in range(3):
                is_rate_limited(USER, self.config)
            self.assertTrue(is_rate_limited(USER, self.config))
        with patch.object(rate_limit_module.time, "time", return_value=1061.0):
            self.assertFalse(is_rate_limited(USER, self.config))


if __name__ == "__main__":
    unittest.main()
