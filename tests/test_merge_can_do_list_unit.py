"""A course's Can-Do list brought in line with its quest without losing what
learners and teachers have in it (set_course_plan's ``keep_progress``)."""

import copy
import unittest

from synapse_pangea_chat.set_course_plan.merge_can_do_list import (
    is_teacher_list,
    losses,
    merge_can_do_list,
    shown_ids,
    totals,
)


def _list(*missions, **top):
    return {"version": 1, "revision": 4, "missions": list(missions), **top}


def _m(lo_id, shown, **extra):
    return {"lo_id": lo_id, "activity_ids": list(shown), **extra}


class TestFollowTheQuest(unittest.TestCase):
    def test_dropped_choices_move_to_removed_and_the_new_ones_show(self) -> None:
        current = _list(_m("lo-a", ["a", "b", "c", "d"]))
        built = _list(_m("lo-a", ["a", "e"]))

        merged = merge_can_do_list(current, built)

        self.assertEqual(
            merged.content["missions"],
            [_m("lo-a", ["a", "e"], removed_activity_ids=["b", "c", "d"])],
        )
        (entry,) = merged.entries
        self.assertEqual(entry["lo_id"], "lo-a")
        self.assertEqual(entry["status"], "follows_quest")
        self.assertEqual(entry["shown_before"], 4)
        self.assertEqual(entry["shown_after"], 2)
        self.assertEqual(entry["moved_to_removed"], 3)
        self.assertEqual(merged.lost, [])

    def test_removed_ids_are_kept_and_one_chosen_again_shows_again(self) -> None:
        current = _list(_m("lo-a", ["a"], removed_activity_ids=["b", "c"]))
        built = _list(_m("lo-a", ["b"]))

        merged = merge_can_do_list(current, built)

        self.assertEqual(
            merged.content["missions"],
            [_m("lo-a", ["b"], removed_activity_ids=["c", "a"])],
        )
        self.assertEqual(merged.entries[0]["back_to_shown"], 1)
        self.assertEqual(merged.entries[0]["moved_to_removed"], 1)

    def test_no_overlap_shows_the_quest_and_credits_every_old_id(self) -> None:
        current = _list(_m("lo-a", ["x", "y"]))
        built = _list(_m("lo-a", ["s1", "s2"]))

        merged = merge_can_do_list(current, built)

        self.assertEqual(
            merged.content["missions"],
            [_m("lo-a", ["s1", "s2"], removed_activity_ids=["x", "y"])],
        )
        self.assertEqual(merged.lost, [])

    def test_an_unchanged_entry_writes_no_empty_removed_list(self) -> None:
        current = _list(_m("lo-a", ["a", "b"]))
        merged = merge_can_do_list(current, _list(_m("lo-a", ["a", "b"])))
        self.assertEqual(merged.content["missions"], [_m("lo-a", ["a", "b"])])
        self.assertEqual(merged.entries[0]["moved_to_removed"], 0)

    def test_hidden_or_unlistable_old_ids_are_credited_like_dropped_ones(
        self,
    ) -> None:
        # The builder lists only what the course listing lists; an old id the
        # review hid is never in it, so it moves to removed and keeps counting.
        current = _list(_m("lo-a", ["hidden-1", "kept", "private-2"]))
        merged = merge_can_do_list(current, _list(_m("lo-a", ["kept", "new"])))
        self.assertEqual(
            merged.content["missions"],
            [
                _m(
                    "lo-a",
                    ["kept", "new"],
                    removed_activity_ids=["hidden-1", "private-2"],
                )
            ],
        )
        self.assertEqual(merged.lost, [])

    def test_an_entry_the_quest_has_nothing_listable_for_is_left_as_it_is(
        self,
    ) -> None:
        # Never an empty entry written silently: the course keeps what it has
        # and the dry run names the entry.
        mission = _m("lo-a", ["x"], removed_activity_ids=["y"], note="Ours")
        merged = merge_can_do_list(_list(mission), _list(_m("lo-a", [])))
        self.assertEqual(merged.content["missions"], [mission])
        (entry,) = merged.entries
        self.assertEqual(entry["status"], "quest_empty")
        self.assertEqual(entry["moved_to_removed"], 0)
        self.assertEqual(merged.lost, [])


class TestWhatTheCourseKeeps(unittest.TestCase):
    def test_due_dates_time_zone_removed_can_dos_and_unknown_keys_are_kept(
        self,
    ) -> None:
        gone = {"lo_id": "lo-z", "activity_ids": ["z1"], "shown_count": 1}
        current = _list(
            _m("lo-a", ["a", "b"], due_at="2026-12-01T00:00:00Z", extra=1),
            time_zone="America/Chicago",
            removed_missions=[gone],
            future_field={"k": "v"},
        )
        built = _list(_m("lo-a", ["a"]), _m("lo-z", ["z1"]))

        merged = merge_can_do_list(current, built)

        self.assertEqual(merged.content["time_zone"], "America/Chicago")
        self.assertEqual(merged.content["removed_missions"], [gone])
        self.assertEqual(merged.content["future_field"], {"k": "v"})
        self.assertEqual(merged.content["version"], 1)
        self.assertEqual(
            merged.content["missions"],
            [
                _m(
                    "lo-a",
                    ["a"],
                    due_at="2026-12-01T00:00:00Z",
                    extra=1,
                    removed_activity_ids=["b"],
                )
            ],
        )
        # The quest's lo-z is one the course removed: not brought back.
        self.assertIn(
            {"lo_id": "lo-z", "status": "removed_by_course"},
            [{k: e[k] for k in ("lo_id", "status")} for e in merged.entries],
        )
        self.assertEqual(merged.lost, [])

    def test_the_courses_note_is_kept_and_the_quests_fills_a_missing_one(
        self,
    ) -> None:
        current = _list(
            _m("lo-a", ["a"], note="Ours"), _m("lo-b", ["b"]), _m("lo-c", ["c"])
        )
        built = _list(
            _m("lo-a", ["a"], note="Quest's"),
            _m("lo-b", ["b"], note="Quest's b"),
            _m("lo-c", ["c"]),
        )

        merged = merge_can_do_list(current, built)

        notes = {m["lo_id"]: m.get("note") for m in merged.content["missions"]}
        self.assertEqual(notes, {"lo-a": "Ours", "lo-b": "Quest's b", "lo-c": None})
        self.assertEqual(
            [e["note"] for e in merged.entries], ["course_kept", "from_quest", None]
        )

    def test_a_can_do_the_quest_no_longer_has_is_kept_in_place(self) -> None:
        own = _m("lo-own", ["o1"], note="Ours", due_at="2026-12-01T00:00:00Z")
        current = _list(_m("lo-a", ["a"]), own, _m("lo-b", ["b"]))
        built = _list(_m("lo-a", ["a"]), _m("lo-b", ["b2"]))

        merged = merge_can_do_list(current, built)

        self.assertEqual(
            [m["lo_id"] for m in merged.content["missions"]],
            ["lo-a", "lo-own", "lo-b"],
        )
        self.assertEqual(merged.content["missions"][1], own)
        self.assertEqual(merged.entries[1]["status"], "course_only")
        self.assertEqual(merged.lost, [])

    def test_a_can_do_new_to_the_quest_is_added_after_the_courses(self) -> None:
        merged = merge_can_do_list(
            _list(_m("lo-a", ["a"])), _list(_m("lo-new", ["n"]), _m("lo-a", ["a"]))
        )
        self.assertEqual(
            merged.content["missions"], [_m("lo-a", ["a"]), _m("lo-new", ["n"])]
        )
        self.assertEqual(merged.entries[1]["status"], "added")

    def test_the_current_list_is_not_changed(self) -> None:
        current = _list(_m("lo-a", ["a", "b"], removed_activity_ids=["c"]))
        before = copy.deepcopy(current)
        merge_can_do_list(current, _list(_m("lo-a", ["d"])))
        self.assertEqual(current, before)


class TestTotals(unittest.TestCase):
    """Per room, as the app shows it: an id counts as shown only when the
    listing lists it."""

    def test_shown_before_counts_listable_ids_only(self) -> None:
        current = _list(
            _m("lo-a", ["h1", "h2", "k"]),
            _m("lo-b", ["h3"]),
            _m("lo-own", ["o1"]),
        )
        built = _list(_m("lo-a", ["k", "n"]), _m("lo-b", []))
        merged = merge_can_do_list(current, built)

        result = totals(current, merged, listable={"k", "o1"})

        self.assertEqual(
            result,
            {
                "entries": 3,
                "shown_before": 2,  # k under lo-a, o1 under lo-own
                "shown_after": 3,  # k, n (the quest's listed choices), o1
                "moved_to_removed": 2,  # h1, h2; lo-b is left as it is
                "empty_before": 1,  # lo-b showed nothing listable
                "empty_after": 1,  # lo-b: its quest entry lists nothing
            },
        )

    def test_unknown_listability_is_reported_as_unknown(self) -> None:
        current = _list(_m("lo-a", ["a"]))
        merged = merge_can_do_list(current, _list(_m("lo-a", ["a"])))
        result = totals(current, merged, listable=None)
        self.assertIsNone(result["shown_before"])
        self.assertIsNone(result["empty_before"])
        self.assertEqual(result["shown_after"], 1)

    def test_shown_ids_are_every_shown_id_once(self) -> None:
        self.assertEqual(
            shown_ids(_list(_m("lo-a", ["a", "b"]), _m("lo-b", ["b", "c"]))),
            ["a", "b", "c"],
        )


class TestNothingLost(unittest.TestCase):
    """ "Nothing lost" is computed from the two lists, so it is said only when
    it is true, whatever the merge did."""

    _CURRENT = _list(
        _m("lo-a", ["a", "b"], due_at="2026-12-01T00:00:00Z", note="Ours"),
        _m("lo-b", ["c"]),
        time_zone="UTC",
    )

    def test_a_faithful_merge_loses_nothing(self) -> None:
        after = _list(
            _m(
                "lo-a",
                ["x"],
                removed_activity_ids=["a", "b"],
                due_at="2026-12-01T00:00:00Z",
                note="Ours",
            ),
            _m("lo-b", ["c"]),
            time_zone="UTC",
        )
        self.assertEqual(losses(self._CURRENT, after), [])

    def test_every_kind_of_loss_is_named(self) -> None:
        after = _list(_m("lo-a", ["a"]))
        self.assertEqual(
            sorted(losses(self._CURRENT, after)),
            sorted(
                [
                    "lo-a: activity b no longer credited",
                    "lo-a: due_at changed",
                    "lo-a: note changed",
                    "lo-b: Can-Do dropped",
                    "list: time_zone changed",
                ]
            ),
        )


class TestTeacherList(unittest.TestCase):
    def test_a_list_only_the_builder_writes_is_not_a_teachers(self) -> None:
        self.assertFalse(is_teacher_list(_list(_m("lo-a", ["a"], note="n"))))
        self.assertFalse(is_teacher_list(None))

    def test_any_field_only_content_studio_writes_marks_a_teachers_list(
        self,
    ) -> None:
        for content in (
            _list(_m("lo-a", ["a"]), time_zone="UTC"),
            _list(_m("lo-a", ["a"]), removed_missions=[]),
            _list(_m("lo-a", ["a"], due_at="2026-12-01T00:00:00Z")),
            _list(_m("lo-a", ["a"], removed_activity_ids=["b"])),
        ):
            with self.subTest(content=content):
                self.assertTrue(is_teacher_list(content))


if __name__ == "__main__":
    unittest.main()
