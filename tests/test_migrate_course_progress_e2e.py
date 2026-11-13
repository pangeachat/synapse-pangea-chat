"""Integration tests for the learner progress migration, against a local
Synapse + PostgreSQL (see course_progress.py's docstring)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import psycopg2
import requests

from .base_e2e import BaseSynapseE2ETest

_PROGRESS = "pangea.course_progress"
_PATH = "/_synapse/client/pangea/v1/migrate_course_progress"
SEEN = "2026-09-10T12:00:00.000Z"
LATER = "2026-10-02T09:30:00.000Z"


def _entry(xp: int, at: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    return {
        "xp": xp,
        "chat": xp,
        "speaking": 0,
        "goals": 0,
        "practice": 0,
        "completed": at is not None or bool(extra),
        "first_completed_at": at,
        **extra,
    }


def _record(can_dos: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "computed_at": "2026-11-01T00:00:00.000Z",
        "app_version": "5.1.0",
        "xp_to_complete": 500,
        "can_dos": can_dos,
    }


class TestMigrateCourseProgress(BaseSynapseE2ETest):
    """Tests for ``POST /_synapse/client/pangea/v1/migrate_course_progress``."""

    def _auth(self, token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _analytics_room(
        self,
        token: str,
        learner_id: str = "",
        teacher_id: Optional[str] = None,
        teacher_power: int = 0,
    ) -> str:
        body: Dict[str, Any] = {
            "visibility": "private",
            "preset": "private_chat",
            "creation_content": {"type": "p.analytics"},
        }
        if teacher_id is not None:
            body["invite"] = [teacher_id]
            body["power_level_content_override"] = {
                "users": {learner_id: 100, teacher_id: teacher_power}
            }
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json=body,
            headers=self._auth(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["room_id"]

    def _room(self, room_id: str) -> str:
        return f"{self.server_url}/_matrix/client/v3/rooms/{quote(room_id, safe='')}"

    def _join(self, token: str, room_id: str) -> None:
        resp = requests.post(
            f"{self._room(room_id)}/join", json={}, headers=self._auth(token)
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def _put(self, token: str, room_id: str, course: str, content: Any) -> str:
        resp = requests.put(
            f"{self._room(room_id)}/state/{_PROGRESS}/{quote(course, safe='')}",
            json=content,
            headers=self._auth(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["event_id"]

    def _current(self, admin: str, room_id: str, course: str) -> Dict[str, Any]:
        resp = requests.get(
            f"{self.server_url}/_synapse/admin/v1/rooms/"
            f"{quote(room_id, safe='')}/state",
            headers=self._auth(admin),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return next(
            e
            for e in resp.json()["state"]
            if e["type"] == _PROGRESS and e["state_key"] == course
        )

    def _run(
        self, admin: str, can_do_map: Dict[str, str], dry_run: bool
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """Every page, two records at a time."""
        found: Dict[Tuple[str, str], Dict[str, Any]] = {}
        after: Optional[List[str]] = None
        for _ in range(100):
            body: Dict[str, Any] = {
                "can_do_map": can_do_map,
                "dry_run": dry_run,
                "limit": 2,
            }
            if after is not None:
                body["after"] = after
            resp = requests.post(
                f"{self.server_url}{_PATH}", json=body, headers=self._auth(admin)
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            for record in resp.json()["records"]:
                found[(record["room_id"], record["course_id"])] = record
            after = resp.json()["next"]
            if after is None:
                return found
        raise AssertionError("paging did not end")

    async def test_records_move_to_merged_can_dos_as_each_learner(self) -> None:
        await self.homeserver()
        (_, admin) = await self.new_user("op", admin=True)
        (teacher_id, teacher) = await self.new_user("teacher")
        tag = self.tag
        old1, old2, merged = f"x1-{tag}", f"x2-{tag}", f"M-{tag}"
        can_do_map = {old1: merged, old2: merged}
        course = f"!course-{tag}:localhost"

        # Ana's app already rewrote her record under the merged id, without
        # the date; her earlier record holds it under the old id.
        (ana_id, ana) = await self.new_user("ana")
        ana_room = self._analytics_room(ana, ana_id, teacher_id)
        self._join(teacher, ana_room)
        self._put(
            ana,
            ana_room,
            course,
            _record({old1: _entry(700, SEEN, xp_to_complete_at_first_completion=500)}),
        )
        self._put(
            ana,
            ana_room,
            course,
            _record(
                {
                    merged: _entry(900, completed_before_tracking=True),
                    "other": _entry(5),
                }
            ),
        )

        # Ben's app has not written since the switch.
        (ben_id, ben) = await self.new_user("ben")
        ben_room = self._analytics_room(ben)
        self._put(
            ben,
            ben_room,
            course,
            _record(
                {old1: _entry(300, LATER), old2: _entry(800, SEEN), "keep": _entry(1)}
            ),
        )

        # Cy is deactivated; Dee's record was sent by someone else; Eve left.
        (cy_id, cy) = await self.new_user("cy")
        cy_room = self._analytics_room(cy, cy_id, teacher_id)
        self._join(teacher, cy_room)
        self._put(cy, cy_room, course, _record({old1: _entry(1, SEEN)}))
        (dee_id, dee) = await self.new_user("dee")
        dee_room = self._analytics_room(dee, dee_id, teacher_id, 100)
        self._join(teacher, dee_room)
        self._put(teacher, dee_room, course, _record({old1: _entry(1, SEEN)}))
        (eve_id, eve) = await self.new_user("eve")
        eve_room = self._analytics_room(eve, eve_id, teacher_id)
        self._join(teacher, eve_room)
        self._put(eve, eve_room, course, _record({old1: _entry(1, SEEN)}))
        resp = requests.post(
            f"{self._room(eve_room)}/leave", json={}, headers=self._auth(eve)
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        resp = requests.post(
            f"{self.server_url}/_synapse/admin/v1/deactivate/{quote(cy_id)}",
            json={"erase": False},
            headers=self._auth(admin),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        mine = {
            name: (room, course)
            for name, room in (
                ("ana", ana_room),
                ("ben", ben_room),
                ("cy", cy_room),
                ("dee", dee_room),
                ("eve", eve_room),
            )
        }
        before = {name: self._current(admin, *key) for name, key in mine.items()}

        # Only server admins may call it.
        body = {"can_do_map": can_do_map, "dry_run": True}
        resp = requests.post(f"{self.server_url}{_PATH}", json=body)
        self.assertEqual(resp.status_code, 401)
        resp = requests.post(
            f"{self.server_url}{_PATH}", json=body, headers=self._auth(ana)
        )
        self.assertEqual(resp.status_code, 403)

        # The dry run reports and writes nothing.
        dry = self._run(admin, can_do_map, dry_run=True)
        outcomes = {name: dry[key]["outcome"] for name, key in mine.items()}
        self.assertEqual(
            outcomes,
            {
                "ana": "rewrite",
                "ben": "rewrite",
                "cy": "deactivated",
                "dee": "not_learners_own",
                "eve": "rewrite",
            },
        )
        self.assertEqual(
            (dry[mine["ana"]]["keys_moved"], dry[mine["ana"]]["completions_kept"]),
            (0, 1),
        )
        self.assertEqual(
            (dry[mine["ben"]]["keys_moved"], dry[mine["ben"]]["completions_kept"]),
            (2, 1),
        )
        for name, key in mine.items():
            self.assertEqual(
                self._current(admin, *key)["event_id"], before[name]["event_id"]
            )

        # The write: each record merged and sent as its learner.
        done = self._run(admin, can_do_map, dry_run=False)
        self.assertEqual(
            {name: done[key]["outcome"] for name, key in mine.items()},
            {**outcomes, "eve": "refused"},
        )
        ana_now = self._current(admin, *mine["ana"])
        self.assertEqual(ana_now["sender"], ana_id)
        self.assertEqual(
            ana_now["content"],
            _record(
                {
                    merged: _entry(900, SEEN, xp_to_complete_at_first_completion=500),
                    "other": _entry(5),
                }
            ),
        )
        ben_now = self._current(admin, *mine["ben"])
        self.assertEqual(ben_now["sender"], ben_id)
        self.assertEqual(
            ben_now["content"]["can_dos"],
            {merged: _entry(800, SEEN), "keep": _entry(1)},
        )
        for name in ("cy", "dee", "eve"):
            self.assertEqual(
                self._current(admin, *mine[name])["event_id"],
                before[name]["event_id"],
            )

        # Silent: nobody in Ana's room is notified of the rewrite, where a
        # message in the same room is.
        resp = requests.put(
            f"{self._room(ana_room)}/send/m.room.message/control-{tag}",
            json={"msgtype": "m.text", "body": "hello"},
            headers=self._auth(ana),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        with psycopg2.connect(self.database_url) as conn, conn.cursor() as cur:
            pushes = []
            for event_id in (ana_now["event_id"], resp.json()["event_id"]):
                cur.execute(
                    "SELECT count(*) FROM event_push_actions WHERE event_id = %s",
                    (event_id,),
                )
                pushes.append(cur.fetchone()[0])
        self.assertEqual(pushes[0], 0)
        self.assertGreater(pushes[1], 0)

        # A second run changes nothing.
        again = self._run(admin, can_do_map, dry_run=False)
        self.assertEqual(again[mine["ana"]]["outcome"], "unchanged")
        self.assertEqual(again[mine["ben"]]["outcome"], "unchanged")
        self.assertEqual(
            self._current(admin, *mine["ana"])["event_id"], ana_now["event_id"]
        )

        # The log carries ids and counts, never a record's content.
        log = "\n".join(self.server_log())
        self.assertIn("migrate_course_progress", log)
        self.assertNotIn(SEEN, log)

    async def test_the_operator_script_runs_every_page(self) -> None:
        await self.homeserver()
        (_, admin) = await self.new_user("op", admin=True)
        (fay_id, fay) = await self.new_user("fay")
        tag = self.tag
        can_do_map = {f"x1-{tag}": f"M-{tag}"}
        course = f"!course-{tag}:localhost"
        room = self._analytics_room(fay)
        self._put(fay, room, course, _record({f"x1-{tag}": _entry(10, SEEN)}))
        path = (
            Path(__file__).resolve().parent.parent
            / "scripts"
            / "migrate_course_can_dos.py"
        )
        spec = importlib.util.spec_from_file_location("migrate_course_can_dos", path)
        assert spec is not None and spec.loader is not None
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)

        dry = script.run_progress(self.server_url, admin, can_do_map, True)
        mine = [r for r in dry if r["course_id"] == course]
        self.assertEqual([r["outcome"] for r in mine], ["rewrite"])
        self.assertEqual(
            script.per_course(mine)[course],
            {"records": 1, "rewrite": 1, "keys_moved": 1, "completions_kept": 1},
        )
        report = {"kind": "spaces", "homeserver": self.server_url}
        with self.assertRaises(SystemExit):
            script._check_report(report, self.server_url, "progress")

        done = script.run_progress(self.server_url, admin, can_do_map, False)
        self.assertEqual(
            [r["changed"] for r in done if r["course_id"] == course], [True]
        )
        self.assertEqual(
            list(self._current(admin, room, course)["content"]["can_dos"]),
            [f"M-{tag}"],
        )
        self.assertEqual(self._current(admin, room, course)["sender"], fay_id)
