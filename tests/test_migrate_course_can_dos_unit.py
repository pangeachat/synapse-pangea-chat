"""Unit tests for the course-space Can-Do migration's rewrite rules
(migrate-course-can-dos.instructions.md)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from synapse.util.frozenutils import freeze

from synapse_pangea_chat.migrate_course_can_dos.migrate_course_can_dos import (
    COPY,
    MAX_NOTE_UTF16,
    PINS,
    PINS_KEY,
    copy_problem,
    join_notes,
    parse_can_do_map,
    parse_request,
    plan_space,
    rewrite_copy,
    rewrite_pins,
    utf16_len,
)

# Old A1/A2/B1 Can-Dos of one family, and a second family's old row.
MAP = {"a1": "M", "a2": "M", "b1": "M", "z1": "Z"}


def _copy(
    missions: List[Dict[str, Any]], removed: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    content: Dict[str, Any] = {"version": 1, "revision": 4, "missions": missions}
    if removed is not None:
        content["removed_missions"] = removed
    return content


def _ids(content: Dict[str, Any]) -> List[str]:
    return [entry["lo_id"] for entry in content["missions"]]


class TestRewriteCopy(unittest.TestCase):
    def test_ids_are_replaced_and_every_other_field_kept(self) -> None:
        content: Dict[str, Any] = {
            "version": 1,
            "revision": 4,
            "time_zone": "Europe/Madrid",
            "start_date": "2026-09-01",
            "holidays": ["2026-10-12"],
            "studio_field": {"kept": True},
            "missions": [
                {
                    "lo_id": "a1",
                    "activity_ids": ["x", "y"],
                    "removed_activity_ids": ["w"],
                    "deleted_activity_ids": ["w"],
                    "note": "Unit 3",
                    "due_at": "2026-09-10T21:00:00Z",
                    "city": {"name": "Madrid", "lat_e6": 1, "lng_e6": 2},
                    "future": 1,
                },
                {"lo_id": "keep", "activity_ids": ["k"]},
            ],
        }
        rewrite = rewrite_copy(content, MAP)

        assert rewrite.content is not None
        self.assertEqual(
            rewrite.content,
            {
                **content,
                "revision": 5,
                "missions": [
                    {**content["missions"][0], "lo_id": "M"},
                    content["missions"][1],
                ],
            },
        )
        self.assertEqual(rewrite.report["ids_changed"], 1)
        self.assertEqual(rewrite.report["combined"], [])
        # The input is not changed.
        self.assertEqual(content["missions"][0]["lo_id"], "a1")
        self.assertEqual(content["revision"], 4)

    def test_a_copy_without_old_ids_is_unchanged(self) -> None:
        rewrite = rewrite_copy(_copy([{"lo_id": "M", "activity_ids": []}]), MAP)

        self.assertIsNone(rewrite.content)
        self.assertEqual(rewrite.report["ids_changed"], 0)

    def test_neighbours_merge_into_one_span_with_the_last_date(self) -> None:
        rewrite = rewrite_copy(
            _copy(
                [
                    {"lo_id": "before", "activity_ids": []},
                    {
                        "lo_id": "a1",
                        "activity_ids": ["x", "y"],
                        "due_at": "2026-09-10T21:00:00Z",
                    },
                    {
                        "lo_id": "a2",
                        "activity_ids": ["y", "z"],
                        "due_at": "2026-09-17T21:00:00Z",
                    },
                    {"lo_id": "after", "activity_ids": []},
                ]
            ),
            MAP,
        )

        assert rewrite.content is not None
        self.assertEqual(_ids(rewrite.content), ["before", "M", "after"])
        merged = rewrite.content["missions"][1]
        self.assertEqual(merged["activity_ids"], ["x", "y", "z"])
        self.assertEqual(merged["due_at"], "2026-09-17T21:00:00Z")
        self.assertEqual(
            rewrite.report["combined"],
            [{"lo_id": "M", "from": ["a1", "a2"], "slots_given_up": []}],
        )

    def test_a_later_occurrence_gives_up_its_slot(self) -> None:
        rewrite = rewrite_copy(
            _copy(
                [
                    {
                        "lo_id": "a1",
                        "activity_ids": ["x"],
                        "due_at": "2026-09-10T21:00:00Z",
                    },
                    {"lo_id": "between", "activity_ids": ["q"]},
                    {
                        "lo_id": "b1",
                        "activity_ids": ["v", "x"],
                        "removed_activity_ids": ["u"],
                        "due_at": "2026-10-01T21:00:00Z",
                    },
                ]
            ),
            MAP,
        )

        assert rewrite.content is not None
        self.assertEqual(_ids(rewrite.content), ["M", "between"])
        merged = rewrite.content["missions"][0]
        self.assertEqual(merged["activity_ids"], ["x", "v"])
        self.assertEqual(merged["removed_activity_ids"], ["u"])
        # The first keeps its own date; the far one's date leaves with its slot.
        self.assertEqual(merged["due_at"], "2026-09-10T21:00:00Z")
        self.assertEqual(
            rewrite.report["combined"],
            [{"lo_id": "M", "from": ["a1", "b1"], "slots_given_up": ["b1"]}],
        )

    def test_a_span_and_a_far_occurrence_together(self) -> None:
        rewrite = rewrite_copy(
            _copy(
                [
                    {"lo_id": "a1", "activity_ids": ["x"], "due_at": "D1"},
                    {"lo_id": "a2", "activity_ids": ["y"], "due_at": "D2"},
                    {"lo_id": "z1", "activity_ids": ["q"]},
                    {"lo_id": "b1", "activity_ids": ["v"], "due_at": "D3"},
                ]
            ),
            MAP,
        )

        assert rewrite.content is not None
        self.assertEqual(_ids(rewrite.content), ["M", "Z"])
        merged = rewrite.content["missions"][0]
        self.assertEqual(merged["activity_ids"], ["x", "y", "v"])
        self.assertEqual(merged["due_at"], "D2")
        self.assertEqual(rewrite.report["combined"][0]["slots_given_up"], ["b1"])

    def test_a_span_ending_undated_keeps_the_latest_date_in_it(self) -> None:
        rewrite = rewrite_copy(
            _copy(
                [
                    {"lo_id": "a1", "activity_ids": [], "due_at": "D1"},
                    {"lo_id": "a2", "activity_ids": []},
                ]
            ),
            MAP,
        )

        assert rewrite.content is not None
        self.assertEqual(rewrite.content["missions"][0]["due_at"], "D1")

    def test_shown_wins_over_hidden_and_deleted_stays_within_hidden(self) -> None:
        rewrite = rewrite_copy(
            _copy(
                [
                    {
                        "lo_id": "a1",
                        "activity_ids": ["x"],
                        "removed_activity_ids": ["y", "d"],
                        "deleted_activity_ids": ["d"],
                    },
                    {
                        "lo_id": "a2",
                        "activity_ids": ["y"],
                        "removed_activity_ids": ["x", "e"],
                        "deleted_activity_ids": ["x"],
                    },
                ]
            ),
            MAP,
        )

        assert rewrite.content is not None
        merged = rewrite.content["missions"][0]
        self.assertEqual(merged["activity_ids"], ["x", "y"])
        self.assertEqual(merged["removed_activity_ids"], ["d", "e"])
        self.assertEqual(merged["deleted_activity_ids"], ["d"])
        self.assertIsNone(copy_problem(rewrite.content))

    def test_notes_join_and_city_comes_from_the_first_that_has_one(self) -> None:
        city = {"name": "Lima", "lat_e6": 1, "lng_e6": 2}
        rewrite = rewrite_copy(
            _copy(
                [
                    {"lo_id": "a1", "activity_ids": [], "note": "Unit 3 "},
                    {"lo_id": "a2", "activity_ids": [], "note": "  "},
                    {"lo_id": "b1", "activity_ids": [], "note": "Unit 3", "city": city},
                    {"lo_id": "z1", "activity_ids": [], "note": "Menus"},
                ]
            ),
            {**MAP, "z1": "M"},
        )

        assert rewrite.content is not None
        merged = rewrite.content["missions"][0]
        self.assertEqual(merged["note"], "Unit 3 / Menus")
        self.assertEqual(merged["city"], city)
        self.assertEqual(rewrite.report["notes_trimmed"], [])

    def test_a_joined_note_over_the_limit_keeps_the_first_and_logs_the_rest(
        self,
    ) -> None:
        first, second, third = "A" * 200, "B" * 100, "C" * 10
        rewrite = rewrite_copy(
            _copy(
                [
                    {"lo_id": "a1", "activity_ids": [], "note": first},
                    {"lo_id": "a2", "activity_ids": [], "note": second},
                    {"lo_id": "b1", "activity_ids": [], "note": third},
                ]
            ),
            MAP,
        )

        assert rewrite.content is not None
        self.assertEqual(rewrite.content["missions"][0]["note"], first + " (+2 more)")
        self.assertEqual(
            rewrite.report["notes_trimmed"],
            [{"lo_id": "M", "note": first + " (+2 more)", "dropped": [second, third]}],
        )

    def test_removed_missions_map_and_combine(self) -> None:
        rewrite = rewrite_copy(
            _copy(
                [{"lo_id": "keep", "activity_ids": ["k"]}],
                [
                    {"lo_id": "a1", "activity_ids": ["x", "h"], "shown_count": 1},
                    {"lo_id": "other", "activity_ids": [], "shown_count": 0},
                    {"lo_id": "a2", "activity_ids": ["y", "h", "x"], "shown_count": 1},
                    {"lo_id": "b1", "activity_ids": ["s", "t"], "shown_count": 0},
                ],
            ),
            MAP,
        )

        assert rewrite.content is not None
        self.assertEqual(
            rewrite.content["removed_missions"],
            [
                # What was shown at removal first (x, then y), then what was
                # hidden; x was hidden in a2 but shown in a1, so it stays shown.
                {
                    "lo_id": "M",
                    "activity_ids": ["x", "y", "h", "s", "t"],
                    "shown_count": 2,
                },
                {"lo_id": "other", "activity_ids": [], "shown_count": 0},
            ],
        )
        self.assertEqual(
            rewrite.report["removed_combined"],
            [{"lo_id": "M", "from": ["a1", "a2", "b1"]}],
        )
        self.assertIsNone(copy_problem(rewrite.content))

    def test_a_removed_can_do_the_course_still_shows_stays_hidden_there(
        self,
    ) -> None:
        rewrite = rewrite_copy(
            _copy(
                [{"lo_id": "a1", "activity_ids": ["x"], "removed_activity_ids": ["w"]}],
                [{"lo_id": "a2", "activity_ids": ["x", "r"], "shown_count": 2}],
            ),
            MAP,
        )

        assert rewrite.content is not None
        self.assertEqual(
            rewrite.content["missions"],
            [{"lo_id": "M", "activity_ids": ["x"], "removed_activity_ids": ["w", "r"]}],
        )
        self.assertNotIn("removed_missions", rewrite.content)
        self.assertEqual(
            rewrite.report["removed_into_missions"], [{"lo_id": "M", "from": ["a2"]}]
        )

    def test_every_rewrite_passes_the_apps_parse(self) -> None:
        missions = [
            {"lo_id": lo, "activity_ids": [lo + "-x"], "note": "N" * 280}
            for lo in ("a1", "z1", "a2", "b1")
        ]
        rewrite = rewrite_copy(_copy(missions), MAP)

        assert rewrite.content is not None
        self.assertIsNone(copy_problem(rewrite.content))
        self.assertEqual(_ids(rewrite.content), ["M", "Z"])


class TestJoinNotes(unittest.TestCase):
    def test_none_and_blank_give_no_note(self) -> None:
        self.assertEqual(join_notes([None, "", "  "]), (None, []))

    def test_a_single_note_is_kept_as_written(self) -> None:
        self.assertEqual(join_notes([" Unit 3 ", None]), (" Unit 3 ", []))

    def test_a_long_first_note_is_cut_to_fit_with_the_count(self) -> None:
        note, dropped = join_notes(["A" * 280, "B"])

        assert note is not None
        self.assertEqual(utf16_len(note), MAX_NOTE_UTF16)
        self.assertTrue(note.endswith("… (+1 more)"))
        kept = note[: -len("… (+1 more)")]
        self.assertEqual(dropped, ["A" * (280 - len(kept)), "B"])

    def test_the_limit_counts_utf16_units_as_the_app_does(self) -> None:
        emoji = "\U0001f600"  # two UTF-16 units, one Python character
        note, _ = join_notes([emoji * 140, "B"])

        assert note is not None
        # 134 whole emoji, the ellipsis and the count: 279 units, 145 characters.
        self.assertEqual(note, emoji * 134 + "… (+1 more)")
        self.assertEqual(utf16_len(note), 279)


class TestRewritePins(unittest.TestCase):
    def test_keys_are_renamed_and_other_fields_kept(self) -> None:
        content: Dict[str, Any] = {
            "enabled": True,
            "xp_to_complete_objective": 40,
            "activities_to_unlock_topic": None,
            "unknown": "kept",
            PINS_KEY: {"a1": ["x", "y"], "keep": ["k"], "a2": ["z", "x"], "z1": []},
        }
        rewrite = rewrite_pins(content, MAP)

        assert rewrite.content is not None
        self.assertEqual(
            rewrite.content,
            {
                **content,
                PINS_KEY: {"M": ["x", "y", "z"], "keep": ["k"], "Z": []},
            },
        )
        self.assertEqual(rewrite.report["keys_changed"], 3)
        self.assertEqual(
            rewrite.report["pooled"], [{"lo_id": "M", "from": ["a1", "a2"]}]
        )
        self.assertEqual(content[PINS_KEY]["a1"], ["x", "y"])

    def test_no_old_keys_or_no_pins_is_unchanged(self) -> None:
        contents: List[Dict[str, Any]] = [
            {"enabled": False},
            {PINS_KEY: None},
            {PINS_KEY: {"M": ["x"]}},
        ]
        for content in contents:
            with self.subTest(content=content):
                self.assertIsNone(rewrite_pins(content, MAP).content)


class TestCopyProblem(unittest.TestCase):
    def test_what_the_app_refuses_is_named(self) -> None:
        good = _copy([{"lo_id": "a1", "activity_ids": ["x"]}])
        self.assertIsNone(copy_problem(good))
        bad: List[Any] = [
            None,
            {**good, "version": True},
            {**good, "version": 2},
            {**good, "revision": 0},
            {**good, "time_zone": ""},
            _copy([{"lo_id": "a1", "activity_ids": []}] * 2),
            _copy([{"lo_id": "a1"}]),
            _copy([{"lo_id": "a1", "activity_ids": [1]}]),
            _copy([{"lo_id": "a1", "activity_ids": [], "note": "N" * 281}]),
            _copy([], [{"lo_id": "a1", "activity_ids": ["x"], "shown_count": 2}]),
            _copy([], [{"lo_id": "a1", "activity_ids": ["x"]}]),
        ]
        for content in bad:
            with self.subTest(content=content):
                self.assertIsNotNone(copy_problem(content))


class TestParsing(unittest.TestCase):
    def test_the_map_is_one_hop_of_ids(self) -> None:
        self.assertEqual(parse_can_do_map({"a": "b"}), ({"a": "b"}, None))
        values: List[Any] = [None, {}, {"a": ""}, {"a": 1}, {"a": "b", "b": "c"}]
        for value in [*values, {"a": "a"}]:
            with self.subTest(value=value):
                self.assertIsNone(parse_can_do_map(value)[0])

    def test_a_write_needs_what_its_dry_run_read(self) -> None:
        base = {"room_id": "!r:x", "can_do_map": {"a": "b"}}
        dry, error = parse_request({**base, "dry_run": True})
        assert dry is not None, error
        self.assertIsNone(dry.expected)

        self.assertIsNone(parse_request(base)[0])
        self.assertIsNone(parse_request({**base, "expected": {COPY: "$e"}})[0])
        write, error = parse_request({**base, "expected": {COPY: "$e", PINS: None}})
        assert write is not None, error
        self.assertEqual(write.expected, {COPY: "$e", PINS: None})
        self.assertFalse(write.dry_run)

        self.assertIsNone(parse_request({**base, "room_id": "r"})[0])
        self.assertIsNone(parse_request({**base, "dry_run": "yes"})[0])


def _event(event_id: str, sender: str, content: Dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        event_id=event_id, sender=sender, content=content, room_id="!r:x"
    )


def _state(**events: SimpleNamespace) -> Dict[Any, Any]:
    state: Dict[Any, Any] = {
        ("m.room.power_levels", ""): SimpleNamespace(
            content={"users": {"@teacher:x": 100, "@student:x": 0}}
        ),
    }
    if "copy" in events:
        state[("pangea.course_missions", "")] = events["copy"]
    if "mode" in events:
        state[("pangea.teacher_mode", "")] = events["mode"]
    return state


class TestPlanSpace(unittest.TestCase):
    def test_neither_event_means_nothing_to_rewrite(self) -> None:
        plan = plan_space(_state(), MAP)

        self.assertEqual(plan.writes, [])
        self.assertEqual(plan.answer["expected"], {COPY: None, PINS: None})
        self.assertIsNone(plan.answer[COPY])
        self.assertIsNone(plan.answer[PINS])

    def test_copy_first_then_pins(self) -> None:
        plan = plan_space(
            _state(
                copy=_event(
                    "$c", "@teacher:x", _copy([{"lo_id": "a1", "activity_ids": []}])
                ),
                mode=_event("$m", "@teacher:x", {PINS_KEY: {"a1": ["x"]}}),
            ),
            MAP,
        )

        self.assertEqual(
            [event_type for event_type, _ in plan.writes],
            ["pangea.course_missions", "pangea.teacher_mode"],
        )
        self.assertEqual(plan.answer["expected"], {COPY: "$c", PINS: "$m"})
        self.assertEqual(plan.answer[COPY]["outcome"], "rewrite")
        self.assertEqual(plan.answer[COPY]["revision"], 4)
        self.assertEqual(plan.answer[COPY]["new_revision"], 5)
        self.assertEqual(plan.answer[PINS]["outcome"], "rewrite")

    def test_frozen_event_content_is_read_like_plain_json(self) -> None:
        # With a check_event_allowed callback registered (blocked_join_gate),
        # Synapse freezes events: lists arrive as tuples.
        plan = plan_space(
            _state(
                copy=_event(
                    "$c",
                    "@teacher:x",
                    freeze(_copy([{"lo_id": "a1", "activity_ids": ["x"]}])),
                ),
                mode=_event(
                    "$m",
                    "@teacher:x",
                    freeze({PINS_KEY: {"a1": ["x"], "a2": ["y"]}}),
                ),
            ),
            MAP,
        )

        self.assertEqual(plan.answer[COPY]["outcome"], "rewrite")
        writes = dict(plan.writes)
        self.assertEqual(
            writes["pangea.course_missions"]["missions"],
            [{"lo_id": "M", "activity_ids": ["x"]}],
        )
        self.assertEqual(writes["pangea.teacher_mode"][PINS_KEY], {"M": ["x", "y"]})

    def test_a_copy_readers_ignore_is_left_alone(self) -> None:
        old_copy = _copy([{"lo_id": "a1", "activity_ids": []}])
        cases = {
            "student sender": _event("$c", "@student:x", old_copy),
            "malformed": _event("$c", "@teacher:x", {**old_copy, "version": 9}),
        }
        for name, event in cases.items():
            with self.subTest(name):
                plan = plan_space(_state(copy=event), MAP)
                self.assertEqual(plan.writes, [])
                self.assertEqual(plan.answer[COPY]["outcome"], "ignored_by_readers")
                self.assertEqual(plan.answer["expected"][COPY], "$c")


if __name__ == "__main__":
    unittest.main()
