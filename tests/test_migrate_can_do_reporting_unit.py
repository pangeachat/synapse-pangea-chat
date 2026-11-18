"""Unit tests for what the Can-Do migration reports: ids, counts and error
types only, and the operator script's account of what was left undone
(the docstrings of migrate_course_can_dos.py and course_progress.py)."""

from __future__ import annotations

import importlib.util
import os
import unittest
from typing import Any, Dict, List
from unittest import mock

from synapse_pangea_chat.migrate_course_can_dos import report as reporting

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "migrate_course_can_dos.py",
)
_spec = importlib.util.spec_from_file_location("migrate_course_can_dos", _SCRIPT)
assert _spec is not None and _spec.loader is not None
script: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

NOTE = "a teacher's own words about unit three"


class TestReport(unittest.TestCase):
    def test_a_failure_is_reported_as_its_type_never_its_message(self) -> None:
        sentry = mock.Mock()
        with mock.patch.object(reporting, "sentry_sdk", sentry), self.assertLogs(
            reporting.logger, "ERROR"
        ) as logs:
            reporting.report_failure(
                "migrate_course_can_dos: a write failed",
                ValueError(NOTE),
                room_id="!r:x",
            )

        sentry.capture_message.assert_called_once_with(
            "migrate_course_can_dos: a write failed",
            level="error",
            extras={"error_type": "ValueError", "room_id": "!r:x"},
        )
        self.assertNotIn(NOTE, "\n".join(logs.output))
        self.assertIn("error_type=ValueError room_id=!r:x", logs.output[0])
        sentry.capture_exception.assert_not_called()

    def test_without_sentry_a_report_is_still_logged(self) -> None:
        with mock.patch.object(reporting, "sentry_sdk", None), self.assertLogs(
            reporting.logger, "WARNING"
        ) as logs:
            reporting.report("migrate_course_progress: records left unmoved", refused=2)
        self.assertIn("refused=2", logs.output[0])


def _record(outcome: str, **extra: Any) -> Dict[str, Any]:
    return {
        "room_id": "!a:x",
        "course_id": "!c:x",
        "user_id": "@l:x",
        "outcome": outcome,
        "keys_moved": 0,
        "completions_kept": 0,
        **extra,
    }


class TestOperatorScript(unittest.TestCase):
    def test_what_is_left_undone(self) -> None:
        for record, left in (
            (_record("rewrite"), False),
            (_record("unchanged"), False),
            (_record("unreadable"), False),
            (_record("unchanged", old_keys_left=1), True),
            # Counted on their own, outside the limit (coordinator, 2026-11-15).
            (_record("deactivated"), False),
            (_record("not_local"), True),
            (_record("not_learners_own"), True),
            (_record("refused"), True),
            (_record("failed"), True),
        ):
            with self.subTest(record=record):
                self.assertIs(script.unresolved(record), left)

    def test_the_summary_names_every_record_left(self) -> None:
        records: List[Dict[str, Any]] = [
            _record("rewrite", completions_restored=1),
            _record("unchanged", old_keys_left=1),
            _record("unreadable"),
        ]
        text = "\n".join(script.summarize_progress(records, dry_run=True))
        self.assertIn("1 restored", text)
        self.assertIn("1 with an old key left in place", text)
        self.assertIn("1 unreadable", text)

    def test_deactivated_learners_are_listed_but_do_not_fail_the_apply(self) -> None:
        text = "\n".join(
            script.summarize_progress([_record("deactivated")], dry_run=False)
        )
        self.assertIn("1 deactivated (outside the limit)", text)

    def test_the_space_summary_counts_what_combining_leaves_out(self) -> None:
        report = {
            "spaces": [
                {
                    "room_id": "!s:x",
                    "status": 200,
                    "body": {
                        "writes": ["pangea.course_missions"],
                        "course_missions": {
                            "combined": [
                                {
                                    "slots_given_up": ["b1"],
                                    "due_dates_dropped": 2,
                                    "cities_dropped": 1,
                                    "fields_dropped": ["extra"],
                                    "hidden_now_shown": 3,
                                }
                            ]
                        },
                        "teacher_mode": {
                            "pooled": [],
                            "unpinned_siblings": [{"lo_id": "M", "from": ["a2"]}],
                            "pin_values_dropped": 0,
                        },
                    },
                },
                {
                    "room_id": "!t:x",
                    "status": 200,
                    "body": {
                        "writes": [],
                        "course_missions": {
                            "outcome": "ignored_by_readers",
                            "old_ids_left": 2,
                        },
                    },
                },
            ]
        }
        text = "\n".join(script.summarize_report(report))
        self.assertIn(
            "combining leaves out 2 due dates, 1 cities, 1 other fields, "
            "and shows 3 hidden activities",
            text,
        )
        self.assertIn("1 with an unpinned old Can-Do", text)
        self.assertIn("1 left with old ids: !t:x", text)


if __name__ == "__main__":
    unittest.main()
