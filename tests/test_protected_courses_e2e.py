"""Protected courses (pangeachat/.github#799) against a real Synapse: the
course management route making a course private, ``knock`` and Protected;
the rules that keep it so and keep the event the module's own; a join by
class code and an approved request each recorded with how they joined; the
member already in it asked; leave and rejoin rows; and the room preview's
flag."""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import quote

import requests

from tests.base_e2e import BaseSynapseE2ETest

P = "/_synapse/client/pangea/v1/"
CODE = "pr0tect"
LIMITS = {
    "rc_joins": {"local": {"per_second": 9999, "burst_count": 9999}},
    "rc_message": {"per_second": 9999, "burst_count": 9999},
    "rc_invites": {
        "per_room": {"per_second": 9999, "burst_count": 9999},
        "per_user": {"per_second": 9999, "burst_count": 9999},
    },
}


class TestProtectedCoursesE2E(BaseSynapseE2ETest):
    def call(
        self,
        method: str,
        path: str,
        token: str,
        body: Any = None,
        params: Optional[Dict[str, str]] = None,
    ) -> requests.Response:
        return requests.request(
            method,
            self.server_url + path,
            json=body,
            params=params,
            headers={"Authorization": "Bearer " + token},
            timeout=60,
        )

    def ok(self, method: str, path: str, token: str, **kw: Any) -> Any:
        resp = self.call(method, path, token, **kw)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def state(self, room: str, event_type: str, key: str = "") -> str:
        return (
            f"/_matrix/client/v3/rooms/{quote(room, safe='')}/state/{event_type}/{key}"
        )

    def ledger(self, room: str, token: str) -> list:
        events = self.ok(
            "GET",
            P + "student_invitations/managed_events",
            token,
            params={"room_id": room},
        )["events"]
        return [(e["user_id"], e["action"], e["via"]) for e in reversed(events)]

    def records(self, room: str, token: str) -> Dict[str, Any]:
        members = self.ok(
            "GET", P + "course_management/members", token, params={"room_id": room}
        )["members"]
        return {m["user_id"]: (m["status"], m["joined_via"]) for m in members}

    async def test_a_protected_course_end_to_end(self) -> None:
        await self.homeserver(
            module_config={
                "protected_courses_enabled": True,
                "room_preview_state_event_types": [],
            },
            synapse_config_overrides=LIMITS,
        )
        teacher, t_token = await self.new_user("teacher")
        early, e_token = await self.new_user("early")
        coder, c_token = await self.new_user("coder")
        asker, a_token = await self.new_user("asker")
        room = self.ok(
            "POST",
            "/_matrix/client/v3/createRoom",
            t_token,
            body={
                "preset": "public_chat",
                "visibility": "public",
                "creation_content": {"type": "m.space"},
            },
        )["room_id"]
        self.ok(
            "PUT",
            self.state(room, "m.room.join_rules"),
            t_token,
            body={"join_rule": "public", "access_code": CODE},
        )
        self.ok("POST", f"/_matrix/client/v3/join/{quote(room)}", e_token, body={})

        # Only the module writes the event, even for the course admin.
        resp = self.call(
            "PUT",
            self.state(room, "pangea.course_management"),
            t_token,
            body={"protected": True},
        )
        self.assertEqual(resp.status_code, 403, resp.text)
        # A student is no course admin.
        resp = self.call(
            "POST",
            P + "course_management/set",
            e_token,
            body={"room_id": room, "protected": True},
        )
        self.assertEqual(resp.status_code, 403, resp.text)

        body = self.ok(
            "POST",
            P + "course_management/set",
            t_token,
            body={"room_id": room, "protected": True},
        )
        self.assertEqual(body["asked"], [early])
        self.assertEqual(
            self.ok("GET", self.state(room, "m.room.join_rules"), t_token),
            {"join_rule": "knock", "access_code": CODE},
        )
        directory = f"/_matrix/client/v3/directory/list/room/{quote(room)}"
        self.assertEqual(self.ok("GET", directory, t_token)["visibility"], "private")
        flag = self.ok("GET", self.state(room, "pangea.course_management"), t_token)
        self.assertTrue(flag["protected"])
        self.assertFalse(flag["outside"]["dms"])

        # It stays private, knock, and Protected.
        for method, path, payload in (
            ("PUT", directory, {"visibility": "public"}),
            ("PUT", self.state(room, "m.room.join_rules"), {"join_rule": "public"}),
        ):
            resp = self.call(method, path, t_token, body=payload)
            self.assertEqual(resp.status_code, 403, (path, resp.text))
        flag_id = self.ok(
            "GET",
            f"/_matrix/client/v3/rooms/{quote(room)}/state",
            t_token,
        )
        (flag_event,) = [e for e in flag_id if e["type"] == "pangea.course_management"]
        resp = self.call(
            "PUT",
            f"/_matrix/client/v3/rooms/{quote(room)}/redact/"
            f"{quote(flag_event['event_id'])}/t1",
            t_token,
            body={},
        )
        self.assertEqual(resp.status_code, 403, resp.text)

        # The class code: the module's invitation names it, the join manages.
        self.ok("POST", P + "knock_with_code", c_token, body={"access_code": CODE})
        self.ok("POST", f"/_matrix/client/v3/join/{quote(room)}", c_token, body={})
        # An approved request.
        self.ok("POST", f"/_matrix/client/v3/knock/{quote(room)}", a_token, body={})
        self.ok(
            "POST",
            f"/_matrix/client/v3/rooms/{quote(room)}/invite",
            t_token,
            body={"user_id": asker},
        )
        self.ok("POST", f"/_matrix/client/v3/join/{quote(room)}", a_token, body={})
        # No member names their own way in.
        resp = self.call(
            "PUT",
            self.state(room, "m.room.member", asker),
            a_token,
            body={"membership": "join", "org.pangea.joined_via": "claim"},
        )
        self.assertEqual(resp.status_code, 403, resp.text)

        # The early member accepts; the code student leaves and comes back.
        self.assertEqual(
            self.ok(
                "POST",
                P + "course_management/answer",
                e_token,
                body={"room_id": room, "accept": True},
            ),
            {"status": "managed"},
        )
        self.ok(
            "POST", f"/_matrix/client/v3/rooms/{quote(room)}/leave", c_token, body={}
        )
        self.ok("POST", P + "knock_with_code", c_token, body={"access_code": CODE})
        self.ok("POST", f"/_matrix/client/v3/join/{quote(room)}", c_token, body={})

        self.assertEqual(
            self.records(room, t_token),
            {
                early: ("managed", "unknown"),
                coder: ("managed", "code"),
                asker: ("managed", "request"),
            },
        )
        self.assertEqual(
            self.ledger(room, t_token),
            [
                (early, "management_asked", "unknown"),
                (coder, "management_started", "code"),
                (asker, "management_started", "request"),
                (early, "management_started", "unknown"),
                (coder, "left", "code"),
                (coder, "rejoined", "code"),
            ],
        )
        self.assertNotIn(teacher, self.records(room, t_token))

        # A control change: one row per managed student.
        self.ok(
            "POST",
            P + "course_management/set",
            t_token,
            body={"room_id": room, "outside": {"calls": True}},
        )
        changed = [r for r in self.ledger(room, t_token) if r[1] == "control_changed"]
        self.assertEqual(len(changed), 3)

        # The preview carries the flag, never the controls.
        preview = self.ok(
            "GET",
            "/_synapse/client/unstable/org.pangea/room_preview",
            c_token,
            params={"rooms": room},
        )["rooms"][room]
        self.assertEqual(
            preview["pangea.course_management"]["default"]["content"],
            {"protected": True},
        )
