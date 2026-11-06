"""Pure decisions of the quest-courses reads (quest-courses.instructions.md)."""

import unittest

from synapse_pangea_chat.quest_courses.quest_courses import (
    STUCK_AFTER_MS,
    ChangePage,
    cursor_is_current,
    format_cursor,
    is_stuck,
    page_changes,
    parse_cursor,
    unclaimed_invitation,
)


class TestCursor(unittest.TestCase):
    def test_a_cursor_names_its_server_and_position(self) -> None:
        self.assertEqual(format_cursor("pangea.chat", 42), "pangea.chat/42")
        self.assertEqual(parse_cursor("pangea.chat/42"), ("pangea.chat", 42))
        self.assertEqual(parse_cursor("host:8448/0"), ("host:8448", 0))
        for bad in (None, "", "42", "/42", "x/-1", "x/4.2", "x/abc", "x/"):
            self.assertIsNone(parse_cursor(bad), bad)

    def test_a_cursor_ahead_of_the_server_starts_over(self) -> None:
        # Taken from another server, or before a restore.
        self.assertFalse(cursor_is_current(101, 100, False))

    def test_a_cursor_whose_changes_were_lost_starts_over(self) -> None:
        self.assertFalse(cursor_is_current(4, 100, True))

    def test_a_cursor_with_nothing_lost_is_current(self) -> None:
        self.assertTrue(cursor_is_current(5, 100, False))
        self.assertTrue(cursor_is_current(100, 100, False))


class TestPageChanges(unittest.TestCase):
    def test_a_short_page_is_caught_up_to_what_is_written(self) -> None:
        page = page_changes([(3, "!a"), (7, "!b")], limit=5, upto=90)
        self.assertEqual(page, ChangePage(["!a", "!b"], 90, True, None))

    def test_a_full_page_never_splits_one_stream_position(self) -> None:
        rows = [(3, "!a"), (4, "!b"), (6, "!c"), (6, "!d")]
        page = page_changes(rows, limit=3, upto=90)
        # Position 6 has a row past the limit, so all of 6 waits for the next
        # page, and the cursor stops just before it.
        self.assertEqual(page, ChangePage(["!a", "!b"], 5, False, None))

    def test_one_position_bigger_than_the_page_is_read_whole(self) -> None:
        rows = [(6, "!a"), (6, "!b"), (6, "!c")]
        page = page_changes(rows, limit=2, upto=90)
        self.assertEqual(page, ChangePage([], 6, False, 6))

    def test_a_room_changed_twice_is_returned_once(self) -> None:
        page = page_changes([(3, "!a"), (4, "!a")], limit=5, upto=9)
        self.assertEqual(page.room_ids, ["!a"])


class TestUnclaimed(unittest.TestCase):
    NOW = 10 * STUCK_AFTER_MS

    def test_a_waiting_invitation_is_never_stuck(self) -> None:
        self.assertFalse(is_stuck("prepared", None, self.NOW))

    def test_a_claim_building_past_the_limit_is_stuck(self) -> None:
        self.assertFalse(is_stuck("provisioning", self.NOW - STUCK_AFTER_MS, self.NOW))
        self.assertTrue(
            is_stuck("provisioning", self.NOW - STUCK_AFTER_MS - 1, self.NOW)
        )

    def test_a_claim_with_no_start_time_is_stuck(self) -> None:
        # Started before the start time was recorded.
        self.assertTrue(is_stuck("provisioning", None, self.NOW))

    def test_an_invitation_reports_its_quest_language_and_title(self) -> None:
        course = unclaimed_invitation(
            "inv-1",
            {"course_plan_id": "q1", "target_language": "es", "title": "Trip"},
            "provisioning",
            5,
            self.NOW - 1,
            self.NOW,
        )
        self.assertEqual(
            course,
            {
                "kind": "invitation",
                "invitation_id": "inv-1",
                "room_id": None,
                "quest_id": "q1",
                "l2": "es",
                "title": "Trip",
                "created_at_ms": 5,
                "claim_started_at_ms": self.NOW - 1,
                "stuck": False,
            },
        )

    def test_a_malformed_specification_reports_no_quest(self) -> None:
        course = unclaimed_invitation("inv-2", {}, "prepared", 5, None, self.NOW)
        self.assertIsNone(course["quest_id"])
        self.assertIsNone(course["title"])


if __name__ == "__main__":
    unittest.main()
