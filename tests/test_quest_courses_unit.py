"""Pure decisions of the quest-courses reads (quest-courses.instructions.md)."""

import unittest

from synapse_pangea_chat.quest_courses.quest_courses import (
    ChangePage,
    cursor_is_current,
    format_cursor,
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
        page = page_changes([(3, "!a", True), (7, "!b", False)], limit=5, upto=90)
        self.assertEqual(page, ChangePage(["!a", "!b"], ["!a"], 90, True, None))

    def test_a_full_page_never_splits_one_stream_position(self) -> None:
        rows = [(3, "!a", False), (4, "!b", True), (6, "!c", True), (6, "!d", False)]
        page = page_changes(rows, limit=3, upto=90)
        # Position 6 has a row past the limit, so all of 6 waits for the next
        # page, and the cursor stops just before it.
        self.assertEqual(page, ChangePage(["!a", "!b"], ["!b"], 5, False, None))

    def test_one_position_bigger_than_the_page_is_read_whole(self) -> None:
        rows = [(6, "!a", True), (6, "!b", False), (6, "!c", False)]
        page = page_changes(rows, limit=2, upto=90)
        self.assertEqual(page, ChangePage([], [], 6, False, 6))

    def test_a_room_changed_twice_is_returned_once(self) -> None:
        page = page_changes([(3, "!a", False), (4, "!a", True)], limit=5, upto=9)
        self.assertEqual(page.room_ids, ["!a"])
        self.assertEqual(page.plan_room_ids, ["!a"])


class TestUnclaimed(unittest.TestCase):
    def test_an_invitation_reports_its_quest_language_and_title(self) -> None:
        course = unclaimed_invitation(
            "inv-1",
            {"course_plan_id": "q1", "target_language": "es", "title": "Trip"},
            5,
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
            },
        )

    def test_a_malformed_specification_reports_no_quest(self) -> None:
        course = unclaimed_invitation("inv-2", {}, 5)
        self.assertIsNone(course["quest_id"])
        self.assertIsNone(course["title"])


if __name__ == "__main__":
    unittest.main()
