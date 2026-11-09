"""The quest-courses reads against a real Synapse and PostgreSQL.

Design: quest-courses.instructions.md. The server is shared by the class, so
every assertion is scoped to the rooms and invitations its own test made.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import psycopg2
import requests

from tests.base_e2e import SERVER_NAME, BaseSynapseE2ETest

PREFIX = "/_synapse/client/pangea/"
QUEST = PREFIX + "v1/quest_courses/"
COURSE_PLAN = "pangea.course_plan"
# One bot for the whole class, since the module's config names it.
BOT_LOCALPART = "questbot"
BOT_ID = f"@{BOT_LOCALPART}:{SERVER_NAME}"
MODULE_CONFIG = {
    "quest_courses_bot_user_id": BOT_ID,
    "app_base_url": "https://app.example.test",
}
SYNAPSE_CONFIG = {
    "rc_joins": {"local": {"per_second": 9999, "burst_count": 9999}},
    "rc_message": {"per_second": 9999, "burst_count": 9999},
}


class TestQuestCoursesE2E(BaseSynapseE2ETest):
    _bot_token: Optional[str] = None

    async def setup_server(self) -> None:
        await self.homeserver(
            module_config=MODULE_CONFIG, synapse_config_overrides=SYNAPSE_CONFIG
        )

    async def bot(self) -> str:
        cls = type(self)
        if cls._bot_token is None:
            _, cls._bot_token = await self._register_localpart(
                BOT_LOCALPART, "pw", True
            )
        return cls._bot_token

    # -- requests --

    def call(
        self,
        method: str,
        path: str,
        token: Optional[str],
        body: Any = None,
        params: Optional[Dict[str, str]] = None,
    ) -> requests.Response:
        headers = {"Authorization": "Bearer " + token} if token else {}
        return requests.request(
            method,
            self.server_url + path,
            json=body,
            params=params,
            headers=headers,
            timeout=30,
        )

    def ok(self, response: requests.Response) -> Dict[str, Any]:
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def changes(self, token: str, since: Optional[str]) -> Dict[str, Any]:
        params = {"since": since} if since is not None else None
        return self.ok(self.call("GET", QUEST + "space_changes", token, params=params))

    def all_spaces(self, token: str) -> Dict[str, Dict[str, Any]]:
        spaces: Dict[str, Dict[str, Any]] = {}
        after: Optional[str] = None
        while True:
            params = {"limit": "50"}
            if after is not None:
                params["after"] = after
            page = self.ok(self.call("GET", QUEST + "spaces", token, params=params))
            for space in page["spaces"]:
                spaces[space["room_id"]] = space
            after = page["next_after"]
            if after is None:
                return spaces

    def changes_until(
        self, token: str, since: str, wanted: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Follow changes from ``since`` until each wanted room last reported
        the wanted fields; returns the last cursor and every room seen."""
        seen: Dict[str, Dict[str, Any]] = {}
        cursor = since
        for _ in range(50):
            page = self.changes(token, cursor)
            self.assertFalse(page["resync"], page)
            for space in page["spaces"]:
                seen[space["room_id"]] = space
            cursor = page["cursor"]
            if all(
                room in seen and fields.items() <= seen[room].items()
                for room, fields in wanted.items()
            ):
                return {"cursor": cursor, "seen": seen}
        self.fail(f"Changes never reached {wanted}; saw {seen}")

    # -- rooms --

    def create_room(
        self,
        token: str,
        name: str,
        plan: Optional[Dict[str, Any]],
        invite: Optional[List[str]] = None,
    ) -> str:
        body: Dict[str, Any] = {
            "visibility": "private",
            "preset": "private_chat",
            "name": name,
            "invite": invite or [],
        }
        if plan is not None:
            body["creation_content"] = {"type": "m.space"}
            body["initial_state"] = [
                {"type": COURSE_PLAN, "state_key": "", "content": plan}
            ]
        r = self.call("POST", "/_matrix/client/v3/createRoom", token, body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["room_id"]

    def put_state(
        self, token: str, room: str, kind: str, key: str, content: Dict[str, Any]
    ) -> None:
        path = f"/_matrix/client/v3/rooms/{quote(room, safe='')}/state/{kind}/" + quote(
            key, safe=""
        )
        r = self.call("PUT", path, token, content)
        self.assertEqual(r.status_code, 200, r.text)

    def join(self, token: str, room: str) -> None:
        r = self.call(
            "POST", f"/_matrix/client/v3/join/{quote(room, safe='')}", token, {}
        )
        self.assertEqual(r.status_code, 200, r.text)

    def leave(self, token: str, room: str) -> None:
        path = f"/_matrix/client/v3/rooms/{quote(room, safe='')}/leave"
        r = self.call("POST", path, token, {})
        self.assertEqual(r.status_code, 200, r.text)

    def unclaimed(self, token: str) -> List[Dict[str, Any]]:
        return self.ok(self.call("GET", QUEST + "unclaimed", token))["courses"]

    # -- tests --

    async def test_only_a_server_admin_may_read(self) -> None:
        await self.setup_server()
        _, user = await self.new_user("user")
        checks: List[Tuple[str, str, Any]] = [
            ("GET", "space_changes", None),
            ("GET", "spaces", None),
            ("GET", "unclaimed", None),
        ]
        for method, path, body in checks:
            anonymous = self.call(method, QUEST + path, None, body)
            self.assertEqual(anonymous.status_code, 401, (path, anonymous.text))
            refused = self.call(method, QUEST + path, user, body)
            self.assertEqual(refused.status_code, 403, (path, refused.text))

    async def test_full_read_then_changes_follow_each_course_plan(self) -> None:
        await self.setup_server()
        _, admin = await self.new_user("admin", admin=True)
        _, teacher = await self.new_user("teacher")
        quest = f"quest-{self.tag}"

        start = self.changes(admin, None)
        self.assertTrue(start["resync"])
        cursor = start["cursor"]

        private = self.create_room(
            teacher, "Private", {"uuid": quest + "-a", "l2": "es"}
        )
        chat = self.create_room(teacher, "Chat", None)
        spaces = self.all_spaces(admin)
        self.assertEqual(
            spaces[private],
            {
                "room_id": private,
                "quest_id": quest + "-a",
                "l2": "es",
                "name": "Private",
                "joined_members": 1,
            },
        )
        self.assertNotIn(chat, spaces)

        # The quest moves; an older space carries the legacy key; a plan on a
        # second state key does not override the room's own.
        self.put_state(
            teacher, private, COURSE_PLAN, "", {"uuid": quest + "-b", "l2": "es"}
        )
        self.put_state(teacher, private, COURSE_PLAN, "other", {"uuid": quest + "-z"})
        legacy = self.create_room(teacher, "Legacy", {"course_plan_id": quest + "-c"})
        followed = self.changes_until(
            admin,
            cursor,
            {private: {"quest_id": quest + "-b"}, legacy: {"quest_id": quest + "-c"}},
        )
        self.assertNotIn(chat, followed["seen"])
        self.assertEqual(followed["seen"][legacy]["l2"], None)

        # The last local member leaving is how a deleted space arrives.
        self.leave(teacher, private)
        gone = self.changes_until(
            admin, followed["cursor"], {private: {"quest_id": None}}
        )
        self.assertIsNone(gone["seen"][private]["l2"])
        self.assertNotIn(private, self.all_spaces(admin))

        # Sending the same cursor again returns the same changes.
        again = self.changes(admin, followed["cursor"])
        self.assertIn(private, [s["room_id"] for s in again["spaces"]])

    async def test_a_cursor_that_does_not_fit_starts_over(self) -> None:
        await self.setup_server()
        _, admin = await self.new_user("admin", admin=True)
        ahead = self.changes(admin, f"{SERVER_NAME}/{10**15}")
        self.assertTrue(ahead["resync"])
        self.assertTrue(ahead["cursor"].startswith(SERVER_NAME + "/"))
        bad = self.call("GET", QUEST + "space_changes", admin, params={"since": "x"})
        self.assertEqual(bad.status_code, 400, bad.text)
        # A cursor from another server is refused, not read as a position here.
        other = self.call(
            "GET", QUEST + "space_changes", admin, params={"since": "elsewhere.org/5"}
        )
        self.assertEqual(other.status_code, 409, other.text)

    async def test_changes_report_names_and_people_but_not_the_bot(self) -> None:
        await self.setup_server()
        bot = await self.bot()
        _, admin = await self.new_user("admin", admin=True)
        student_id, student = await self.new_user("student")
        _, teacher = await self.new_user("teacher")
        quest = f"q-{self.tag}"
        course = self.create_room(teacher, "Spanish trip", {"uuid": quest})
        chat = self.create_room(teacher, "Chat", None, [student_id])
        empty = self.create_room(bot, "Bot only", {"uuid": quest + "-2"})
        cursor = self.changes(admin, None)["cursor"]
        self.assertEqual(self.all_spaces(admin)[empty]["joined_members"], 0)

        # A student joins, then the bot, then the course is renamed; a member
        # joining a room that is not a course is not reported.
        self.put_state(
            teacher, course, "m.room.join_rules", "", {"join_rule": "public"}
        )
        self.join(student, course)
        self.join(bot, course)
        self.put_state(teacher, course, "m.room.name", "", {"name": "Madrid trip"})
        self.join(student, chat)
        seen = self.changes_until(
            admin,
            cursor,
            {course: {"name": "Madrid trip", "joined_members": 2, "quest_id": quest}},
        )["seen"]
        self.assertNotIn(chat, seen)

    async def test_unclaimed_lists_waiting_building_and_bot_held_courses(self) -> None:
        await self.setup_server()
        bot = await self.bot()
        _, admin = await self.new_user("admin", admin=True)

        def prepare(key: str, quest: str) -> str:
            body = {
                "request_key": f"{key}-{self.tag}",
                "teacher_email": f"{key}+{self.tag}@school.example",
                "course_plan_id": quest,
                "title": f"Course {key}",
                "target_language": "fr",
                "send_email": False,
            }
            r = self.call("POST", PREFIX + "v2/create_course_space", admin, body)
            self.assertEqual(r.status_code, 200, r.text)
            return r.json()["invitation_id"]

        waiting = prepare("waiting", f"qw-{self.tag}")
        building = prepare("building", f"qb-{self.tag}")
        revoked = prepare("revoked", f"qr-{self.tag}")
        r = self.call("DELETE", PREFIX + "v2/course_invitations/" + revoked, admin)
        self.assertEqual(r.status_code, 200, r.text)

        held = self.create_room(
            bot, "Held course", {"uuid": f"qh-{self.tag}", "l2": "de"}
        )
        claimed = self.create_room(bot, "Claimed course", {"uuid": f"qc-{self.tag}"})
        deleted = self.create_room(bot, "Deleted course", {"uuid": f"qd-{self.tag}"})
        self.leave(bot, deleted)
        self.unclaimed(admin)  # creates the invitation and claim tables
        with psycopg2.connect(self.database_url) as conn, conn.cursor() as cur:
            # A claim still being built, and bot-held legacy courses.
            cur.execute(
                "UPDATE pangea_course_invitation SET status = 'provisioning',"
                " claimant = %s WHERE invitation_id = %s",
                ("@someone:" + SERVER_NAME, building),
            )
            for room, claimant in ((held, None), (claimed, "@t:x"), (deleted, None)):
                cur.execute(
                    "INSERT INTO pangea_course_claim"
                    " (room_id, admin_code_sha256, created_at_ms, claimed_by)"
                    " VALUES (%s, %s, 7, %s)",
                    (room, "test:" + room, claimant),
                )

        listed = self.unclaimed(admin)
        invitations = {
            c["invitation_id"]: c for c in listed if c["kind"] == "invitation"
        }
        rooms = {c["room_id"]: c for c in listed if c["kind"] == "course_room"}
        self.assertEqual(invitations[waiting]["quest_id"], f"qw-{self.tag}")
        self.assertEqual(invitations[waiting]["l2"], "fr")
        self.assertEqual(invitations[waiting]["title"], "Course waiting")
        # Claimed but not yet built is still unclaimed: no space exists yet.
        self.assertIn(building, invitations)
        self.assertNotIn(revoked, invitations)
        self.assertEqual(
            rooms[held],
            {
                "kind": "course_room",
                "invitation_id": None,
                "room_id": held,
                "quest_id": f"qh-{self.tag}",
                "l2": "de",
                "title": "Held course",
                "created_at_ms": 7,
            },
        )
        self.assertNotIn(claimed, rooms)
        self.assertNotIn(deleted, rooms)
        # No address leaves through this read.
        self.assertNotIn("@school.example", str(listed))
