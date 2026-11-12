from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.delete_room import is_rate_limited as rate_limit_module
from synapse_pangea_chat.delete_room.cleanup_space_relationships import (
    cleanup_space_relationships,
)
from synapse_pangea_chat.delete_room.is_rate_limited import is_rate_limited
from synapse_pangea_chat.delete_room.user_has_highest_power_level import (
    user_has_highest_power_level,
)

USER = "@teacher:my.domain.name"
OTHER = "@student:my.domain.name"
BOT = "@bot:my.domain.name"
ROOM = "!room:my.domain.name"
SPACE = "!space:my.domain.name"


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

    async def test_joined_members_ignore_a_departed_admin(self):
        api = _api_with_power_levels({"users": {OTHER: 100, USER: 50}})
        self.assertTrue(
            await user_has_highest_power_level(api, USER, ROOM, joined_members={USER})
        )

    async def test_joined_members_still_count_a_joined_admin(self):
        api = _api_with_power_levels({"users": {OTHER: 100, USER: 50}})
        self.assertFalse(
            await user_has_highest_power_level(
                api, USER, ROOM, joined_members={USER, OTHER}
            )
        )

    async def test_joined_members_count_an_unlisted_member_at_users_default(self):
        api = _api_with_power_levels({"users_default": 100, "users": {USER: 50}})
        self.assertFalse(
            await user_has_highest_power_level(
                api, USER, ROOM, joined_members={USER, OTHER}
            )
        )


def _event(event_type: str, state_key: str, content: dict) -> SimpleNamespace:
    return SimpleNamespace(type=event_type, state_key=state_key, content=content)


def _joined(user_id: str) -> SimpleNamespace:
    return _event("m.room.member", user_id, {"membership": "join"})


def _api_for_unlink(space_power_levels: dict, space_members: list) -> MagicMock:
    """ROOM is a child of SPACE, whose state is the given levels and members."""
    api = MagicMock()
    api.is_mine = lambda user_id: user_id.endswith(":my.domain.name")
    space_state = [
        _event("m.room.power_levels", "", space_power_levels),
        _event("m.space.child", ROOM, {"via": ["my.domain.name"]}),
        *space_members,
    ]

    async def get_room_state(room_id, event_filter):
        if room_id == ROOM:
            events = [_event("m.space.parent", SPACE, {"via": ["my.domain.name"]})]
        else:
            events = space_state
        types = {event_type for event_type, _ in event_filter}
        return {(e.type, e.state_key): e for e in events if e.type in types}

    api.get_room_state = AsyncMock(side_effect=get_room_state)
    api.create_and_send_event_into_room = AsyncMock()
    return api


class TestCleanupSpaceRelationships(unittest.IsolatedAsyncioTestCase):
    def assert_unlinked_as(self, api: MagicMock, sender: str):
        api.create_and_send_event_into_room.assert_awaited_once()
        event = api.create_and_send_event_into_room.await_args.args[0]
        self.assertEqual(event["room_id"], SPACE)
        self.assertEqual(event["state_key"], ROOM)
        self.assertEqual(event["content"], {})
        self.assertEqual(event["sender"], sender)

    async def test_a_requester_who_may_change_the_space_unlinks_as_themselves(self):
        api = _api_for_unlink(
            {"users": {USER: 50, OTHER: 100}}, [_joined(USER), _joined(OTHER)]
        )
        await cleanup_space_relationships(api, ROOM, USER)
        self.assert_unlinked_as(api, USER)

    async def test_a_requester_outside_the_space_unlinks_as_its_top_member(self):
        # Courses the client creates let every member change their children,
        # so the student could send it too; the teacher outranks them.
        api = _api_for_unlink(
            {"users": {USER: 100}, "events": {"m.space.child": 0}},
            [_joined(OTHER), _joined(USER)],
        )
        await cleanup_space_relationships(api, ROOM, BOT)
        self.assert_unlinked_as(api, USER)

    async def test_departed_and_remote_members_are_not_chosen(self):
        api = _api_for_unlink(
            {
                "users": {
                    "@gone:my.domain.name": 100,
                    "@remote:other.server": 100,
                    OTHER: 50,
                }
            },
            [
                _event(
                    "m.room.member", "@gone:my.domain.name", {"membership": "leave"}
                ),
                _joined("@remote:other.server"),
                _joined(OTHER),
            ],
        )
        await cleanup_space_relationships(api, ROOM, BOT)
        self.assert_unlinked_as(api, OTHER)

    async def test_a_link_no_member_may_remove_is_left_with_a_warning(self):
        # m.space.child falls back to state_default, which defaults to 50
        api = _api_for_unlink({"users": {OTHER: 0}}, [_joined(OTHER)])
        with self.assertLogs(
            "synapse.module.synapse_pangea_chat.delete_room.cleanup_space_relationships",
            level="WARNING",
        ) as logs:
            await cleanup_space_relationships(api, ROOM, BOT)
        api.create_and_send_event_into_room.assert_not_awaited()
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])


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
