"""Integration tests for the set_course_plan endpoint.

Uses ``BaseSynapseE2ETest`` to spin up a local Synapse + PostgreSQL instance
with the ``PangeaChat`` module loaded.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import requests

from .base_e2e import BaseSynapseE2ETest

_COURSE_PLAN = "pangea.course_plan"
_COURSE_COPY = "pangea.course_missions"
_NEW_COPY_MISSIONS = [
    {"lo_id": "lo-b", "activity_ids": ["act-b"]},
    {"lo_id": "lo-a", "activity_ids": ["act-a"]},
]


class _QuestCmsHandler(BaseHTTPRequestHandler):
    """Answers the two reads the new quest's course copy is built from; any
    other quest is unknown, so its copy cannot be built."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        code: int
        body: Dict[str, Any]
        if (
            self.headers.get("Authorization")
            != "service-users API-Key test-cms-api-key"
        ):
            code, body = 401, {"errors": [{"message": "unauthorized"}]}
        elif path == "/api/quest-plans/quest-new":
            code, body = 200, {
                "id": "quest-new",
                "req": {"target_language": "es"},
                "res": {
                    "learning_objective_sequence": [
                        {"learning_objective": "lo-b"},
                        {"learning_objective": "lo-a"},
                    ]
                },
            }
        elif path == "/api/activities-v2":
            code, body = 200, {
                "docs": [
                    {
                        "id": f"row-{lo}",
                        "req": {},
                        "res": {"plan": {"activity_id": f"act-{lo}", "l2": "es"}},
                        "learningObjectiveRefs": [f"lo-{lo}"],
                        "hidden": False,
                        "visibility": "public",
                    }
                    for lo in ("a", "b")
                ]
            }
        else:
            code, body = 404, {"errors": [{"message": "not found"}]}
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class TestSetCoursePlanEndpoint(BaseSynapseE2ETest):
    """Tests for ``POST /_synapse/client/pangea/v1/set_course_plan``."""

    _cms: HTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._cms = HTTPServer(("127.0.0.1", 0), _QuestCmsHandler)
        threading.Thread(target=cls._cms.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._cms.shutdown()
        cls._cms.server_close()
        super().tearDownClass()

    # ── helpers ──────────────────────────────────────────────────────

    def _call(self, body: Any, access_token: Optional[str]) -> requests.Response:
        headers = (
            {"Authorization": f"Bearer {access_token}"}
            if access_token is not None
            else {}
        )
        return requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/set_course_plan",
            json=body,
            headers=headers,
        )

    def _create_course_space(
        self,
        access_token: str,
        plan: Dict[str, Any],
        name: str = "Old course",
        topic: str = "Old text",
        copy: Optional[Dict[str, Any]] = None,
    ) -> str:
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json={
                "visibility": "private",
                "preset": "private_chat",
                "name": name,
                "topic": topic,
                "creation_content": {"type": "m.space"},
                "initial_state": [
                    {"type": _COURSE_PLAN, "state_key": "", "content": plan}
                ]
                + (
                    [{"type": _COURSE_COPY, "state_key": "", "content": copy}]
                    if copy is not None
                    else []
                ),
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["room_id"]

    def _admin_state(self, admin_token: str, room_id: str) -> Dict[tuple, Dict]:
        resp = requests.get(
            f"{self.server_url}/_synapse/admin/v1/rooms/{quote(room_id, safe='')}/state",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return {(e["type"], e["state_key"]): e for e in resp.json()["state"]}

    # ── tests ────────────────────────────────────────────────────────

    async def test_operator_switches_a_course_plan_as_the_teacher(self) -> None:
        port = self._cms.server_address[1]
        await self.homeserver(
            module_config={"cms_base_url": f"http://127.0.0.1:{port}"}
        )
        (operator_id, admin_token) = await self.new_user("op", admin=True)
        (teacher_id, teacher_token) = await self.new_user("teacher")

        space = self._create_course_space(
            teacher_token, {"uuid": "quest-old", "l2": "es"}
        )
        before = self._admin_state(admin_token, space)
        switch = {
            "room_id": space,
            "quest_id": "quest-new",
            "expected_quest_id": "quest-old",
            "title": "New course",
            "description": "New text",
        }

        # Only server admins may call it.
        self.assertEqual(self._call(switch, None).status_code, 401)
        self.assertEqual(self._call(switch, teacher_token).status_code, 403)

        # A malformed request and a room with no course plan are refused.
        resp = self._call({**switch, "room_id": "not-a-room"}, admin_token)
        self.assertEqual(resp.status_code, 400, resp.text)
        plain_room = await self.create_private_room(teacher_token)
        resp = self._call({**switch, "room_id": plain_room}, admin_token)
        self.assertEqual(resp.status_code, 404, resp.text)

        # A space that no longer points where the operator expects is left alone.
        resp = self._call({**switch, "expected_quest_id": "stale"}, admin_token)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(resp.json()["current_quest_id"], "quest-old")

        # A dry run names the sender, reports the current name and topic and
        # the copy it would write, and writes nothing.
        all_four = [_COURSE_COPY, _COURSE_PLAN, "m.room.name", "m.room.topic"]
        first_copy = {
            "built": True,
            "previous_revision": None,
            "revision": 1,
            "missions": 2,
        }
        resp = self._call({**switch, "dry_run": True}, admin_token)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["sender"], teacher_id)
        self.assertEqual(resp.json()["previous_name"], "Old course")
        self.assertEqual(resp.json()["previous_topic"], "Old text")
        self.assertEqual(resp.json()["changes"], all_four)
        self.assertEqual(resp.json()["course_copy"], first_copy)
        self.assertFalse(resp.json()["changed"])
        state = self._admin_state(admin_token, space)
        self.assertEqual(state[(_COURSE_PLAN, "")]["content"]["uuid"], "quest-old")
        self.assertEqual(state[("m.room.name", "")]["content"]["name"], "Old course")
        self.assertNotIn((_COURSE_COPY, ""), state)

        # The real write: new quest, same language, the quest's title and
        # description, all sent as the teacher.
        resp = self._call(switch, admin_token)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            resp.json(),
            {
                "room_id": space,
                "previous_quest_id": "quest-old",
                "quest_id": "quest-new",
                "previous_name": "Old course",
                "previous_topic": "Old text",
                "changes": all_four,
                "course_copy": first_copy,
                "sender": teacher_id,
                "changed": True,
                "dry_run": False,
            },
        )
        after = self._admin_state(admin_token, space)
        copy = after[(_COURSE_COPY, "")]
        self.assertEqual(
            copy["content"],
            {"version": 1, "revision": 1, "missions": _NEW_COPY_MISSIONS},
        )
        self.assertEqual(copy["sender"], teacher_id)
        plan = after[(_COURSE_PLAN, "")]
        self.assertEqual(plan["content"], {"uuid": "quest-new", "l2": "es"})
        self.assertEqual(plan["sender"], teacher_id)
        name = after[("m.room.name", "")]
        self.assertEqual(name["content"], {"name": "New course"})
        self.assertEqual(name["sender"], teacher_id)
        topic = after[("m.room.topic", "")]["content"]
        self.assertEqual(topic["topic"], "New text")
        self.assertEqual(topic["m.topic"], {"m.text": [{"body": "New text"}]})

        # Nobody joined and nobody's power changed.
        self.assertNotIn(("m.room.member", operator_id), after)
        self.assertEqual(
            after[("m.room.power_levels", "")]["content"],
            before[("m.room.power_levels", "")]["content"],
        )

        # Running it again once it has landed reports unchanged.
        resp = self._call({**switch, "expected_quest_id": "quest-new"}, admin_token)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(resp.json()["changed"])
        self.assertEqual(resp.json()["changes"], [])

        # The legacy plan-id spelling is read, then normalised to uuid. Passing
        # the teacher's own name back as the title keeps it. The course's own
        # copy is replaced at one revision above the old one.
        legacy = self._create_course_space(
            teacher_token,
            {"course_plan_id": "legacy-old", "l2": "fr"},
            name="Period 3",
            copy={"version": 1, "revision": 3, "missions": []},
        )
        resp = self._call(
            {
                **switch,
                "room_id": legacy,
                "expected_quest_id": "legacy-old",
                "title": "Period 3",
            },
            admin_token,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            resp.json()["changes"], [_COURSE_COPY, _COURSE_PLAN, "m.room.topic"]
        )
        legacy_state = self._admin_state(admin_token, legacy)
        self.assertEqual(
            legacy_state[(_COURSE_COPY, "")]["content"],
            {"version": 1, "revision": 4, "missions": _NEW_COPY_MISSIONS},
        )
        self.assertEqual(
            legacy_state[(_COURSE_PLAN, "")]["content"],
            {"uuid": "quest-new", "l2": "fr"},
        )
        self.assertEqual(
            legacy_state[("m.room.name", "")]["content"]["name"], "Period 3"
        )

        # A quest the CMS does not know: the copy is written empty, so readers
        # fall back to the shared quest instead of the old quest's Can-Dos.
        unknown = self._create_course_space(
            teacher_token,
            {"uuid": "quest-old", "l2": "es"},
            copy={"version": 1, "revision": 2, "missions": []},
        )
        resp = self._call(
            {**switch, "room_id": unknown, "quest_id": "quest-missing"}, admin_token
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            resp.json()["course_copy"],
            {"built": False, "previous_revision": 2, "revision": None, "missions": 0},
        )
        unknown_state = self._admin_state(admin_token, unknown)
        self.assertEqual(unknown_state[(_COURSE_COPY, "")]["content"], {})
        self.assertEqual(
            unknown_state[(_COURSE_PLAN, "")]["content"]["uuid"], "quest-missing"
        )

        # With only a student left, nobody can send the event: refused,
        # and nobody is promoted to make it writable. (A room every local
        # member has left has no current state, so it reads as 404.)
        (student_id, student_token) = await self.new_user("student")
        orphan = self._create_course_space(
            teacher_token, {"uuid": "quest-old", "l2": "es"}
        )
        orphan_path = (
            f"{self.server_url}/_matrix/client/v3/rooms/{quote(orphan, safe='')}"
        )
        steps: List[Tuple[str, str, Dict[str, Any]]] = [
            (f"{orphan_path}/invite", teacher_token, {"user_id": student_id}),
            (f"{orphan_path}/join", student_token, {}),
            (f"{orphan_path}/leave", teacher_token, {}),
        ]
        for url, token, body in steps:
            step = requests.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
            self.assertEqual(step.status_code, 200, step.text)
        orphan_before = self._admin_state(admin_token, orphan)
        resp = self._call({**switch, "room_id": orphan}, admin_token)
        self.assertEqual(resp.status_code, 409, resp.text)
        orphan_after = self._admin_state(admin_token, orphan)
        self.assertEqual(
            orphan_after[(_COURSE_PLAN, "")]["content"]["uuid"], "quest-old"
        )
        self.assertEqual(
            orphan_after[("m.room.name", "")]["content"]["name"], "Old course"
        )
        self.assertNotIn((_COURSE_COPY, ""), orphan_after)
        self.assertEqual(
            orphan_after[("m.room.power_levels", "")]["content"],
            orphan_before[("m.room.power_levels", "")]["content"],
        )
