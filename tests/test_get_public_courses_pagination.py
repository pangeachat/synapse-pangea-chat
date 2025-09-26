import unittest
from typing import Any, Dict, List, Optional

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.get_public_courses import (
    DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE,
    RESPONSE_STATE_EVENTS,
    _cache,
    get_public_courses,
)


class FakeDBPool:
    def __init__(
        self,
        rooms: List[str],
        state_data: Dict[str, Dict[str, Dict[Optional[str], Dict[str, Any]]]],
    ):
        self._rooms = rooms
        self._state_data = state_data
        self._event_types = list(
            dict.fromkeys(
                RESPONSE_STATE_EVENTS + (DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE,)
            )
        )

    async def execute(self, name: str, _query: str, *params: Any):
        if name == "get_public_courses_total_count":
            return [(len(self._rooms),)]

        if name == "get_public_courses_room_ids":
            _, offset, limit = params
            subset = self._rooms[offset : offset + limit]
            return [(room_id,) for room_id in subset]

        if name == "get_public_courses_state_events":
            room_param_count = len(params) - len(self._event_types)
            room_ids = params[:room_param_count]
            rows = []
            for room_id in room_ids:
                room_state = self._state_data.get(room_id, {})
                for event_type, state_entries in room_state.items():
                    for state_key, event_json in state_entries.items():
                        rows.append((room_id, event_type, state_key, event_json))
            return rows

        raise AssertionError(f"Unexpected query name: {name}")


class FakeRoomStore:
    def __init__(
        self,
        rooms: List[str],
        state_data: Dict[str, Dict[str, Dict[Optional[str], Dict[str, Any]]]],
    ):
        self.db_pool = FakeDBPool(rooms, state_data)


class GetPublicCoursesPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _cache.clear()
        self.config = PangeaChatConfig()
        self.rooms = [
            "!course-000:server",
            "!course-001:server",
            "!course-002:server",
        ]
        self.state_data: Dict[str, Dict[str, Dict[Optional[str], Dict[str, Any]]]] = {}
        for idx, room_id in enumerate(self.rooms, start=1):
            self.state_data[room_id] = {
                DEFAULT_REQUIRED_COURSE_STATE_EVENT_TYPE: {
                    None: {"content": {"plan_id": f"plan-{idx}"}}
                },
                "m.room.name": {None: {"content": {"name": f"Course {idx}"}}},
                "m.room.topic": {None: {"content": {"topic": f"Topic {idx}"}}},
            }
        self.room_store = FakeRoomStore(self.rooms, self.state_data)

    async def test_returns_next_token_when_more_results(self):
        response = await get_public_courses(
            self.room_store, self.config, limit=2, since=None
        )

        self.assertEqual(len(response["chunk"]), 2)
        self.assertEqual(
            [course["room_id"] for course in response["chunk"]], self.rooms[:2]
        )
        self.assertEqual(response["next_batch"], "2")
        self.assertIsNone(response["prev_batch"])
        self.assertEqual(response["total_room_count_estimate"], len(self.rooms))

    async def test_returns_prev_token_on_later_page(self):
        response = await get_public_courses(
            self.room_store, self.config, limit=2, since="2"
        )

        self.assertEqual(len(response["chunk"]), 1)
        self.assertEqual(
            [course["room_id"] for course in response["chunk"]], self.rooms[2:3]
        )
        self.assertIsNone(response["next_batch"])
        self.assertEqual(response["prev_batch"], "0")
        self.assertEqual(response["total_room_count_estimate"], len(self.rooms))


if __name__ == "__main__":
    unittest.main()
