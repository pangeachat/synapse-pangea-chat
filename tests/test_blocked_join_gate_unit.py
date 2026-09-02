"""Unit tests for the blocked join gate.

Mock the ModuleApi and Synapse's ignore index so these run without a
homeserver. Design: .github/instructions/blocked-join-gate.instructions.md
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from synapse.api.errors import Codes
from synapse.module_api import NOT_SPAM

from synapse_pangea_chat.blocked_join_gate import (
    BlockedJoinGate,
    is_blocked_by_room_admin,
    server_initiated_entry,
)

ROOM = "!room:example.com"
REQUESTER = "@requester:example.com"
ADMIN = "@admin:example.com"
OTHER_ADMIN = "@other_admin:example.com"
MEMBER = "@member:example.com"


def _state_event(event_type: str, state_key: str, content: dict) -> MagicMock:
    event = MagicMock()
    event.type = event_type
    event.state_key = state_key
    event.content = content
    return event


def _make_api(
    *,
    ignored_by: set[str] | None = None,
    power_users: dict[str, int] | None = None,
    users_default: int = 0,
    memberships: dict[str, str] | None = None,
    creator: str = ADMIN,
    has_power_levels: bool = True,
    creator_power: bool = False,
) -> MagicMock:
    """Build a mock ModuleApi whose room state and ignore index are canned.

    ``memberships`` maps user_id -> membership for m.room.member lookups;
    users absent from it have no member event. ``ignored_by`` is the set of
    users who ignore the requester.
    """
    api = MagicMock()
    api._hs.get_datastores.return_value.main.ignored_by = AsyncMock(
        return_value=frozenset(ignored_by or set())
    )
    memberships = memberships or {}
    power_users = power_users or {}

    async def get_room_state(room_id, event_filter=None):
        assert room_id == ROOM
        result = {}
        for event_type, state_key in event_filter or []:
            if event_type == "m.room.power_levels" and has_power_levels:
                result[(event_type, state_key)] = _state_event(
                    event_type,
                    state_key,
                    {"users": power_users, "users_default": users_default},
                )
            elif event_type == "m.room.create":
                create = _state_event(event_type, state_key, {"creator": creator})
                create.sender = creator
                # Explicit room_version: a bare MagicMock attribute would make
                # the creator-power getattr truthy in every test.
                create.room_version = SimpleNamespace(
                    msc4289_creator_power_enabled=creator_power
                )
                result[(event_type, state_key)] = create
            elif event_type == "m.room.member" and state_key is None:
                for user, membership in memberships.items():
                    result[(event_type, user)] = _state_event(
                        event_type, user, {"membership": membership}
                    )
            elif event_type == "m.room.member" and state_key in memberships:
                result[(event_type, state_key)] = _state_event(
                    event_type, state_key, {"membership": memberships[state_key]}
                )
        return result

    api.get_room_state = AsyncMock(side_effect=get_room_state)
    return api


class TestIsBlockedByRoomAdmin(unittest.IsolatedAsyncioTestCase):
    async def test_not_ignored_by_anyone(self) -> None:
        api = _make_api(power_users={ADMIN: 100}, memberships={ADMIN: "join"})
        self.assertFalse(await is_blocked_by_room_admin(api, ROOM, REQUESTER))

    async def test_ignored_by_the_only_admin(self) -> None:
        api = _make_api(
            ignored_by={ADMIN},
            power_users={ADMIN: 100},
            memberships={ADMIN: "join"},
        )
        self.assertTrue(await is_blocked_by_room_admin(api, ROOM, REQUESTER))

    async def test_ignored_by_every_admin(self) -> None:
        api = _make_api(
            ignored_by={ADMIN, OTHER_ADMIN, MEMBER},
            power_users={ADMIN: 100, OTHER_ADMIN: 100},
            memberships={ADMIN: "join", OTHER_ADMIN: "join", MEMBER: "join"},
        )
        self.assertTrue(await is_blocked_by_room_admin(api, ROOM, REQUESTER))

    async def test_ignored_by_only_some_admins_does_not_block(self) -> None:
        api = _make_api(
            ignored_by={OTHER_ADMIN},
            power_users={ADMIN: 100, OTHER_ADMIN: 100},
            memberships={ADMIN: "join", OTHER_ADMIN: "join"},
        )
        self.assertFalse(await is_blocked_by_room_admin(api, ROOM, REQUESTER))

    async def test_room_with_no_joined_admin_never_blocks(self) -> None:
        api = _make_api(
            ignored_by={ADMIN, MEMBER},
            power_users={ADMIN: 100},
            memberships={ADMIN: "leave", MEMBER: "join"},
        )
        self.assertFalse(await is_blocked_by_room_admin(api, ROOM, REQUESTER))

    async def test_ignored_by_non_admin_member_does_not_block(self) -> None:
        api = _make_api(
            ignored_by={MEMBER},
            power_users={ADMIN: 100, MEMBER: 50},
            memberships={ADMIN: "join", MEMBER: "join"},
        )
        self.assertFalse(await is_blocked_by_room_admin(api, ROOM, REQUESTER))

    async def test_admin_who_left_does_not_count(self) -> None:
        # ADMIN left but is still listed at 100; only OTHER_ADMIN counts, and
        # they block the requester, so the room blocks.
        api = _make_api(
            ignored_by={OTHER_ADMIN},
            power_users={ADMIN: 100, OTHER_ADMIN: 100},
            memberships={ADMIN: "leave", OTHER_ADMIN: "join"},
        )
        self.assertTrue(await is_blocked_by_room_admin(api, ROOM, REQUESTER))

    async def test_users_default_at_admin_level_fails_open(self) -> None:
        # Every-member-is-admin is a hostile-shaped config; the gate allows
        # rather than enumerate the room's membership.
        api = _make_api(
            ignored_by={ADMIN, MEMBER},
            power_users={},
            users_default=100,
            memberships={ADMIN: "join", MEMBER: "join"},
        )
        self.assertFalse(await is_blocked_by_room_admin(api, ROOM, REQUESTER))

    async def test_more_candidates_than_bound_fails_open(self) -> None:
        crowd = {f"@fake{i}:example.com": 100 for i in range(30)}
        api = _make_api(
            ignored_by=set(crowd),
            power_users=crowd,
            memberships={u: "join" for u in crowd},
        )
        self.assertFalse(await is_blocked_by_room_admin(api, ROOM, REQUESTER))

    async def test_creator_power_room_counts_creator_as_admin(self) -> None:
        # Room v12: creator holds power without a power-levels users entry.
        # The listed admin blocks, the creator does not — request allowed.
        api = _make_api(
            ignored_by={OTHER_ADMIN},
            power_users={OTHER_ADMIN: 100},
            memberships={ADMIN: "join", OTHER_ADMIN: "join"},
            creator=ADMIN,
            creator_power=True,
        )
        self.assertFalse(await is_blocked_by_room_admin(api, ROOM, REQUESTER))
        # Creator blocks too — request refused.
        api = _make_api(
            ignored_by={ADMIN, OTHER_ADMIN},
            power_users={OTHER_ADMIN: 100},
            memberships={ADMIN: "join", OTHER_ADMIN: "join"},
            creator=ADMIN,
            creator_power=True,
        )
        self.assertTrue(await is_blocked_by_room_admin(api, ROOM, REQUESTER))

    async def test_no_power_levels_event_falls_back_to_room_creator(self) -> None:
        api = _make_api(
            ignored_by={ADMIN},
            has_power_levels=False,
            creator=ADMIN,
            memberships={ADMIN: "join"},
        )
        self.assertTrue(await is_blocked_by_room_admin(api, ROOM, REQUESTER))

    async def test_no_admin_lookup_when_nobody_ignores_requester(self) -> None:
        # The ignore index is the cheap first check; room state is only read
        # once there is a candidate blocker.
        api = _make_api(power_users={ADMIN: 100}, memberships={ADMIN: "join"})
        await is_blocked_by_room_admin(api, ROOM, REQUESTER)
        api.get_room_state.assert_not_called()


class TestBlockedJoinGateCallbacks(unittest.IsolatedAsyncioTestCase):
    def _gate(self, api: MagicMock) -> BlockedJoinGate:
        return BlockedJoinGate(config=MagicMock(), api=api)

    def test_registers_join_and_event_callbacks(self) -> None:
        api = _make_api()
        gate = self._gate(api)
        api.register_spam_checker_callbacks.assert_called_once()
        spam_kwargs = api.register_spam_checker_callbacks.call_args.kwargs
        self.assertEqual(spam_kwargs["user_may_join_room"], gate.user_may_join_room)
        api.register_third_party_rules_callbacks.assert_called_once()
        rules_kwargs = api.register_third_party_rules_callbacks.call_args.kwargs
        self.assertEqual(rules_kwargs["check_event_allowed"], gate.check_event_allowed)

    async def test_join_allowed_when_not_blocked(self) -> None:
        api = _make_api(power_users={ADMIN: 100}, memberships={ADMIN: "join"})
        gate = self._gate(api)
        self.assertEqual(
            await gate.user_may_join_room(REQUESTER, ROOM, is_invited=False),
            NOT_SPAM,
        )

    async def test_join_refused_when_blocked(self) -> None:
        api = _make_api(
            ignored_by={ADMIN}, power_users={ADMIN: 100}, memberships={ADMIN: "join"}
        )
        gate = self._gate(api)
        self.assertEqual(
            await gate.user_may_join_room(REQUESTER, ROOM, is_invited=False),
            Codes.FORBIDDEN,
        )

    async def test_pending_invite_does_not_override_block(self) -> None:
        api = _make_api(
            ignored_by={ADMIN, OTHER_ADMIN},
            power_users={ADMIN: 100, OTHER_ADMIN: 100},
            memberships={ADMIN: "join", OTHER_ADMIN: "join"},
        )
        gate = self._gate(api)
        self.assertEqual(
            await gate.user_may_join_room(REQUESTER, ROOM, is_invited=True),
            Codes.FORBIDDEN,
        )

    async def test_knock_refused_when_blocked(self) -> None:
        api = _make_api(
            ignored_by={ADMIN}, power_users={ADMIN: 100}, memberships={ADMIN: "join"}
        )
        gate = self._gate(api)
        knock = _state_event("m.room.member", REQUESTER, {"membership": "knock"})
        knock.room_id = ROOM
        knock.sender = REQUESTER
        knock.membership = "knock"
        knock.is_state.return_value = True
        self.assertEqual(await gate.check_event_allowed(knock, {}), (False, None))

    async def test_knock_allowed_when_not_blocked(self) -> None:
        api = _make_api(power_users={ADMIN: 100}, memberships={ADMIN: "join"})
        gate = self._gate(api)
        knock = _state_event("m.room.member", REQUESTER, {"membership": "knock"})
        knock.room_id = ROOM
        knock.sender = REQUESTER
        knock.membership = "knock"
        knock.is_state.return_value = True
        self.assertEqual(await gate.check_event_allowed(knock, {}), (True, None))

    async def test_non_knock_events_pass_without_lookup(self) -> None:
        api = _make_api(
            ignored_by={ADMIN}, power_users={ADMIN: 100}, memberships={ADMIN: "join"}
        )
        gate = self._gate(api)
        for event_type, membership in [
            ("m.room.message", None),
            ("m.room.member", "join"),
            ("m.room.member", "leave"),
        ]:
            event = _state_event(event_type, REQUESTER, {})
            event.room_id = ROOM
            event.sender = REQUESTER
            event.membership = membership
            event.is_state.return_value = event_type == "m.room.member"
            self.assertEqual(await gate.check_event_allowed(event, {}), (True, None))
        api._hs.get_datastores.return_value.main.ignored_by.assert_not_called()

    async def test_admin_leave_kick_of_knocker_is_not_gated(self) -> None:
        # A membership event whose sender is not the target (an admin denying
        # a knock, kicking, etc.) is never a join request.
        api = _make_api(
            ignored_by={ADMIN}, power_users={ADMIN: 100}, memberships={ADMIN: "join"}
        )
        gate = self._gate(api)
        event = _state_event("m.room.member", REQUESTER, {"membership": "knock"})
        event.room_id = ROOM
        event.sender = ADMIN
        event.membership = "knock"
        event.is_state.return_value = True
        self.assertEqual(await gate.check_event_allowed(event, {}), (True, None))

    async def test_server_initiated_join_is_exempt(self) -> None:
        api = _make_api(
            ignored_by={ADMIN}, power_users={ADMIN: 100}, memberships={ADMIN: "join"}
        )
        gate = self._gate(api)
        with server_initiated_entry():
            self.assertEqual(
                await gate.user_may_join_room(REQUESTER, ROOM, is_invited=True),
                NOT_SPAM,
            )
        # Outside the context the same join is refused again.
        self.assertEqual(
            await gate.user_may_join_room(REQUESTER, ROOM, is_invited=True),
            Codes.FORBIDDEN,
        )

    async def test_member_event_without_membership_key_passes(self) -> None:
        api = _make_api(
            ignored_by={ADMIN}, power_users={ADMIN: 100}, memberships={ADMIN: "join"}
        )
        gate = self._gate(api)
        event = _state_event("m.room.member", REQUESTER, {})
        event.room_id = ROOM
        event.sender = REQUESTER
        event.is_state.return_value = True
        self.assertEqual(await gate.check_event_allowed(event, {}), (True, None))


if __name__ == "__main__":
    unittest.main()
