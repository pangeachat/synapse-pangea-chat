"""Protected courses (pangeachat/.github#799) against a real Synapse: the
course management route making a course private, ``knock`` and Protected;
the rules that keep it so and keep the event the module's own; a join by
class code and an approved request each recorded with how they joined; the
member already in it asked; leave and rejoin rows; and the room preview's
flag."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

import psycopg2
import requests

from tests.base_e2e import BaseSynapseE2ETest
from tests.mock_moderation_server import MockModerationServer

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


CONFIG = {
    "protected_courses_enabled": True,
    "room_preview_state_event_types": [],
    "protected_courses_always_reachable": [],
}
CONTROL = "org.pangea.control"


class ProtectedE2E(BaseSynapseE2ETest):
    """The requests both classes make; no tests of its own."""

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

    def course(self, token: str) -> str:
        """A course space anyone can join, with a course plan."""
        return self.ok(
            "POST",
            "/_matrix/client/v3/createRoom",
            token,
            body={
                "preset": "public_chat",
                "creation_content": {"type": "m.space"},
                "initial_state": [
                    {
                        "type": "pangea.course_plan",
                        "state_key": "",
                        "content": {"uuid": "p"},
                    }
                ],
            },
        )["room_id"]

    def chat(self, space: str, token: str, server: str) -> str:
        """A public chat of ``space``, linked both ways."""
        room = self.ok(
            "POST",
            "/_matrix/client/v3/createRoom",
            token,
            body={
                "preset": "public_chat",
                "initial_state": [
                    {
                        "type": "m.space.parent",
                        "state_key": space,
                        "content": {"via": [server]},
                    }
                ],
            },
        )["room_id"]
        self.ok(
            "PUT",
            self.state(space, "m.space.child", room),
            token,
            body={"via": [server]},
        )
        return room

    def set_controls(self, room: str, token: str, **groups: Any) -> None:
        self.ok(
            "POST",
            P + "course_management/set",
            token,
            body={"room_id": room, **groups},
        )

    def invite_and_join(self, room: str, token: str, user: str, u_token: str) -> None:
        self.ok(
            "POST",
            f"/_matrix/client/v3/rooms/{quote(room)}/invite",
            token,
            body={"user_id": user},
        )
        self.ok("POST", f"/_matrix/client/v3/join/{quote(room)}", u_token, body={})


class TestProtectedCoursesE2E(ProtectedE2E):
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
        await self.homeserver(module_config=CONFIG, synapse_config_overrides=LIMITS)
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

        self.becomes(
            lambda: self.records(room, t_token),
            {
                early: ("managed", "unknown"),
                coder: ("managed", "code"),
                asker: ("managed", "request"),
            },
            "the records",
        )
        self.becomes(
            lambda: self.ledger(room, t_token),
            [
                (early, "management_asked", "unknown"),
                (coder, "management_started", "code"),
                (asker, "management_started", "request"),
                (early, "management_started", "unknown"),
                (coder, "left", "code"),
                (coder, "rejoined", "code"),
            ],
            "the ledger",
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

    def join(self, room: str, token: str) -> requests.Response:
        return self.call(
            "POST", f"/_matrix/client/v3/join/{quote(room)}", token, body={}
        )

    def refused(self, resp: requests.Response, control: str) -> None:
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(resp.json().get(CONTROL), control, resp.text)

    def dm(self, token: str, other: str) -> requests.Response:
        return self.call(
            "POST",
            "/_matrix/client/v3/createRoom",
            token,
            body={"preset": "private_chat", "is_direct": True, "invite": [other]},
        )

    def send(
        self, room: str, token: str, kind: str = "m.room.message"
    ) -> requests.Response:
        return self.call(
            "PUT",
            f"/_matrix/client/v3/rooms/{quote(room)}/send/{kind}/t{time.monotonic_ns()}",
            token,
            body={"msgtype": "m.text", "body": "hi"},
        )

    def becomes(self, read: Callable[[], Any], want: Any, what: str) -> None:
        """Wait for a record the join hook writes after the join returns:
        Synapse runs ``on_new_event`` in the background. The same 10 s as
        ``membership_becomes``, read every half second to stay inside the
        module routes' rate limits."""
        got = None
        for _ in range(20):
            got = read()
            if got == want:
                return
            time.sleep(0.5)
        self.fail(f"{what}: last seen {got!r}, wanted {want!r}")

    def membership_becomes(self, room: str, user: str, token: str, want: str) -> None:
        for _ in range(100):
            got = self.call("GET", self.state(room, "m.room.member", user), token)
            if got.status_code == 200 and got.json().get("membership") == want:
                return
            time.sleep(0.1)
        self.fail(f"{user} never became {want} in {room}")

    async def test_the_controls_end_to_end(self) -> None:
        await self.homeserver(module_config=CONFIG, synapse_config_overrides=LIMITS)
        teacher, t_token = await self.new_user("teacher")
        student, s_token = await self.new_user("student")
        mate, m_token = await self.new_user("mate")
        outsider, o_token = await self.new_user("outsider")
        server = teacher.split(":", 1)[1]

        # The student is already in another course and its chat.
        other = self.course(o_token)
        other_chat = self.chat(other, o_token, server)
        for room in (other, other_chat):
            self.ok("POST", f"/_matrix/client/v3/join/{quote(room)}", s_token, body={})

        # Protected course A; the student and a classmate join by invitation.
        course = self.course(t_token)
        self.set_controls(course, t_token, protected=True)
        for user, token in ((student, s_token), (mate, m_token)):
            self.ok(
                "POST",
                f"/_matrix/client/v3/rooms/{quote(course)}/invite",
                t_token,
                body={"user_id": user},
            )
            self.ok("POST", f"/_matrix/client/v3/join/{quote(course)}", token, body={})

        # Managed: taken out of the other course, which they cannot rejoin,
        # browse, or create.
        self.membership_becomes(other_chat, student, o_token, "leave")
        self.membership_becomes(other, student, o_token, "leave")
        self.refused(self.join(other_chat, s_token), "outside.other_courses")
        self.refused(
            self.call("GET", "/_synapse/client/pangea/v1/public_courses", s_token),
            "outside.other_courses",
        )
        resp = self.call(
            "POST",
            "/_matrix/client/v3/createRoom",
            s_token,
            body={"creation_content": {"type": "m.space"}},
        )
        self.refused(resp, "outside.other_courses")
        mine = self.ok("GET", P + "course_management/mine", s_token)
        self.assertEqual([c["room_id"] for c in mine["courses"]], [course])
        self.assertFalse(mine["controls"]["outside"]["dms"])
        self.assertTrue(mine["controls"]["in_course"]["dms"])

        # A DM with someone outside: refused both ways.
        self.refused(self.dm(o_token, student), "outside.dms")
        self.refused(self.dm(s_token, outsider), "outside.dms")
        # A group room is no DM, whatever order its invitations go out in.
        group = self.call(
            "POST",
            "/_matrix/client/v3/createRoom",
            s_token,
            body={"invite": [outsider, mate]},
        )
        self.assertEqual(group.status_code, 200, group.text)
        # An invitation into an empty room is checked at its join, both ways.
        lone = self.ok("POST", "/_matrix/client/v3/createRoom", s_token, body={})
        self.ok(
            "POST",
            f"/_matrix/client/v3/rooms/{quote(lone['room_id'])}/invite",
            s_token,
            body={"user_id": outsider},
        )
        self.refused(self.join(lone["room_id"], o_token), "outside.dms")
        # The course admin is always reachable.
        admin_dm = self.dm(t_token, student)
        self.assertEqual(admin_dm.status_code, 200, admin_dm.text)
        admin_dm_id = admin_dm.json()["room_id"]
        self.ok(
            "POST", f"/_matrix/client/v3/join/{quote(admin_dm_id)}", s_token, body={}
        )
        self.assertEqual(self.send(admin_dm_id, s_token).status_code, 200)

        # A classmate DM is allowed until the in-course control is turned off.
        mate_dm = self.dm(m_token, student)
        self.assertEqual(mate_dm.status_code, 200, mate_dm.text)
        mate_dm_id = mate_dm.json()["room_id"]
        self.ok(
            "POST", f"/_matrix/client/v3/join/{quote(mate_dm_id)}", s_token, body={}
        )
        self.assertEqual(self.send(mate_dm_id, m_token).status_code, 200)
        self.set_controls(course, t_token, in_course={"dms": False})
        self.refused(self.send(mate_dm_id, m_token), "in_course.dms")
        self.refused(self.send(mate_dm_id, s_token), "in_course.dms")

        # A call is refused where calls are off.
        alone = self.ok("POST", "/_matrix/client/v3/createRoom", s_token, body={})
        self.refused(
            self.send(alone["room_id"], s_token, "m.call.invite"), "outside.calls"
        )
        self.assertEqual(self.send(alone["room_id"], s_token).status_code, 200)

        # Turned on: back into the other course, and joining is allowed.
        self.set_controls(course, t_token, outside={"other_courses": True})
        self.membership_becomes(other, student, o_token, "join")
        self.membership_becomes(other_chat, student, o_token, "join")
        self.ok("GET", "/_synapse/client/pangea/v1/public_courses", s_token)
        third = self.course(o_token)
        self.assertEqual(self.join(third, s_token).status_code, 200)

        # Someone unmanaged is unaffected.
        bystander, _ = await self.new_user("bystander")
        plain = self.dm(o_token, bystander)
        self.assertEqual(plain.status_code, 200, plain.text)

    async def test_graduation_end_to_end(self) -> None:
        await self.homeserver(module_config=CONFIG, synapse_config_overrides=LIMITS)
        teacher, t_token = await self.new_user("teacher")
        other_teacher, ot_token = await self.new_user("otherteacher")
        student, s_token = await self.new_user("student")
        mate, m_token = await self.new_user("mate")
        outsider, o_token = await self.new_user("outsider")
        server = teacher.split(":", 1)[1]
        course = self.course(t_token)
        chat = self.chat(course, t_token, server)
        self.set_controls(course, t_token, protected=True)
        for user, token in ((student, s_token), (mate, m_token)):
            self.invite_and_join(course, t_token, user, token)
        self.ok("POST", f"/_matrix/client/v3/join/{quote(chat)}", s_token, body={})
        # The student shares analytics with the teacher.
        analytics = self.ok(
            "POST",
            "/_matrix/client/v3/createRoom",
            s_token,
            body={"creation_content": {"type": "p.analytics"}, "invite": [teacher]},
        )["room_id"]
        self.ok("POST", f"/_matrix/client/v3/join/{quote(analytics)}", t_token, body={})
        self.refused(self.dm(s_token, outsider), "outside.dms")

        # Only a course admin graduates.
        one = {"room_id": course, "user_ids": [student]}
        resp = self.call("POST", P + "course_management/graduate", m_token, body=one)
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertEqual(
            self.ok("POST", P + "course_management/graduate", t_token, body=one),
            {"graduated": [student]},
        )
        self.membership_becomes(analytics, teacher, s_token, "leave")
        # The controls are lifted.
        self.assertEqual(self.dm(s_token, outsider).status_code, 200)
        elsewhere = self.course(o_token)
        self.assertEqual(self.join(elsewhere, s_token).status_code, 200)
        # The course's history is readable, not changeable; still a member.
        for _ in range(100):
            if self.send(chat, s_token).status_code == 403:
                break
            time.sleep(0.1)
        else:
            self.fail("the graduate can still send in the course chat")
        self.ok(
            "GET",
            f"/_matrix/client/v3/rooms/{quote(chat)}/messages",
            s_token,
            params={"dir": "b"},
        )
        self.membership_becomes(course, student, t_token, "join")
        self.assertIn((student, "graduated", "invite"), self.ledger(course, t_token))
        # The app shows the graduation once.
        mine = self.ok("GET", P + "course_management/mine", s_token)
        self.assertEqual(mine["courses"], [])
        self.assertEqual([g["room_id"] for g in mine["graduations"]], [course])
        self.assertEqual([g["graduated_by"] for g in mine["graduations"]], [teacher])
        self.ok(
            "POST",
            P + "course_management/graduation_seen",
            s_token,
            body={"room_id": course},
        )
        mine = self.ok("GET", P + "course_management/mine", s_token)
        self.assertEqual(mine["graduations"], [])

        # Joining the course again manages again.
        self.ok(
            "POST", f"/_matrix/client/v3/rooms/{quote(course)}/leave", s_token, body={}
        )
        self.invite_and_join(course, t_token, student, s_token)
        self.becomes(
            lambda: self.records(course, t_token)[student][0],
            "managed",
            "the rejoined student's record",
        )
        self.refused(self.dm(s_token, outsider), "outside.dms")

        # Managed by a second Protected course too; graduating the whole class
        # of the first leaves the second's controls in place.
        second = self.course(ot_token)
        self.set_controls(
            second, ot_token, protected=True, outside={"other_courses": True}
        )
        self.set_controls(course, t_token, outside={"other_courses": True})
        self.invite_and_join(second, ot_token, student, s_token)
        self.assertEqual(
            self.ok(
                "POST",
                P + "course_management/graduate",
                t_token,
                body={"room_id": course, "all": True},
            ),
            {"graduated": sorted([mate, student])},
        )
        self.assertEqual(self.dm(m_token, outsider).status_code, 200)
        self.becomes(
            lambda: [
                c["room_id"]
                for c in self.ok("GET", P + "course_management/mine", s_token)[
                    "courses"
                ]
            ],
            [second],
            "the student's managing courses",
        )
        self.refused(self.dm(s_token, outsider), "outside.dms")


class TestProtectedModerationE2E(ProtectedE2E):
    """A managed student's flags reach the Protected course from any chat;
    everyone else's, and every report, stay with the chat's own course. Its
    own class: the moderation config is a homeserver of its own."""

    moderation: MockModerationServer

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.moderation = MockModerationServer().start()
        cls.addClassCleanup(cls.moderation.stop)

    def flag(self, room: str, token: str) -> str:
        sent = self.call(
            "PUT",
            f"/_matrix/client/v3/rooms/{quote(room)}/send/m.room.message/"
            f"t{time.monotonic_ns()}",
            token,
            body={"msgtype": "m.text", "body": "FLAGME you are awful"},
        )
        self.assertEqual(sent.status_code, 200, sent.text)
        return str(sent.json()["event_id"])

    def course_ids(self, incident_id: str) -> List[str]:
        """The row's stored courses, once Tier 2 has written it."""
        conn = psycopg2.connect(self.database_url)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                for _ in range(120):
                    cur.execute(
                        "SELECT course_ids FROM pangea_safety_incidents"
                        " WHERE incident_id = %s",
                        (incident_id,),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        return list(json.loads(row[0]))
                    time.sleep(0.5)
        finally:
            conn.close()
        self.fail(f"{incident_id} was never written")

    async def test_a_managed_students_flags_reach_the_protected_course(
        self,
    ) -> None:
        moderation = {
            "tier2_enabled": True,
            "choreo_base_url": self.moderation.base_url,
            "choreo_access_token": "syt_mock_service_token",
        }
        await self.homeserver(
            module_config={**CONFIG, "moderation": moderation},
            synapse_config_overrides=LIMITS,
        )
        teacher, t_token = await self.new_user("teacher")
        student, s_token = await self.new_user("student")
        mate, m_token = await self.new_user("mate")
        _, o_token = await self.new_user("outsider")
        server = teacher.split(":", 1)[1]
        other = self.course(o_token)
        other_chat = self.chat(other, o_token, server)
        course = self.course(t_token)
        course_chat = self.chat(course, t_token, server)
        self.set_controls(
            course, t_token, protected=True, outside={"other_courses": True}
        )
        for user, token in ((student, s_token), (mate, m_token)):
            self.invite_and_join(course, t_token, user, token)
        for room in (course_chat, other, other_chat):
            self.ok("POST", f"/_matrix/client/v3/join/{quote(room)}", s_token, body={})
        dm = self.ok(
            "POST",
            "/_matrix/client/v3/createRoom",
            s_token,
            body={"preset": "private_chat", "is_direct": True, "invite": [mate]},
        )["room_id"]
        self.ok("POST", f"/_matrix/client/v3/join/{quote(dm)}", m_token, body={})

        in_other = self.flag(other_chat, s_token)
        in_dm = self.flag(dm, s_token)
        in_course = self.flag(course_chat, s_token)
        by_outsider = self.flag(other_chat, o_token)
        report = self.ok(
            "POST",
            P + "report",
            m_token,
            body={
                "report_id": str(uuid.uuid4()),
                "room_id": dm,
                "event_id": in_dm,
                "reason": "rude",
            },
        )["incident_id"]

        self.assertEqual(self.course_ids(f"mod:{in_other}"), [other, course])
        self.assertEqual(self.course_ids(f"mod:{in_dm}"), [course])
        self.assertEqual(self.course_ids(f"mod:{in_course}"), [course])
        self.assertEqual(self.course_ids(f"mod:{by_outsider}"), [other])
        self.assertEqual(self.course_ids(report), [])
        # Read back as the Protected course's admin sees it.
        listed = self.ok(
            "GET", P + "safety_incidents", t_token, params={"space_id": course}
        )["incidents"]
        self.assertEqual(
            sorted(i["event_id"] for i in listed),
            sorted([in_other, in_dm, in_course]),
        )
