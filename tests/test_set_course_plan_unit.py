"""Unit tests for the set_course_plan decision and request parsing."""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional

from synapse_pangea_chat.set_course_plan.set_course_plan import (
    MISMATCH,
    NOT_A_COURSE,
    UNCHANGED,
    UPDATE,
    parse_request,
    plan_update,
)


class TestPlanUpdate(unittest.TestCase):
    def test_no_course_plan_is_not_a_course(self) -> None:
        contents: List[Optional[Dict[str, Any]]] = [
            None,
            {},
            {"uuid": ""},
            {"l2": "es"},
        ]
        for content in contents:
            with self.subTest(content=content):
                self.assertEqual(
                    plan_update(content, "old", "new").outcome, NOT_A_COURSE
                )

    def test_a_different_current_plan_is_refused(self) -> None:
        update = plan_update({"uuid": "teacher-pick", "l2": "es"}, "old", "new")

        self.assertEqual(update.outcome, MISMATCH)
        self.assertEqual(update.current_quest_id, "teacher-pick")
        self.assertIsNone(update.content)

    def test_the_expected_id_is_checked_before_already_there(self) -> None:
        # The doc orders the checks: a space that does not point where the
        # operator expected is refused, even if it already points at the target.
        update = plan_update({"uuid": "new"}, "old", "new")

        self.assertEqual(update.outcome, MISMATCH)

    def test_already_on_the_new_quest_is_unchanged(self) -> None:
        update = plan_update({"uuid": "new", "l2": "es"}, "new", "new")

        self.assertEqual(update.outcome, UNCHANGED)
        self.assertIsNone(update.content)

    def test_update_swaps_the_id_and_keeps_every_other_field(self) -> None:
        update = plan_update({"uuid": "old", "l2": "es", "extra": 1}, "old", "new")

        self.assertEqual(update.outcome, UPDATE)
        self.assertEqual(update.current_quest_id, "old")
        self.assertEqual(update.content, {"uuid": "new", "l2": "es", "extra": 1})

    def test_legacy_plan_id_is_read_and_then_dropped(self) -> None:
        update = plan_update({"course_plan_id": "old", "l2": "es"}, "old", "new")

        self.assertEqual(update.outcome, UPDATE)
        self.assertEqual(update.content, {"uuid": "new", "l2": "es"})


class TestParseRequest(unittest.TestCase):
    _VALID = {
        "room_id": "!space:pangea.chat",
        "quest_id": "new",
        "expected_quest_id": "old",
    }

    def test_valid_body(self) -> None:
        parsed, error = parse_request({**self._VALID, "dry_run": True})

        self.assertIsNone(error)
        self.assertEqual(parsed, ("!space:pangea.chat", "new", "old", True))

    def test_dry_run_defaults_to_false(self) -> None:
        parsed, error = parse_request(self._VALID)

        self.assertIsNone(error)
        assert parsed is not None
        self.assertFalse(parsed[3])

    def test_invalid_bodies_are_refused(self) -> None:
        bad = [
            "not an object",
            {**self._VALID, "room_id": "#alias:pangea.chat"},
            {**self._VALID, "room_id": 5},
            {**self._VALID, "quest_id": ""},
            {**self._VALID, "quest_id": "x" * 256},
            {k: v for k, v in self._VALID.items() if k != "expected_quest_id"},
            {**self._VALID, "dry_run": "yes"},
        ]
        for body in bad:
            with self.subTest(body=body):
                parsed, error = parse_request(body)
                self.assertIsNone(parsed)
                self.assertIsInstance(error, str)


if __name__ == "__main__":
    unittest.main()
