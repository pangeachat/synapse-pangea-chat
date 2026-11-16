"""Integration tests for the stale course progress guard, against a local
Synapse + PostgreSQL (synapse_pangea_chat/stale_progress_guard/guard.py)."""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import quote

import requests

from .base_e2e import BaseSynapseE2ETest

_PROGRESS = "pangea.course_progress"
_PATH = "/_synapse/client/pangea/v1/retired_can_dos"
SEEN = "2026-09-10T12:00:00.000Z"
LATER = "2026-11-16T09:30:00.000Z"


def _entry(xp: int, at: Optional[str] = None) -> Dict[str, Any]:
    return {
        "xp": xp,
        "chat": xp,
        "speaking": 0,
        "goals": 0,
        "practice": 0,
        "completed": at is not None,
        "first_completed_at": at,
    }


def _record(can_dos: Dict[str, Any], computed_at: str) -> Dict[str, Any]:
    return {
        "computed_at": computed_at,
        "app_version": "5.1.0",
        "xp_to_complete": 500,
        "can_dos": can_dos,
    }


class TestStaleProgressGuard(BaseSynapseE2ETest):
    def _auth(self, token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _room(self, room_id: str) -> str:
        return f"{self.server_url}/_matrix/client/v3/rooms/{quote(room_id, safe='')}"

    def _analytics_room(self, token: str) -> str:
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json={
                "visibility": "private",
                "preset": "private_chat",
                "creation_content": {"type": "p.analytics"},
            },
            headers=self._auth(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["room_id"]

    def _put_state(
        self, token: str, room_id: str, event_type: str, key: str, content: Any
    ) -> None:
        resp = requests.put(
            f"{self._room(room_id)}/state/{event_type}/{quote(key, safe='')}",
            json=content,
            headers=self._auth(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def _stored(self, token: str, room_id: str, event_type: str, key: str) -> Any:
        resp = requests.get(
            f"{self._room(room_id)}/state/{event_type}/{quote(key, safe='')}",
            headers=self._auth(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _load(self, admin: str, can_do_map: Dict[str, str]) -> Dict[str, Any]:
        resp = requests.put(
            f"{self.server_url}{_PATH}",
            json={"can_do_map": can_do_map},
            headers=self._auth(admin),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    async def test_a_stale_write_keeps_the_migrated_record(self) -> None:
        await self.homeserver()
        (_, admin) = await self.new_user("op", admin=True)
        (_, learner) = await self.new_user("ana")
        tag = self.tag
        old1, old2, merged, other = f"x1-{tag}", f"x2-{tag}", f"M-{tag}", f"Z-{tag}"
        self._load(admin, {old1: merged, old2: merged})
        room = self._analytics_room(learner)
        course = f"!course-{tag}:localhost"

        # The record as the migration left it.
        migrated = _record(
            {merged: _entry(700, SEEN), other: _entry(900, SEEN)},
            "2026-11-14T00:00:00.000Z",
        )
        self._put_state(learner, room, _PROGRESS, course, migrated)
        self.assertEqual(self._stored(learner, room, _PROGRESS, course), migrated)

        # An app still holding the old outline writes old ids, no dates, and
        # leaves out a Can-Do it does not know.
        stale = _record({old1: _entry(300), old2: _entry(200)}, LATER)
        self._put_state(learner, room, _PROGRESS, course, stale)

        stored = self._stored(learner, room, _PROGRESS, course)
        self.assertEqual(
            stored["can_dos"], {merged: _entry(700, SEEN), other: _entry(900, SEEN)}
        )
        self.assertEqual(stored["computed_at"], LATER)

    async def test_a_completion_made_on_a_stale_app_is_kept(self) -> None:
        await self.homeserver()
        (_, admin) = await self.new_user("op", admin=True)
        (_, learner) = await self.new_user("ben")
        tag = self.tag
        old1, merged = f"x1-{tag}", f"M-{tag}"
        self._load(admin, {old1: merged})
        room = self._analytics_room(learner)
        course = f"!course-{tag}:localhost"
        self._put_state(
            learner, room, _PROGRESS, course, _record({merged: _entry(400)}, SEEN)
        )

        self._put_state(
            learner, room, _PROGRESS, course, _record({old1: _entry(520, LATER)}, LATER)
        )

        stored = self._stored(learner, room, _PROGRESS, course)
        self.assertEqual(list(stored["can_dos"]), [merged])
        self.assertTrue(stored["can_dos"][merged]["completed"])
        self.assertEqual(stored["can_dos"][merged]["first_completed_at"], LATER)

    async def test_a_current_write_and_writes_with_no_map_pass_as_sent(self) -> None:
        await self.homeserver()
        (_, admin) = await self.new_user("op", admin=True)
        (_, learner) = await self.new_user("cai")
        tag = self.tag
        old1, merged = f"x1-{tag}", f"M-{tag}"
        self._load(admin, {old1: merged})
        room = self._analytics_room(learner)
        course = f"!course-{tag}:localhost"
        self._put_state(
            learner,
            room,
            _PROGRESS,
            course,
            _record({merged: _entry(400), f"Q-{tag}": _entry(5)}, SEEN),
        )

        # A write from an up-to-date app may drop a Can-Do (the teacher
        # removed it): it is stored as sent.
        current = _record({merged: _entry(450)}, LATER)
        self._put_state(learner, room, _PROGRESS, course, current)
        self.assertEqual(self._stored(learner, room, _PROGRESS, course), current)

        # With the map cleared, nothing is corrected.
        resp = requests.delete(f"{self.server_url}{_PATH}", headers=self._auth(admin))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), {"count": 0, "can_do_map": {}})
        stale = _record({old1: _entry(1)}, LATER)
        self._put_state(learner, room, _PROGRESS, course, stale)
        self.assertEqual(self._stored(learner, room, _PROGRESS, course), stale)

    async def test_a_course_list_naming_a_retired_id_is_accepted(self) -> None:
        await self.homeserver()
        (_, admin) = await self.new_user("op", admin=True)
        (_, teacher) = await self.new_user("dee")
        tag = self.tag
        old1, merged = f"x1-{tag}", f"M-{tag}"
        self._load(admin, {old1: merged})
        room = self._analytics_room(teacher)
        content = {"missions": [{"lo_id": old1, "activity_ids": []}]}

        self._put_state(teacher, room, "pangea.course_missions", "", content)

        self.assertEqual(
            self._stored(teacher, room, "pangea.course_missions", ""), content
        )
        self.assertTrue(
            any(
                "pangea.course_missions" in line and "retired Can-Dos" in line
                for line in self.server_log()
            )
        )

    async def test_the_map_endpoint_is_for_server_admins(self) -> None:
        await self.homeserver()
        (_, admin) = await self.new_user("op", admin=True)
        (_, learner) = await self.new_user("eve")
        tag = self.tag

        resp = requests.put(
            f"{self.server_url}{_PATH}",
            json={"can_do_map": {f"x1-{tag}": f"M-{tag}"}},
            headers=self._auth(learner),
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        resp = requests.get(f"{self.server_url}{_PATH}")
        self.assertEqual(resp.status_code, 401, resp.text)
        resp = requests.put(
            f"{self.server_url}{_PATH}",
            json={"can_do_map": {"a": "b", "b": "c"}},
            headers=self._auth(admin),
        )
        self.assertEqual(resp.status_code, 400, resp.text)

        loaded = self._load(admin, {f"x1-{tag}": f"M-{tag}", f"x2-{tag}": f"M-{tag}"})
        self.assertEqual(loaded["count"], 2)
        resp = requests.get(f"{self.server_url}{_PATH}", headers=self._auth(admin))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json(), loaded)
