"""CodeIndex (knock-with-code.instructions.md, "Code Lookup")."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple, cast

from synapse.util.clock import Clock
from twisted.internet import defer
from twisted.internet.testing import MemoryReactorClock
from twisted.python.failure import Failure

from synapse_pangea_chat.room_code.code_index import CodeIndex, RoomCodeMatch


class FakeStore:
    """Current join rules per room, with the stream position each was written at."""

    def __init__(self, reactor: MemoryReactorClock) -> None:
        self.rooms: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self.committed = 0
        self.refresh_args: List[Tuple[Any, ...]] = []
        # When set, refresh queries wait on this until the test fires it.
        self.hold_refreshes: Optional["defer.Deferred[None]"] = None
        self.hs = SimpleNamespace(get_clock=lambda: Clock(cast(Any, reactor), "test"))
        self.database_engine = SimpleNamespace(supports_using_any_list=False)
        self.db_pool = SimpleNamespace(execute=self._execute)

    def set_rules(self, room_id: str, stream: int, **content: Any) -> None:
        self.rooms[room_id] = (stream, content)
        self.committed = max(self.committed, stream)

    def get_room_max_stream_ordering(self) -> int:
        return self.committed

    async def _execute(self, desc: str, sql: str, *args: Any) -> List[Tuple[str, str]]:
        if desc == "pangea_code_index_confirm":
            wanted = set(args)
        else:
            self.refresh_args.append(args)
            if self.hold_refreshes is not None:
                await self.hold_refreshes
            after = args[0] if args else None
            wanted = {
                room_id
                for room_id, (stream, _) in self.rooms.items()
                if after is None or stream > after
            }
        return [
            (room_id, json.dumps({"content": content}))
            for room_id, (_, content) in self.rooms.items()
            if room_id in wanted
        ]


class TestCodeIndex(unittest.TestCase):
    def setUp(self) -> None:
        self.reactor = MemoryReactorClock()
        self.store = FakeStore(self.reactor)
        self.index = CodeIndex(self.store)

    def lookup(self, code: str) -> "defer.Deferred[List[RoomCodeMatch]]":
        return defer.ensureDeferred(self.index.lookup(code))

    def result(self, d: "defer.Deferred[Any]") -> Any:
        out: List[Any] = []
        d.addBoth(out.append)
        for _ in range(20):
            if out:
                break
            self.reactor.advance(0)
        self.assertEqual(len(out), 1, "lookup did not finish")
        if isinstance(out[0], Failure):
            out[0].raiseException()
        return out[0]

    def test_first_lookup_loads_everything_unfiltered(self) -> None:
        self.store.set_rules("!a:x", 3, access_code="abc1234")

        self.assertEqual(
            self.result(self.lookup("ABC1234")),
            [RoomCodeMatch(room_id="!a:x", is_admin_code=False)],
        )
        self.assertEqual(self.store.refresh_args, [()])

    def test_admin_status_comes_from_state(self) -> None:
        self.store.set_rules(
            "!a:x", 1, access_code="abc1234", admin_access_code="adm1234"
        )

        self.assertEqual(
            self.result(self.lookup("adm1234")),
            [RoomCodeMatch(room_id="!a:x", is_admin_code=True)],
        )
        # The admin code is used up: the room still holds the class code only.
        self.store.set_rules("!a:x", 2, access_code="abc1234")
        self.assertEqual(self.result(self.lookup("adm1234")), [])

    def test_new_course_resolves_first_try_via_catch_up(self) -> None:
        self.store.set_rules("!a:x", 1, access_code="abc1234")
        self.result(self.lookup("abc1234"))

        self.store.set_rules("!b:x", 2, access_code="new1234")

        self.assertEqual(
            self.result(self.lookup("new1234")),
            [RoomCodeMatch(room_id="!b:x", is_admin_code=False)],
        )
        self.assertEqual(self.store.refresh_args, [(), (1,)])

    def test_rotated_code_stops_matching(self) -> None:
        self.store.set_rules("!a:x", 1, access_code="old1234")
        self.result(self.lookup("old1234"))

        self.store.set_rules("!a:x", 2, access_code="new1234")

        self.assertEqual(self.result(self.lookup("old1234")), [])
        self.assertEqual(
            self.result(self.lookup("new1234")),
            [RoomCodeMatch(room_id="!a:x", is_admin_code=False)],
        )
        self.assertNotIn("old1234", self.index._rooms_by_code)

    def test_position_is_the_committed_one_read_before_the_query(self) -> None:
        # A row at 5 is visible although Synapse only reports 3 committed:
        # the next catch-up must start from 3, not from the highest row seen.
        self.store.set_rules("!a:x", 3, access_code="abc1234")
        self.store.rooms["!b:x"] = (5, {"access_code": "bbb1234"})
        self.result(self.lookup("zzz9999"))
        self.result(self.lookup("zzz9999"))

        self.assertEqual(self.store.refresh_args, [(), (3,)])

    def test_concurrent_misses_share_one_refresh_after_the_one_in_flight(self) -> None:
        self.store.hold_refreshes = defer.Deferred()
        first = self.lookup("zzz9999")
        self.reactor.advance(0)
        waiting = [self.lookup("zzz9999") for _ in range(5)]
        self.reactor.advance(0)

        self.store.hold_refreshes, held = None, self.store.hold_refreshes
        held.callback(None)
        self.reactor.advance(0)

        for d in [first, *waiting]:
            self.assertEqual(self.result(d), [])
        self.assertEqual(len(self.store.refresh_args), 2)


if __name__ == "__main__":
    unittest.main()
