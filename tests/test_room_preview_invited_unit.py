"""The session preview's ``invited`` list: who holds a pending invite to a session.

Read from the membership the preview already fetches each request, so it costs no
extra query and is never older than the request; only the membership-gated session
previews carry it, never the plain ``room_preview``.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

from synapse_pangea_chat.room_preview.get_room_preview import (
    _room_cache,
    get_room_preview,
)

ROOM = "!session:my.domain.name"
TYPES = ["pangea.activity_roles", "pangea.activity_plan"]


def _member(membership: str) -> Any:
    event = MagicMock()
    event.content = {"membership": membership}
    return event


def _api(memberships: Dict[str, str]) -> Any:
    api = MagicMock()
    api.get_room_state = AsyncMock(
        return_value={
            ("m.room.member", user): _member(m) for user, m in memberships.items()
        }
    )
    return api


def _store() -> Any:
    store = MagicMock()
    engine_module = MagicMock()
    engine_module.__name__ = "sqlite3"
    store.db_pool.engine.module = engine_module
    roles = {"content": {"roles": {"r1": {"user_id": "@held:d"}}}}
    store.db_pool.execute = AsyncMock(
        return_value=[(ROOM, "pangea.activity_roles", "", json.dumps(roles))]
    )
    return store


def _config() -> Any:
    config = MagicMock()
    config.room_preview_state_event_types = TYPES
    return config


class TestInvited(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _room_cache.clear()

    def tearDown(self) -> None:
        _room_cache.clear()

    async def _read(
        self, api: Any, store: Any, include_invited: Optional[bool]
    ) -> Dict[str, Any]:
        if include_invited is None:
            result = await get_room_preview([ROOM], api, store, _config())
        else:
            result = await get_room_preview(
                [ROOM], api, store, _config(), include_invited=include_invited
            )
        return result[ROOM]

    async def test_lists_pending_invites_only(self) -> None:
        api = _api(
            {
                "@held:d": "join",
                "@asked:d": "invite",
                "@gone:d": "leave",
                "@also:d": "invite",
            }
        )
        room = await self._read(api, _store(), True)
        self.assertEqual(room["invited"], ["@also:d", "@asked:d"])
        # The role-holder summary is unchanged by it.
        self.assertEqual(room["membership_summary"], {"@held:d": "join"})

    async def test_cached_room_answers_current_invites_without_a_state_query(
        self,
    ) -> None:
        store = _store()
        await self._read(_api({"@held:d": "join", "@asked:d": "invite"}), store, True)
        # She joined; the cached state answers, the membership is read afresh.
        room = await self._read(
            _api({"@held:d": "join", "@asked:d": "join"}), store, True
        )
        self.assertEqual(room["invited"], [])
        self.assertEqual(store.db_pool.execute.await_count, 1)

    async def test_plain_preview_never_carries_it(self) -> None:
        store = _store()
        api = _api({"@held:d": "join", "@asked:d": "invite"})
        await self._read(api, store, True)
        # A session read before must not leave it in the shared cache.
        self.assertNotIn("invited", await self._read(api, store, None))
        self.assertNotIn("invited", await self._read(api, store, False))

    async def test_session_without_roles_yet_still_lists_its_invites(self) -> None:
        store = _store()
        store.db_pool.execute = AsyncMock(return_value=[])
        room = await self._read(_api({"@asked:d": "invite"}), store, True)
        self.assertEqual(room["invited"], ["@asked:d"])


if __name__ == "__main__":
    unittest.main()
