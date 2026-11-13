"""Integration tests for the course-space Can-Do migration: the endpoint and
its operator script, against a local Synapse + PostgreSQL
(see migrate_course_can_dos.py's docstring)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests

from .base_e2e import BaseSynapseE2ETest

_COPY = "pangea.course_missions"
_MODE = "pangea.teacher_mode"
_PATH = "/_synapse/client/pangea/v1/migrate_course_can_dos"

MAP = {"lo-a1": "lo-m", "lo-a2": "lo-m", "lo-b1": "lo-m", "lo-z1": "lo-z"}
CITY = {"name": "Lima", "lat_e6": -12046374, "lng_e6": -77042793}
OLD_COPY: Dict[str, Any] = {
    "version": 1,
    "revision": 4,
    "time_zone": "America/Lima",
    "start_date": "2026-09-01",
    "missions": [
        {
            "lo_id": "lo-a1",
            "activity_ids": ["act-1", "act-2"],
            "note": "Unit 3",
            "due_at": "2026-09-10T05:00:00Z",
            "city": CITY,
        },
        {
            "lo_id": "lo-a2",
            "activity_ids": ["act-2", "act-3"],
            "note": "Bring menus",
            "due_at": "2026-09-17T05:00:00Z",
        },
        {"lo_id": "lo-keep", "activity_ids": ["act-k"]},
    ],
    "removed_missions": [
        {"lo_id": "lo-b1", "activity_ids": ["act-9"], "shown_count": 1}
    ],
}
NEW_COPY: Dict[str, Any] = {
    "version": 1,
    "revision": 5,
    "time_zone": "America/Lima",
    "start_date": "2026-09-01",
    "missions": [
        {
            "lo_id": "lo-m",
            "activity_ids": ["act-1", "act-2", "act-3"],
            # The removed B1 Can-Do's activity stays credited, hidden.
            "removed_activity_ids": ["act-9"],
            "note": "Unit 3 / Bring menus",
            "due_at": "2026-09-17T05:00:00Z",
            "city": CITY,
        },
        {"lo_id": "lo-keep", "activity_ids": ["act-k"]},
    ],
}
OLD_MODE = {
    "enabled": False,
    "xp_to_complete_objective": 30,
    "pinned_activities_by_objective": {
        "lo-a1": ["act-1"],
        "lo-a2": ["act-3"],
        "lo-z1": ["act-z"],
    },
}
NEW_MODE = {
    "enabled": False,
    "xp_to_complete_objective": 30,
    "pinned_activities_by_objective": {"lo-m": ["act-1", "act-3"], "lo-z": ["act-z"]},
}


def _load_script() -> Any:
    """scripts/ is not a package; the script is loaded by path."""
    path = (
        Path(__file__).resolve().parent.parent / "scripts" / "migrate_course_can_dos.py"
    )
    spec = importlib.util.spec_from_file_location("migrate_course_can_dos", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ids(state: Dict[tuple, Dict[str, Any]]) -> Dict[tuple, str]:
    """Each state event's id: what changes when anything is written."""
    return {key: event["event_id"] for key, event in state.items()}


class TestMigrateCourseCanDos(BaseSynapseE2ETest):
    """Tests for ``POST /_synapse/client/pangea/v1/migrate_course_can_dos``."""

    # ── helpers ──────────────────────────────────────────────────────

    def _call(self, body: Any, token: Optional[str]) -> requests.Response:
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return requests.post(f"{self.server_url}{_PATH}", json=body, headers=headers)

    def _space(
        self,
        token: str,
        copy: Optional[Dict[str, Any]] = None,
        mode: Optional[Dict[str, Any]] = None,
    ) -> str:
        initial_state = [
            {"type": "pangea.course_plan", "state_key": "", "content": {"uuid": "q"}}
        ]
        for event_type, content in ((_COPY, copy), (_MODE, mode)):
            if content is not None:
                initial_state.append(
                    {"type": event_type, "state_key": "", "content": content}
                )
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json={
                "visibility": "private",
                "preset": "private_chat",
                "creation_content": {"type": "m.space"},
                "initial_state": initial_state,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["room_id"]

    def _state(self, admin_token: str, room_id: str) -> Dict[tuple, Dict[str, Any]]:
        resp = requests.get(
            f"{self.server_url}/_synapse/admin/v1/rooms/{quote(room_id, safe='')}/state",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return {(e["type"], e["state_key"]): e for e in resp.json()["state"]}

    def _put_state(
        self, token: str, room_id: str, event_type: str, content: Dict[str, Any]
    ) -> None:
        resp = requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/"
            f"{quote(room_id, safe='')}/state/{event_type}/",
            json=content,
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    # ── tests ────────────────────────────────────────────────────────

    async def test_dry_run_then_write_as_the_teacher(self) -> None:
        await self.homeserver()
        (operator_id, admin) = await self.new_user("op", admin=True)
        (teacher_id, teacher) = await self.new_user("teacher")
        space = self._space(teacher, copy=OLD_COPY, mode=OLD_MODE)
        before = self._state(admin, space)
        dry = {"room_id": space, "can_do_map": MAP, "dry_run": True}

        # Only server admins may call it, and a bad request is refused.
        self.assertEqual(self._call(dry, None).status_code, 401)
        self.assertEqual(self._call(dry, teacher).status_code, 403)
        for body in (
            {**dry, "can_do_map": {"lo-a1": "lo-m", "lo-m": "lo-n"}},
            {**dry, "room_id": "nope"},
            {key: value for key, value in dry.items() if key != "dry_run"},
        ):
            resp = self._call(body, admin)
            self.assertEqual(resp.status_code, 400, (body, resp.text))
        resp = self._call({**dry, "room_id": "!missing:localhost"}, admin)
        self.assertEqual(resp.status_code, 404, resp.text)

        # The dry run reports what would change and writes nothing.
        expected = {
            "course_missions": before[(_COPY, "")]["event_id"],
            "teacher_mode": before[(_MODE, "")]["event_id"],
        }
        resp = self._call(dry, admin)
        self.assertEqual(resp.status_code, 200, resp.text)
        report = resp.json()
        self.assertEqual(report["expected"], expected)
        self.assertEqual(report["writes"], [_COPY, _MODE])
        self.assertEqual(report["sender"], teacher_id)
        self.assertFalse(report["changed"])
        self.assertEqual(
            report["course_missions"],
            {
                "event_id": expected["course_missions"],
                "outcome": "rewrite",
                "revision": 4,
                "new_revision": 5,
                "ids_changed": 3,
                "combined": [
                    {"lo_id": "lo-m", "from": ["lo-a1", "lo-a2"], "slots_given_up": []}
                ],
                "removed_combined": [],
                "removed_into_missions": [{"lo_id": "lo-m", "from": ["lo-b1"]}],
                "notes_trimmed": [],
                "notes_added": [],
                "no_statement": [],
            },
        )
        self.assertEqual(
            report["teacher_mode"],
            {
                "event_id": expected["teacher_mode"],
                "outcome": "rewrite",
                "keys_changed": 3,
                "pooled": [{"lo_id": "lo-m", "from": ["lo-a1", "lo-a2"]}],
            },
        )
        self.assertEqual(_ids(self._state(admin, space)), _ids(before))

        # The write sends both as the teacher.
        resp = self._call(
            {"room_id": space, "can_do_map": MAP, "expected": expected}, admin
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["changed"])
        after = self._state(admin, space)
        self.assertEqual(after[(_COPY, "")]["content"], NEW_COPY)
        self.assertEqual(after[(_COPY, "")]["sender"], teacher_id)
        self.assertEqual(after[(_MODE, "")]["content"], NEW_MODE)
        self.assertEqual(after[(_MODE, "")]["sender"], teacher_id)
        # Nobody joined and nobody's power changed.
        self.assertNotIn(("m.room.member", operator_id), after)
        self.assertEqual(
            after[("m.room.power_levels", "")]["content"],
            before[("m.room.power_levels", "")]["content"],
        )

        # A second run finds nothing left to change.
        resp = self._call(dry, admin)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["writes"], [])
        self.assertEqual(resp.json()["course_missions"]["outcome"], "unchanged")

        # A space with neither a copy nor pins has nothing to rewrite.
        plain = self._space(teacher)
        resp = self._call({**dry, "room_id": plain}, admin)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["writes"], [])
        self.assertIsNone(resp.json()["course_missions"])

    async def test_a_teacher_edit_after_the_dry_run_is_never_overwritten(
        self,
    ) -> None:
        await self.homeserver()
        (_, admin) = await self.new_user("op", admin=True)
        (_, teacher) = await self.new_user("teacher")
        space = self._space(teacher, copy=OLD_COPY)
        dry = {"room_id": space, "can_do_map": MAP, "dry_run": True}
        expected = self._call(dry, admin).json()["expected"]

        # The teacher publishes from an old draft that still holds old ids.
        edited = {**OLD_COPY, "revision": 5, "missions": OLD_COPY["missions"][1:]}
        self._put_state(teacher, space, _COPY, edited)

        resp = self._call(
            {"room_id": space, "can_do_map": MAP, "expected": expected}, admin
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertFalse(resp.json()["changed"])
        self.assertEqual(self._state(admin, space)[(_COPY, "")]["content"], edited)

        # A fresh dry run maps the teacher's edit, keeping it.
        fresh = self._call(dry, admin).json()
        resp = self._call(
            {"room_id": space, "can_do_map": MAP, "expected": fresh["expected"]}, admin
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        content = self._state(admin, space)[(_COPY, "")]["content"]
        self.assertEqual(content["revision"], 6)
        self.assertEqual(
            content["missions"],
            [
                {
                    "lo_id": "lo-m",
                    "activity_ids": ["act-2", "act-3"],
                    "removed_activity_ids": ["act-9"],
                    "note": "Bring menus",
                    "due_at": "2026-09-17T05:00:00Z",
                },
                {"lo_id": "lo-keep", "activity_ids": ["act-k"]},
            ],
        )

    async def test_a_trimmed_note_is_logged_whole(self) -> None:
        await self.homeserver()
        (_, admin) = await self.new_user("op", admin=True)
        (_, teacher) = await self.new_user("teacher")
        long_note, dropped = "L" * 270, f"dropped-{self.tag}"
        space = self._space(
            teacher,
            copy={
                "version": 1,
                "revision": 1,
                "missions": [
                    {"lo_id": "lo-a1", "activity_ids": [], "note": long_note},
                    {"lo_id": "lo-a2", "activity_ids": [], "note": dropped},
                ],
            },
        )
        dry = {"room_id": space, "can_do_map": MAP, "dry_run": True}
        report = self._call(dry, admin).json()
        self.assertEqual(
            report["course_missions"]["notes_trimmed"],
            [{"lo_id": "lo-m", "note": long_note + " (+1 more)", "dropped": [dropped]}],
        )
        resp = self._call(
            {"room_id": space, "can_do_map": MAP, "expected": report["expected"]}, admin
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(any(dropped in line for line in self.server_log()))

    async def test_an_entry_without_a_note_gains_the_old_wording(self) -> None:
        await self.homeserver()
        (_, admin) = await self.new_user("op", admin=True)
        (_, teacher) = await self.new_user("teacher")
        script = _load_script()
        statements = {
            "lo-a1": "Can order food at a market.",
            "lo-a2": "Can ask what something costs.",
            "lo-z1": "Can read a menu.",
            "lo-m": "Can buy food and drink.",
            "lo-z": "Can read a menu",
        }
        old_copy = {
            "version": 1,
            "revision": 2,
            "missions": [
                {"lo_id": "lo-a1", "activity_ids": ["act-1"]},
                {"lo_id": "lo-a2", "activity_ids": ["act-2"]},
                {"lo_id": "lo-z1", "activity_ids": ["act-z"], "note": "Menus"},
                {"lo_id": "lo-keep", "activity_ids": ["act-k"]},
            ],
        }
        space = self._space(teacher, copy=old_copy)

        report = script.dry_run(self.server_url, admin, MAP, [space], statements)
        copy_report = report["spaces"][0]["body"]["course_missions"]
        self.assertEqual(
            copy_report["notes_added"],
            [
                {
                    "lo_id": "lo-m",
                    "note": "Can order food at a market. / Can ask what something costs.",
                    "from": ["lo-a1", "lo-a2"],
                }
            ],
        )
        self.assertIn(
            "1 entries gain a note from the old wording",
            script.summarize_report(report),
        )
        self.assertEqual(self._state(admin, space)[(_COPY, "")]["content"], old_copy)

        results = script.apply(self.server_url, admin, report)
        self.assertEqual(script.summarize_results(results), ["1 written"])
        content = self._state(admin, space)[(_COPY, "")]["content"]
        self.assertEqual(
            content["missions"],
            [
                {
                    "lo_id": "lo-m",
                    "activity_ids": ["act-1", "act-2"],
                    "note": "Can order food at a market. / Can ask what something costs.",
                },
                {"lo_id": "lo-z", "activity_ids": ["act-z"], "note": "Menus"},
                {"lo_id": "lo-keep", "activity_ids": ["act-k"]},
            ],
        )

        # A second run changes nothing.
        again = script.dry_run(self.server_url, admin, MAP, [space], statements)
        self.assertEqual(again["spaces"][0]["body"]["writes"], [])

    async def test_a_space_with_no_local_instructor_is_refused(self) -> None:
        await self.homeserver()
        (_, admin) = await self.new_user("op", admin=True)
        (_, teacher) = await self.new_user("teacher")
        (student_id, student) = await self.new_user("student")
        space = self._space(teacher, copy=OLD_COPY)
        path = f"{self.server_url}/_matrix/client/v3/rooms/{quote(space, safe='')}"
        for url, token, body in (
            (f"{path}/invite", teacher, {"user_id": student_id}),
            (f"{path}/join", student, {}),
            (f"{path}/leave", teacher, {}),
        ):
            step = requests.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
            self.assertEqual(step.status_code, 200, step.text)
        before = self._state(admin, space)

        dry = {"room_id": space, "can_do_map": MAP, "dry_run": True}
        resp = self._call(dry, admin)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["writes"], [_COPY])
        resp = self._call(
            {"room_id": space, "can_do_map": MAP, "expected": resp.json()["expected"]},
            admin,
        )
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(_ids(self._state(admin, space)), _ids(before))

    async def test_the_operator_script_dry_runs_then_applies(self) -> None:
        await self.homeserver()
        (_, admin) = await self.new_user("op", admin=True)
        (_, teacher) = await self.new_user("teacher")
        script = _load_script()
        old = self._space(teacher, copy=OLD_COPY, mode=OLD_MODE)
        edited = self._space(teacher, copy=OLD_COPY)
        plain = self._space(teacher)

        walked = set(script.walk_spaces(self.server_url, admin))
        self.assertLessEqual({old, edited, plain}, walked)

        report = script.dry_run(self.server_url, admin, MAP, [old, edited, plain])
        self.assertEqual([s["room_id"] for s in report["spaces"]], [old, edited, plain])
        summary = script.summarize_report(report)
        self.assertIn("2 to rewrite", summary)
        self.assertIn("2 entries combined, 0 slots given up", summary)
        self.assertIn("1 pin sets pooled", summary)
        self.assertEqual(self._state(admin, old)[(_COPY, "")]["content"], OLD_COPY)

        # A teacher edits one space between the dry run and the apply.
        teacher_edit = {**OLD_COPY, "revision": 5}
        self._put_state(teacher, edited, _COPY, teacher_edit)

        with self.assertRaises(SystemExit):
            script.apply("http://elsewhere.invalid", admin, report)
        results = script.apply(self.server_url, admin, report)
        self.assertEqual([r["room_id"] for r in results], [old, edited])
        self.assertEqual(
            script.summarize_results(results),
            ["1 written", f"1 skipped, changed since the dry run: {edited}"],
        )
        state = self._state(admin, old)
        self.assertEqual(state[(_COPY, "")]["content"], NEW_COPY)
        self.assertEqual(state[(_MODE, "")]["content"], NEW_MODE)
        self.assertEqual(
            self._state(admin, edited)[(_COPY, "")]["content"], teacher_edit
        )

        # The same report again: the written space has nothing left to change.
        again = script.apply(self.server_url, admin, report)
        self.assertEqual(
            script.summarize_results(again)[:2],
            ["0 written", f"1 already had nothing left to change: {old}"],
        )
