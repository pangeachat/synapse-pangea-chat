"""Unit tests for the learner progress migration's merge
(migrate-course-progress.instructions.md)."""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional, Tuple

from synapse.util.frozenutils import freeze

from synapse_pangea_chat.migrate_course_can_dos.course_progress import (
    MAX_LIMIT,
    merge_progress,
    parse_request,
)

MAP = {"x1": "M", "x2": "M", "z1": "Z"}
SEEN = "2026-09-10T12:00:00.000Z"
EARLIER = "2026-09-01T08:00:00.000Z"
LATER = "2026-10-01T08:00:00.000Z"


def _entry(
    xp: int = 100,
    at: Optional[str] = None,
    threshold: Optional[int] = None,
    replayed: bool = False,
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "xp": xp,
        "chat": xp,
        "speaking": 0,
        "goals": 0,
        "practice": 0,
        "completed": at is not None or replayed,
        "first_completed_at": at,
    }
    if threshold is not None:
        entry["xp_to_complete_at_first_completion"] = threshold
    if replayed:
        entry["completed_before_tracking"] = True
    return entry


def _record(can_dos: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "computed_at": "2026-11-01T00:00:00.000Z",
        "app_version": "5.1.0",
        "xp_to_complete": 500,
        "can_dos": can_dos,
    }


def _merge(
    can_dos: Dict[str, Any], earlier: Optional[List[Tuple[str, Dict[str, Any]]]] = None
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    rewrite = merge_progress(_record(can_dos), earlier or [], MAP)
    return rewrite.content, rewrite.report


class TestMergeProgress(unittest.TestCase):
    def test_an_old_key_moves_to_its_merged_can_do_in_its_place(self) -> None:
        old = _entry(700, SEEN, 500)
        content, report = _merge({"a": _entry(1), "x1": old, "b": _entry(2)})

        assert content is not None
        self.assertEqual(list(content["can_dos"]), ["a", "M", "b"])
        self.assertEqual(content["can_dos"]["M"], old)
        self.assertEqual(report, {"keys_moved": 1, "completions_kept": 1})
        # The rest of the record is left as it is.
        self.assertEqual(
            {k: v for k, v in content.items() if k != "can_dos"},
            {k: v for k, v in _record({}).items() if k != "can_dos"},
        )

    def test_the_earliest_completion_wins_with_its_threshold(self) -> None:
        content, report = _merge(
            {
                "x1": _entry(600, LATER, 800),
                "x2": _entry(900, EARLIER, 400, replayed=True),
            }
        )

        assert content is not None
        merged = content["can_dos"]["M"]
        # Figures from the entry with the most XP; the completion from the
        # earliest, with the threshold and replay mark stored beside it.
        self.assertEqual(merged["xp"], 900)
        self.assertEqual(merged["first_completed_at"], EARLIER)
        self.assertEqual(merged["xp_to_complete_at_first_completion"], 400)
        self.assertIs(merged["completed_before_tracking"], True)
        self.assertEqual(list(content["can_dos"]), ["M"])
        self.assertEqual(report["keys_moved"], 2)

    def test_a_completion_the_app_already_dropped_comes_back_from_history(
        self,
    ) -> None:
        # The app rewrote the record under the merged id, with no date.
        current = _entry(900, None, replayed=True)
        content, report = _merge(
            {"M": current},
            [
                (
                    "x1",
                    {
                        "first_completed_at": SEEN,
                        "xp_to_complete_at_first_completion": 500,
                    },
                )
            ],
        )

        assert content is not None
        merged = content["can_dos"]["M"]
        self.assertEqual(merged["xp"], 900)
        self.assertEqual(merged["first_completed_at"], SEEN)
        self.assertEqual(merged["xp_to_complete_at_first_completion"], 500)
        self.assertNotIn("completed_before_tracking", merged)
        self.assertEqual(report, {"keys_moved": 0, "completions_kept": 1})

    def test_an_earlier_date_under_the_merged_id_is_kept(self) -> None:
        content, report = _merge(
            {"M": _entry(900, EARLIER, 400)},
            [("x1", {"first_completed_at": SEEN})],
        )

        self.assertIsNone(content)
        self.assertEqual(report, {"keys_moved": 0, "completions_kept": 0})

    def test_a_stale_app_writing_old_ids_back_is_undone(self) -> None:
        # After a first run, an app holding the old outline wrote x1 again
        # with no date; the run's own version holds the date under M.
        content, _ = _merge(
            {"x1": _entry(950, None, replayed=True)},
            [
                (
                    "x1",
                    {
                        "first_completed_at": SEEN,
                        "xp_to_complete_at_first_completion": 500,
                    },
                ),
                (
                    "M",
                    {
                        "first_completed_at": SEEN,
                        "xp_to_complete_at_first_completion": 500,
                    },
                ),
            ],
        )

        assert content is not None
        self.assertEqual(list(content["can_dos"]), ["M"])
        self.assertEqual(content["can_dos"]["M"]["xp"], 950)
        self.assertEqual(content["can_dos"]["M"]["first_completed_at"], SEEN)

    def test_a_re_run_changes_nothing(self) -> None:
        first, _ = _merge(
            {"x1": _entry(700, SEEN, 500), "z1": _entry(10)},
            [("x2", {"first_completed_at": LATER})],
        )
        assert first is not None
        history: List[Tuple[str, Dict[str, Any]]] = [
            (
                "x1",
                {"first_completed_at": SEEN, "xp_to_complete_at_first_completion": 500},
            ),
            ("x2", {"first_completed_at": LATER}),
            (
                "M",
                {"first_completed_at": SEEN, "xp_to_complete_at_first_completion": 500},
            ),
        ]
        again = merge_progress(first, history, MAP)

        self.assertIsNone(again.content)

    def test_records_without_old_or_merged_ids_and_unreadable_ones(self) -> None:
        unrelated, _ = _merge({"a": _entry(1)})
        self.assertIsNone(unrelated)
        self.assertIsNone(merge_progress({"can_dos": None}, [], MAP).content)
        # An old entry that is not an entry is left as it is.
        content, report = _merge({"x1": "nonsense", "z1": _entry(5)})
        assert content is not None
        self.assertEqual(content["can_dos"], {"x1": "nonsense", "Z": _entry(5)})
        self.assertEqual(report["keys_moved"], 1)

    def test_an_unreadable_date_or_threshold_never_wins(self) -> None:
        content, _ = _merge(
            {
                "M": _entry(900, LATER, 500),
                "x1": {
                    **_entry(10, EARLIER),
                    "xp_to_complete_at_first_completion": "x",
                },
            },
            [("x2", {"first_completed_at": "yesterday"})],
        )

        assert content is not None
        self.assertEqual(content["can_dos"]["M"]["first_completed_at"], LATER)

    def test_frozen_content_reads_like_plain_json(self) -> None:
        rewrite = merge_progress(
            freeze(_record({"x1": _entry(700, SEEN, 500)})), [], MAP
        )

        assert rewrite.content is not None
        self.assertEqual(rewrite.content["can_dos"]["M"]["first_completed_at"], SEEN)


class TestParseRequest(unittest.TestCase):
    def test_pages_and_limits(self) -> None:
        base = {"can_do_map": {"a": "b"}, "dry_run": True}
        req, error = parse_request(base)
        assert req is not None, error
        self.assertEqual(req.after, ("", ""))
        req, error = parse_request({**base, "after": ["!r:x", "!c:x"], "limit": 5})
        assert req is not None, error
        self.assertEqual((req.after, req.limit), (("!r:x", "!c:x"), 5))
        for bad in (
            {**base, "after": "!r:x"},
            {**base, "limit": 0},
            {**base, "limit": MAX_LIMIT + 1},
            {**base, "dry_run": "yes"},
            {**base, "can_do_map": {"a": "b", "b": "c"}},
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_request(bad)[0])


if __name__ == "__main__":
    unittest.main()
