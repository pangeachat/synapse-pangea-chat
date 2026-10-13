"""Integration tests for the set_course_plan endpoint.

Uses ``BaseSynapseE2ETest`` to spin up a local Synapse + PostgreSQL instance
with the ``PangeaChat`` module loaded.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

from .base_e2e import BaseSynapseE2ETest

_COURSE_PLAN = "pangea.course_plan"


class TestSetCoursePlanEndpoint(BaseSynapseE2ETest):
    """Tests for ``POST /_synapse/client/pangea/v1/set_course_plan``."""

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

    def _create_course_space(self, access_token: str, plan: Dict[str, Any]) -> str:
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json={
                "visibility": "private",
                "preset": "private_chat",
                "creation_content": {"type": "m.space"},
                "initial_state": [
                    {"type": _COURSE_PLAN, "state_key": "", "content": plan}
                ],
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
        await self.homeserver()
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

        # A dry run names the sender and writes nothing.
        resp = self._call({**switch, "dry_run": True}, admin_token)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["sender"], teacher_id)
        self.assertFalse(resp.json()["changed"])
        state = self._admin_state(admin_token, space)
        self.assertEqual(state[(_COURSE_PLAN, "")]["content"]["uuid"], "quest-old")

        # The real write: new quest, same language, sent as the teacher.
        resp = self._call(switch, admin_token)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            resp.json(),
            {
                "room_id": space,
                "previous_quest_id": "quest-old",
                "quest_id": "quest-new",
                "sender": teacher_id,
                "changed": True,
                "dry_run": False,
            },
        )
        after = self._admin_state(admin_token, space)
        plan = after[(_COURSE_PLAN, "")]
        self.assertEqual(plan["content"], {"uuid": "quest-new", "l2": "es"})
        self.assertEqual(plan["sender"], teacher_id)

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

        # The legacy plan-id spelling is read, then normalised to uuid.
        legacy = self._create_course_space(
            teacher_token, {"course_plan_id": "legacy-old", "l2": "fr"}
        )
        resp = self._call(
            {
                "room_id": legacy,
                "quest_id": "quest-new",
                "expected_quest_id": "legacy-old",
            },
            admin_token,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        content = self._admin_state(admin_token, legacy)[(_COURSE_PLAN, "")]
        self.assertEqual(content["content"], {"uuid": "quest-new", "l2": "fr"})

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
            orphan_after[("m.room.power_levels", "")]["content"],
            orphan_before[("m.room.power_levels", "")]["content"],
        )
