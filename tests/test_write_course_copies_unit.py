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


if __name__ == "__main__":
    unittest.main()
