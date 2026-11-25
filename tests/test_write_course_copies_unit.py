"""Unit tests for the operator script that gives course spaces with no Can-Do
list of their own one (scripts/write_course_copies.py, admin-dash-api#472)."""

from __future__ import annotations

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stdout
from typing import Any, Dict, List, Optional
from unittest import mock

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "write_course_copies.py",
)
_spec = importlib.util.spec_from_file_location("write_course_copies", _SCRIPT)
assert _spec is not None and _spec.loader is not None
script: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

HS = "https://matrix.staging.pangea.chat"


def _space(
    quest: Optional[str] = "q1",
    name: str = "My class",
    topic: str = "Mine",
    has_list: bool = False,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = [
        {"type": "m.room.name", "state_key": "", "content": {"name": name}},
        {"type": "m.room.topic", "state_key": "", "content": {"topic": topic}},
    ]
    if quest is not None:
        events.append(
            {"type": "pangea.course_plan", "state_key": "", "content": {"uuid": quest}}
        )
    if has_list:
        events.append(
            {"type": "pangea.course_missions", "state_key": "", "content": {}}
        )
    return events


class TestWriteCourseCopies(unittest.TestCase):
    def _run(
        self,
        rooms: Dict[str, Optional[List[Dict[str, Any]]]],
        commit: bool = False,
        answer: tuple = (200, {"can_do_list": {"built": True, "missions": 3}}),
    ) -> tuple:
        posts: List[Dict[str, Any]] = []

        def call(url: str, token: str, body: Optional[Dict[str, Any]] = None) -> tuple:
            if body is not None:
                posts.append(body)
                return answer
            room = next(
                r for r in rooms if r.replace("!", "%21").replace(":", "%3A") in url
            )
            state = rooms[room]
            return (200, {"state": state}) if state is not None else (500, {})

        with mock.patch.object(script, "_call", side_effect=call), mock.patch.object(
            script, "walk_spaces", return_value=iter(rooms)
        ), mock.patch.object(script.time, "sleep"), redirect_stdout(io.StringIO()):
            code = script.run(HS, "t", commit)
        return code, posts

    def test_only_a_course_with_no_list_is_sent_its_own_quest_name_and_topic(
        self,
    ) -> None:
        code, posts = self._run(
            {
                "!a:x": _space("q1"),
                "!b:x": _space("q2", has_list=True),
                "!c:x": _space(None),
            },
            commit=True,
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            posts,
            [
                {
                    "room_id": "!a:x",
                    "quest_id": "q1",
                    "expected_quest_id": "q1",
                    "title": "My class",
                    "description": "Mine",
                    "dry_run": False,
                }
            ],
        )

    def test_the_default_is_a_dry_run(self) -> None:
        _, posts = self._run({"!a:x": _space()})
        self.assertTrue(posts[0]["dry_run"])

    def test_a_refusal_or_an_unbuilt_list_exits_1(self) -> None:
        unbuilt = (200, {"can_do_list": {"built": False}})
        self.assertEqual(self._run({"!a:x": _space()}, answer=unbuilt)[0], 1)
        self.assertEqual(
            self._run({"!a:x": _space()}, answer=(409, {"error": "x"}))[0], 1
        )

    def test_an_unreadable_or_unnamed_space_exits_1_and_sends_nothing(self) -> None:
        self.assertEqual(self._run({"!a:x": None}), (1, []))
        self.assertEqual(self._run({"!a:x": _space(name="")}), (1, []))

    def test_production_needs_confirmation(self) -> None:
        with mock.patch.dict(os.environ, {"MATRIX_ADMIN_TOKEN": "t"}):
            with self.assertRaises(SystemExit):
                script.main(["--homeserver", "https://matrix.pangea.chat"])


class TestReplaceListedRooms(unittest.TestCase):
    """--rooms: replace the lists the builder made from its newest rows only
    (pangeachat/.github#832), each only while its room still holds the list
    event named in the file."""

    def _run(
        self,
        file_text: str,
        rooms: Dict[str, Optional[List[Dict[str, Any]]]],
        commit: bool = False,
        answer: tuple = (
            200,
            {
                "can_do_list": {
                    "built": True,
                    "current_activities": 400,
                    "rebuilt_activities": 292,
                    "current_outside_selection": 216,
                }
            },
        ),
    ) -> tuple:
        posts: List[Dict[str, Any]] = []

        def call(url: str, token: str, body: Optional[Dict[str, Any]] = None) -> tuple:
            if body is not None:
                posts.append(body)
                return answer
            room = next(
                r for r in rooms if r.replace("!", "%21").replace(":", "%3A") in url
            )
            state = rooms[room]
            return (200, {"state": state}) if state is not None else (500, {})

        out = io.StringIO()
        with mock.patch.object(script, "_call", side_effect=call), mock.patch.object(
            script, "walk_spaces", side_effect=AssertionError("no walk")
        ), mock.patch.object(script.time, "sleep"), redirect_stdout(out):
            code = script.run_replace(HS, "t", script.parse_rooms(file_text), commit)
        return code, posts, out.getvalue()

    def test_the_rooms_file_names_a_room_and_its_list_event(self) -> None:
        self.assertEqual(
            script.parse_rooms("# built since the switch\n!a:x $e1\n\n!b:x  $e2\n"),
            [("!a:x", "$e1"), ("!b:x", "$e2")],
        )
        for bad in ("!a:x", "a:x $e1", "!a:x e1", "!a:x $e1 extra"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                script.parse_rooms(bad)

    def test_each_listed_room_is_sent_a_replacement_of_its_named_list(self) -> None:
        code, posts, out = self._run(
            "!a:x $e1\n", {"!a:x": _space("q1", has_list=True)}
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            posts,
            [
                {
                    "room_id": "!a:x",
                    "quest_id": "q1",
                    "expected_quest_id": "q1",
                    "title": "My class",
                    "description": "Mine",
                    "replace_copy_event_id": "$e1",
                    "dry_run": True,
                }
            ],
        )
        # What changes, per room, before anything is written.
        self.assertIn(
            "!a:x: quest q1: 400 activities now, 292 rebuilt, 216 outside", out
        )

    def test_commit_writes(self) -> None:
        _, posts, _ = self._run(
            "!a:x $e1\n", {"!a:x": _space("q1", has_list=True)}, commit=True
        )
        self.assertFalse(posts[0]["dry_run"])

    def test_a_list_published_since_is_reported_and_exits_1(self) -> None:
        refused = (
            409,
            {
                "error": "The course's Can-Do list is not the one named",
                "current_copy_event_id": "$teacher",
            },
        )
        code, _, out = self._run(
            "!a:x $e1\n", {"!a:x": _space("q1", has_list=True)}, answer=refused
        )
        self.assertEqual(code, 1)
        self.assertIn("published over", out)

    def test_an_unreadable_room_or_one_with_no_quest_sends_nothing(self) -> None:
        code, posts, _ = self._run(
            "!a:x $e1\n!b:x $e2\n", {"!a:x": None, "!b:x": _space(None)}
        )
        self.assertEqual((code, posts), (1, []))


_MERGED = (
    200,
    {
        "can_do_list": {
            "built": True,
            "missions": 2,
            "current_activities": 278,
            "rebuilt_activities": 35,
            "current_outside_selection": 243,
            "entries": [
                {
                    "lo_id": "lo-a",
                    "status": "follows_quest",
                    "shown_before": 30,
                    "shown_after": 4,
                    "moved_to_removed": 26,
                    "back_to_shown": 0,
                    "removed_after": 26,
                    "note": "course_kept",
                    "shows_none": False,
                },
                {
                    "lo_id": "lo-own",
                    "status": "course_only",
                    "shown_before": 3,
                    "shown_after": 3,
                    "moved_to_removed": 0,
                    "back_to_shown": 0,
                    "removed_after": 0,
                    "note": None,
                    "shows_none": False,
                },
            ],
            "lost": [],
            "nothing_lost": True,
        }
    },
)


def _with_list_event(
    events: List[Dict[str, Any]], event_id: str
) -> List[Dict[str, Any]]:
    return [
        {**e, "event_id": event_id} if e["type"] == "pangea.course_missions" else e
        for e in events
    ]


class TestReplaceKeepingProgress(unittest.TestCase):
    """--rooms --keep-progress: follow the trimmed quest, keep every credited
    id, refuse a teacher's list unless the room is named, and leave an undo
    line for every room written."""

    def _run(
        self,
        file_text: str,
        rooms: Dict[str, List[List[Dict[str, Any]]]],
        answer: tuple = _MERGED,
        commit: bool = False,
        allow: frozenset = frozenset(),
    ) -> tuple:
        posts: List[Dict[str, Any]] = []
        reads: Dict[str, int] = {}

        def call(url: str, token: str, body: Optional[Dict[str, Any]] = None) -> tuple:
            if body is not None:
                posts.append(body)
                return answer
            room = next(
                r for r in rooms if r.replace("!", "%21").replace(":", "%3A") in url
            )
            n = reads.get(room, 0)
            reads[room] = n + 1
            return (200, {"state": rooms[room][min(n, len(rooms[room]) - 1)]})

        out = io.StringIO()
        undo = io.StringIO()
        with mock.patch.object(script, "_call", side_effect=call), mock.patch.object(
            script.time, "sleep"
        ), redirect_stdout(out):
            code = script.run_replace(
                HS,
                "t",
                script.parse_rooms(file_text),
                commit,
                keep_progress=True,
                allow_teacher_list=allow,
                undo_out=undo,
            )
        return code, posts, out.getvalue(), undo.getvalue()

    def test_each_entry_is_reported_and_nothing_lost_is_said(self) -> None:
        before = _with_list_event(_space("q1", has_list=True), "$e1")
        code, posts, out, _ = self._run("!a:x $e1\n", {"!a:x": [before]})

        self.assertEqual(code, 0)
        self.assertTrue(posts[0]["keep_progress"])
        self.assertNotIn("allow_teacher_list", posts[0])
        self.assertIn("!a:x: quest q1: 2 Can-Dos, nothing lost", out)
        self.assertIn(
            "lo-a: follows_quest, shown 30 -> 4, 26 moved to removed, "
            "0 shown again, 26 removed in all, note course_kept",
            out,
        )
        self.assertIn("lo-own: course_only, shown 3 -> 3", out)

    def test_a_loss_is_printed_and_never_called_nothing_lost(self) -> None:
        lossy = (
            409,
            {
                "error": "Following the quest would lose part of the course's list",
                "code": "would_lose",
                "lost": ["lo-a: due_at changed"],
            },
        )
        before = _with_list_event(_space("q1", has_list=True), "$e1")
        code, _, out, _ = self._run("!a:x $e1\n", {"!a:x": [before]}, answer=lossy)

        self.assertEqual(code, 1)
        self.assertNotIn("nothing lost", out)
        self.assertIn("lo-a: due_at changed", out)

    def test_a_teachers_list_is_refused_unless_its_room_is_named(self) -> None:
        refused = (
            409,
            {"error": "published by a teacher", "code": "teacher_list"},
        )
        before = _with_list_event(_space("q1", has_list=True), "$e1")
        code, _, out, _ = self._run("!a:x $e1\n", {"!a:x": [before]}, answer=refused)
        self.assertEqual(code, 1)
        self.assertIn("!a:x: refused, a teacher published this list", out)

        _, posts, _, _ = self._run(
            "!a:x $e1\n", {"!a:x": [before]}, allow=frozenset({"!a:x"})
        )
        self.assertTrue(posts[0]["allow_teacher_list"])

    def test_a_commit_leaves_an_undo_line_naming_the_new_and_old_lists(
        self,
    ) -> None:
        before = _with_list_event(_space("q1", has_list=True), "$e1")
        after = _with_list_event(_space("q1", has_list=True), "$merged")
        _, _, out, undo = self._run(
            "!a:x $e1\n", {"!a:x": [before, after]}, commit=True
        )

        self.assertEqual(undo, "!a:x $merged $e1\n")
        self.assertIn("undo: !a:x $merged $e1", out)


class TestUndo(unittest.TestCase):
    """--undo: write each room's earlier list back, only while the room still
    holds the list the file names."""

    def test_the_undo_file_names_the_current_and_the_earlier_list(self) -> None:
        self.assertEqual(
            script.parse_undo("# from the run\n!a:x $merged $e1\n"),
            [("!a:x", "$merged", "$e1")],
        )
        for bad in ("!a:x $merged", "!a:x merged $e1", "!a:x $m $e1 extra"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                script.parse_undo(bad)

    def test_each_room_is_sent_a_restore_of_its_earlier_list(self) -> None:
        posts: List[Dict[str, Any]] = []
        restored = (
            200,
            {"can_do_list": {"restored": "$e1", "previous_revision": 2, "revision": 3}},
        )

        def call(url: str, token: str, body: Optional[Dict[str, Any]] = None) -> tuple:
            if body is not None:
                posts.append(body)
                return restored
            return (200, {"state": _space("q1", has_list=True)})

        out = io.StringIO()
        with mock.patch.object(script, "_call", side_effect=call), mock.patch.object(
            script.time, "sleep"
        ), redirect_stdout(out):
            code = script.run_undo(HS, "t", [("!a:x", "$merged", "$e1")], False)

        self.assertEqual(code, 0)
        self.assertEqual(
            posts,
            [
                {
                    "room_id": "!a:x",
                    "quest_id": "q1",
                    "expected_quest_id": "q1",
                    "title": "My class",
                    "description": "Mine",
                    "replace_copy_event_id": "$merged",
                    "restore_copy_event_id": "$e1",
                    "dry_run": True,
                }
            ],
        )
        self.assertIn(
            "!a:x: $e1 restored over $merged, revision 2 -> 3", out.getvalue()
        )


if __name__ == "__main__":
    unittest.main()
