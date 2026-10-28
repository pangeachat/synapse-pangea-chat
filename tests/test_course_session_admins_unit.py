import unittest
from types import SimpleNamespace
from typing import Any, Dict, Optional

from synapse_pangea_chat.course_session_admins.course_session_admins import (
    is_activity_session_type,
    is_first_link,
    mute_rule_conditions,
    with_room_listed,
)

ROOM = "!session:example.com"
OTHER = "!other:example.com"


def _event(
    *,
    event_type: str = "m.space.child",
    state_key: Optional[str] = ROOM,
    content: Optional[Dict[str, Any]] = None,
    unsigned: Optional[Dict[str, Any]] = None,
) -> Any:
    return SimpleNamespace(
        type=event_type,
        state_key=state_key,
        content={"via": ["example.com"]} if content is None else content,
        unsigned=unsigned or {},
        is_state=lambda: state_key is not None,
    )


class TestIsActivitySessionType(unittest.TestCase):
    def test_the_clients_session_type_with_and_without_an_activity(self):
        self.assertTrue(is_activity_session_type("p.activity.session:act-1"))
        self.assertTrue(is_activity_session_type("p.activity.session"))

    def test_other_room_types_are_not_sessions(self):
        self.assertFalse(is_activity_session_type("m.space"))
        self.assertFalse(is_activity_session_type("p.analytics"))
        self.assertFalse(is_activity_session_type("p.activity.sessionx"))
        self.assertFalse(is_activity_session_type(None))
        self.assertFalse(is_activity_session_type(7))


class TestIsFirstLink(unittest.TestCase):
    def test_a_new_link_is_the_first(self):
        self.assertTrue(is_first_link(_event()))

    def test_a_link_sent_again_is_not(self):
        # No backfill: an older session's link re-sent replaces an earlier one.
        event = _event(unsigned={"replaces_state": "$earlier"})
        self.assertFalse(is_first_link(event))

    def test_an_unlink_is_not(self):
        self.assertFalse(is_first_link(_event(content={})))

    def test_other_events_are_not(self):
        self.assertFalse(is_first_link(_event(event_type="m.room.member")))
        self.assertFalse(is_first_link(_event(state_key="")))
        self.assertFalse(is_first_link(_event(state_key=None)))


class TestWithRoomListed(unittest.TestCase):
    def test_adds_to_an_empty_list(self):
        self.assertEqual(with_room_listed(None, ROOM), {"room_ids": [ROOM]})

    def test_keeps_the_list_sorted_and_other_keys(self):
        content = {"room_ids": [ROOM], "other": 1}
        self.assertEqual(
            with_room_listed(content, OTHER),
            {"room_ids": sorted([ROOM, OTHER]), "other": 1},
        )

    def test_keeps_rooms_read_back_frozen(self):
        # Synapse's get_global answers frozen data: the list arrives as a tuple,
        # and every room already on it must stay listed.
        self.assertEqual(
            with_room_listed({"room_ids": (ROOM,)}, OTHER),
            {"room_ids": sorted([ROOM, OTHER])},
        )

    def test_an_already_listed_room_writes_nothing(self):
        self.assertIsNone(with_room_listed({"room_ids": [ROOM]}, ROOM))

    def test_malformed_entries_are_dropped_not_raised_on(self):
        self.assertEqual(
            with_room_listed({"room_ids": [ROOM, 3, None]}, OTHER),
            {"room_ids": sorted([ROOM, OTHER])},
        )
        self.assertEqual(
            with_room_listed({"room_ids": "nope"}, ROOM), {"room_ids": [ROOM]}
        )


class TestMuteRule(unittest.TestCase):
    def test_matches_the_room_like_the_apps_mute(self):
        self.assertEqual(
            mute_rule_conditions(ROOM),
            [{"kind": "event_match", "key": "room_id", "pattern": ROOM}],
        )


if __name__ == "__main__":
    unittest.main()
