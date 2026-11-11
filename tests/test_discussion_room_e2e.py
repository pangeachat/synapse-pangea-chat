"""Discussion rooms on a real homeserver: placement in rooms of 12, the room
the bot sets up, ending and reopening a seat, who is refused, and the rooms
placement never uses.

Design: .github/instructions/discussion-room.instructions.md.
"""

import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

from .base_e2e import SERVER_NAME, BaseSynapseE2ETest
from .mock_cms_server import MockCmsServer

ENDPOINT = "/_synapse/client/pangea/v1/discussion_room"
BOT_LOCALPART = "discussion-bot"
BOT = f"@{BOT_LOCALPART}:{SERVER_NAME}"
BOT_PASSWORD = "bot-pw"
QUEST = "quest-1"
MISSION = "lo-1"
VERSION = "0123456789abcdef0123456789abcdef"
# The activity's latest version; VERSION is an older one choreo still keeps.
LATEST = "fedcba9876543210fedcba9876543210"
UNKNOWN_VERSION = "ffffffffffffffffffffffffffffffff"
REMOVED = "PANGEA_DISCUSSION_REMOVED"
ROLES = "pangea.activity_roles"
PLAN = "pangea.activity_plan"


def _plan(activity_id: str, **overrides: Any) -> Dict[str, Any]:
    plan: Dict[str, Any] = {
        "activity_id": activity_id,
        "mode": "Discussion",
        "title": "Weekend plans",
        "description": "Talk about your weekend.",
        # A CMS row's role item carries Payload's own row `id` beside the
        # role id (the shape that broke every real discussion in local testing).
        "roles": [
            {
                "id": "6650f1a2b3c4d5e6f7a8b9c0",
                "role_id": "participant",
                "name": "Participant",
                "goal_ids": ["q1", "q2"],
            }
        ],
        "media": [],
    }
    plan.update(overrides)
    return plan


def _row(plan: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    return {
        "id": f"row-{plan['activity_id']}",
        "req": {},
        "res": {"plan": plan},
        "learningObjectiveRefs": [MISSION],
        **fields,
    }


class TestDiscussionRoomE2E(BaseSynapseE2ETest):
    cms: MockCmsServer
    cms_url: str

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.cms = MockCmsServer()
        cls.cms_url = cls.cms.start()
        cls.cms.seed_quest(
            {
                "id": QUEST,
                "req": {"target_language": "es"},
                "res": {
                    "learning_objective_sequence": [{"learning_objective": MISSION}]
                },
            }
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cms.stop()
        super().tearDownClass()

    async def _start(self) -> None:
        await self.homeserver(
            module_config={
                "cms_base_url": self.cms_url,
                "user_activity_notification_bot_user_id": BOT,
                # The mock serves choreo's activity read too.
                "discussion_room_choreo_base_url": self.cms_url,
            }
        )
        await self._ensure_bot()
        self.operator_token = (await self.new_user("operator", admin=True))[1]

    # -- setup helpers -----------------------------------------------------

    async def _ensure_bot(self) -> None:
        """The discussion bot is one account for the whole class's server."""
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/login",
            json={
                "type": "m.login.password",
                "user": BOT_LOCALPART,
                "password": BOT_PASSWORD,
            },
            timeout=10,
        )
        if response.status_code != 200:
            await self.register_user("", "", BOT_LOCALPART, BOT_PASSWORD, admin=False)

    def _bot_token(self) -> str:
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/login",
            json={
                "type": "m.login.password",
                "user": BOT_LOCALPART,
                "password": BOT_PASSWORD,
            },
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def _headers(self, token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _url(self, path: str) -> str:
        return f"{self.server_url}{path}"

    def _room_url(self, room_id: str, rest: str) -> str:
        return self._url(f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/{rest}")

    def _discussion(
        self, name: str, plan: Optional[Dict[str, Any]] = None, **row_fields: Any
    ) -> str:
        activity_id = f"{name}-{self.tag}"
        self.cms.seed_activity(_row(_plan(activity_id, **(plan or {})), **row_fields))
        self.cms.seed_activity_versions(activity_id, [LATEST, VERSION])
        return activity_id

    def _course(
        self,
        teacher_token: str,
        plan: Optional[Dict[str, Any]] = None,
        member_events: Tuple[str, ...] = (),
    ) -> str:
        """A course space as production has it: any member may link a child,
        and its quest named in pangea.course_plan. ``member_events`` are
        further state any member may write."""
        response = requests.post(
            self._url("/_matrix/client/v3/createRoom"),
            headers=self._headers(teacher_token),
            json={
                "preset": "public_chat",
                "visibility": "private",
                "creation_content": {"type": "m.space"},
                "power_level_content_override": {
                    "events": {
                        "m.space.child": 0,
                        **{event_type: 0 for event_type in member_events},
                    }
                },
                "initial_state": [
                    {
                        "type": "pangea.course_plan",
                        "state_key": "",
                        "content": plan if plan is not None else {"uuid": QUEST},
                    }
                ],
            },
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["room_id"]

    def _join(self, token: str, room_id: str) -> requests.Response:
        return requests.post(
            self._url(f"/_matrix/client/v3/join/{quote(room_id, safe='')}"),
            headers=self._headers(token),
            json={},
            timeout=10,
        )

    async def _students(
        self, course_id: str, count: int, prefix: str = "s"
    ) -> List[Tuple[str, str]]:
        students = [await self.new_user(f"{prefix}{n}") for n in range(count)]
        for _, token in students:
            # A class joining one course meets Synapse's per-room join limit;
            # setup waits it out.
            for _ in range(20):
                response = self._join(token, course_id)
                if response.status_code != 429:
                    break
                time.sleep(response.json().get("retry_after_ms", 1000) / 1000)
            self.assertEqual(response.status_code, 200, response.text)
        return students

    async def _setting(self) -> Tuple[Tuple[str, str], str, str]:
        """The class's server with its bot, then a teacher, their course, and
        a discussion of it."""
        await self._start()
        teacher = await self.new_user("teacher")
        course_id = self._course(teacher[1])
        return teacher, course_id, self._discussion("disc")

    # -- endpoint helpers -------------------------------------------------------

    def _place(
        self,
        token: str,
        course_id: str,
        activity_id: str,
        version_id: Optional[str] = VERSION,
    ) -> requests.Response:
        body: Dict[str, Any] = {"course_id": course_id, "activity_id": activity_id}
        if version_id is not None:
            body["version_id"] = version_id
        return requests.post(
            self._url(ENDPOINT), headers=self._headers(token), json=body, timeout=60
        )

    def _placed(self, token: str, course_id: str, activity_id: str) -> Dict[str, Any]:
        response = self._place(token, course_id, activity_id)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _preview_response(
        self, token: str, course_id: str, activity_id: str
    ) -> requests.Response:
        return requests.get(
            self._url(ENDPOINT),
            headers=self._headers(token),
            params={"course_id": course_id, "activity_id": activity_id},
            timeout=30,
        )

    def _preview(self, token: str, course_id: str, activity_id: str) -> Dict[str, Any]:
        response = self._preview_response(token, course_id, activity_id)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _seat(self, token: str, room_id: str, action: str) -> requests.Response:
        return requests.post(
            self._url(ENDPOINT),
            headers=self._headers(token),
            json={"room_id": room_id, "action": action},
            timeout=30,
        )

    # -- reading the server back --------------------------------------------------

    def _state(self, room_id: str) -> Dict[Tuple[str, str], Dict[str, Any]]:
        response = requests.get(
            self._url(f"/_synapse/admin/v1/rooms/{quote(room_id, safe='')}/state"),
            headers=self._headers(self.operator_token),
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {(e["type"], e["state_key"]): e for e in response.json()["state"]}

    def _seats(self, room_id: str) -> Dict[str, Dict[str, Any]]:
        roles = self._state(room_id).get((ROLES, ""))
        return roles["content"]["roles"] if roles else {}

    def _membership(self, room_id: str, user_id: str) -> Optional[str]:
        event = self._state(room_id).get(("m.room.member", user_id))
        return event["content"]["membership"] if event else None

    async def _eventually(
        self, check: Callable[[], bool], what: str, seconds: float = 20
    ) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return
            time.sleep(0.25)
        self.fail(f"Timed out waiting for {what}")

    def _set_power(
        self, token: str, room_id: str, user_id: str, level: int
    ) -> requests.Response:
        current = self._state(room_id)[("m.room.power_levels", "")]["content"]
        users = dict(current.get("users", {}))
        users[user_id] = level
        return requests.put(
            self._room_url(room_id, "state/m.room.power_levels/"),
            headers=self._headers(token),
            json={**current, "users": users},
            timeout=10,
        )

    # -- tests ----------------------------------------------------------------------

    async def test_thirteen_at_once_fill_rooms_of_twelve_and_one(self):
        teacher, course_id, activity_id = await self._setting()
        students = await self._students(course_id, 13)
        by_user = dict(students)

        with ThreadPoolExecutor(max_workers=13) as pool:
            responses = list(
                pool.map(lambda s: self._place(s[1], course_id, activity_id), students)
            )
        for response in responses:
            self.assertEqual(response.status_code, 200, response.text)
        answers = [r.json() for r in responses]
        sizes = Counter(answer["room_id"] for answer in answers)
        self.assertEqual(sorted(sizes.values()), [1, 12])
        self.assertEqual(sum(answer["created"] for answer in answers), 2)

        # One seat each, keyed by student, in the room each was placed in;
        # everyone joined.
        full_room = next(room for room, n in sizes.items() if n == 12)
        for room_id in sizes:
            placed = {
                u for (u, _), a in zip(students, answers) if a["room_id"] == room_id
            }
            seats = self._seats(room_id)
            self.assertEqual(set(seats), {f"participant:{u}" for u in placed})
            self.assertEqual({seat["user_id"] for seat in seats.values()}, placed)
            state = self._state(room_id)
            for user_id in placed:
                self.assertEqual(
                    state[("m.room.member", user_id)]["content"]["membership"], "join"
                )

        # A second call returns the same room and writes no second seat.
        first_user, first_token = students[0]
        again = self._placed(first_token, course_id, activity_id)
        self.assertEqual(again, {"room_id": answers[0]["room_id"], "created": False})
        self.assertEqual(
            len(self._seats(answers[0]["room_id"])), sizes[answers[0]["room_id"]]
        )

        # A student who leaves the full room on their own frees their place;
        # their next tap takes them back, even though it is full again.
        leaver = next(
            u for (u, _), a in zip(students, answers) if a["room_id"] == full_room
        )
        left = requests.post(
            self._room_url(full_room, "leave"),
            headers=self._headers(by_user[leaver]),
            json={},
            timeout=10,
        )
        self.assertEqual(left.status_code, 200, left.text)
        newcomer, newcomer_token = (await self._students(course_id, 1, prefix="late"))[
            0
        ]
        self.assertEqual(
            self._preview(newcomer_token, course_id, activity_id),
            {"room_id": full_room, "students": 11, "joined": False},
        )
        self.assertEqual(
            self._placed(newcomer_token, course_id, activity_id)["room_id"], full_room
        )
        self.assertEqual(
            self._placed(by_user[leaver], course_id, activity_id)["room_id"], full_room
        )
        self.assertEqual(self._membership(full_room, leaver), "join")
        self.assertEqual(len(self._seats(full_room)), 13)

    async def test_preview_and_an_admin_who_taps_join(self):
        (teacher_id, teacher_token), course_id, activity_id = await self._setting()
        (s1, s1_token), (s2, s2_token) = await self._students(course_id, 2)

        self.assertEqual(
            self._preview(s1_token, course_id, activity_id),
            {"room_id": None, "students": 0, "joined": False},
        )
        room_id = self._placed(s1_token, course_id, activity_id)["room_id"]
        self.assertEqual(
            self._preview(s1_token, course_id, activity_id),
            {"room_id": room_id, "students": 1, "joined": True},
        )

        # A course admin who taps Join is placed and counts like a student.
        self.assertEqual(
            self._placed(teacher_token, course_id, activity_id)["room_id"], room_id
        )
        self.assertIn(f"participant:{teacher_id}", self._seats(room_id))
        self.assertEqual(
            self._preview(s2_token, course_id, activity_id),
            {"room_id": room_id, "students": 2, "joined": False},
        )

    async def test_the_room_the_bot_sets_up(self):
        (teacher_id, teacher_token), course_id, activity_id = await self._setting()
        ((s1, s1_token), (s2, _)) = await self._students(course_id, 2)
        answer = self._placed(s1_token, course_id, activity_id)
        self.assertTrue(answer["created"])
        room_id = answer["room_id"]
        state = self._state(room_id)

        create = state[("m.room.create", "")]
        self.assertEqual(create["sender"], BOT)
        self.assertEqual(create["content"]["type"], f"p.activity.session:{activity_id}")
        self.assertEqual(
            create["content"]["pangea.discussion"],
            {"course_id": course_id, "activity_id": activity_id},
        )
        self.assertEqual(state[("m.room.member", BOT)]["content"]["membership"], "join")

        # The plan pins the version the start page read, with the mode.
        plan = state[(PLAN, "")]
        self.assertEqual(plan["sender"], BOT)
        self.assertEqual(
            plan["content"],
            {
                "activity_id": activity_id,
                "version_id": VERSION,
                "source_course_id": course_id,
                "mode": "discussion",
            },
        )
        self.assertEqual(state[(ROLES, "")]["sender"], BOT)

        power = state[("m.room.power_levels", "")]["content"]
        self.assertEqual(power["users"], {BOT: 100, teacher_id: 50})
        self.assertEqual(power["events"][PLAN], 100)
        self.assertEqual(power["events"][ROLES], 100)
        self.assertEqual((power["invite"], power["state_default"]), (100, 100))
        self.assertEqual(
            state[("m.room.join_rules", "")]["content"],
            {
                "join_rule": "knock_restricted",
                "allow": [{"type": "m.room_membership", "room_id": course_id}],
            },
        )
        self.assertEqual(
            state[("m.room.history_visibility", "")]["content"]["history_visibility"],
            "shared",
        )
        self.assertEqual(state[("m.room.name", "")]["content"]["name"], "Weekend plans")
        link = self._state(course_id).get(("m.space.child", room_id))
        self.assertIsNotNone(link)
        self.assertEqual(link["content"]["via"], [SERVER_NAME])

        # A student can neither rewrite the seats nor repoint the plan.
        for event_type in (ROLES, PLAN):
            response = requests.put(
                self._room_url(room_id, f"state/{event_type}/"),
                headers=self._headers(s1_token),
                json={},
                timeout=10,
            )
            self.assertEqual(response.status_code, 403, event_type)

        # The teacher is joined to read along and left at 50: raising her to
        # the room's invite level would give her the bot's power.
        await self._eventually(
            lambda: self._membership(room_id, teacher_id) == "join",
            "the teacher joined to read along",
        )
        time.sleep(1)
        self.assertEqual(
            self._state(room_id)[("m.room.power_levels", "")]["content"]["users"],
            {BOT: 100, teacher_id: 50},
        )

        # The teacher can't invite anyone (Join is the only way in) or change
        # the room's settings.
        invite = requests.post(
            self._room_url(room_id, "invite"),
            headers=self._headers(teacher_token),
            json={"user_id": s2},
            timeout=10,
        )
        self.assertEqual(invite.status_code, 403, invite.text)
        for event_type, content in (
            ("m.room.name", {"name": "Renamed"}),
            ("m.room.topic", {"topic": "Something else"}),
            ("m.room.avatar", {"url": "mxc://example.org/x"}),
            ("m.room.join_rules", {"join_rule": "public"}),
            ("m.room.history_visibility", {"history_visibility": "joined"}),
            ("m.room.encryption", {"algorithm": "m.megolm.v1.aes-sha2"}),
            ("m.room.tombstone", {"body": "moved", "replacement_room": course_id}),
        ):
            response = requests.put(
                self._room_url(room_id, f"state/{event_type}/"),
                headers=self._headers(teacher_token),
                json=content,
                timeout=10,
            )
            self.assertEqual(response.status_code, 403, event_type)

        # The teacher moderates students but can't remove the bot or raise
        # themselves: the bot can't be taken out of the room.
        self.assertEqual(self._join(teacher_token, room_id).status_code, 200)
        kick_bot = requests.post(
            self._room_url(room_id, "kick"),
            headers=self._headers(teacher_token),
            json={"user_id": BOT},
            timeout=10,
        )
        self.assertEqual(kick_bot.status_code, 403, kick_bot.text)
        self.assertEqual(
            self._set_power(teacher_token, room_id, teacher_id, 100).status_code, 403
        )
        self.assertEqual(
            self._set_power(teacher_token, room_id, BOT, 0).status_code, 403
        )
        kick_student = requests.post(
            self._room_url(room_id, "kick"),
            headers=self._headers(teacher_token),
            json={"user_id": s1},
            timeout=10,
        )
        self.assertEqual(kick_student.status_code, 200, kick_student.text)

    async def test_a_new_room_pins_only_a_version_choreo_knows(self):
        _, course_id, activity_id = await self._setting()
        (_, s1_token), (_, s2_token), (_, s3_token) = await self._students(course_id, 3)
        # Not a version of the activity: no room is made on it.
        unknown = self._place(s1_token, course_id, activity_id, UNKNOWN_VERSION)
        self.assertEqual(unknown.status_code, 409, unknown.text)
        self.assertEqual(unknown.json()["errcode"], "PANGEA_DISCUSSION_UNKNOWN_VERSION")
        self.assertIsNone(self._preview(s1_token, course_id, activity_id)["room_id"])

        # An older version choreo still keeps is pinned as it is.
        room_id = self._placed(s2_token, course_id, activity_id)["room_id"]
        plan = self._state(room_id)[(PLAN, "")]["content"]
        self.assertEqual(plan["version_id"], VERSION)

        # A room with space takes a student whatever version they send.
        placed = self._place(s3_token, course_id, activity_id, UNKNOWN_VERSION)
        self.assertEqual(placed.status_code, 200, placed.text)
        self.assertEqual(placed.json(), {"room_id": room_id, "created": False})

    async def test_the_room_image_never_holds_a_join(self):
        _, course_id, _ = await self._setting()
        (_, s1_token), (_, s2_token) = await self._students(course_id, 2)
        fast, slow = f"img-fast-{self.tag}", f"img-slow-{self.tag}"
        self.cms.seed_media(fast, {"sizes": {"medium": {"url": "/media/fast.jpg"}}})
        self.cms.seed_media(
            slow, {"sizes": {"medium": {"url": "/media/slow.jpg"}}}, delay_seconds=8
        )
        with_image = self._discussion(
            "fast", plan={"media": [{"blockType": "image", "upload_id": fast}]}
        )
        slow_image = self._discussion(
            "slow", plan={"media": [{"blockType": "image", "upload_id": slow}]}
        )

        room_id = self._placed(s1_token, course_id, with_image)["room_id"]
        avatar = self._state(room_id)[("m.room.avatar", "")]["content"]["url"]
        self.assertEqual(avatar, f"{self.cms_url}/media/fast.jpg")

        # An image the CMS is slow to give is given up on: the room is made
        # without one, well before the CMS answers.
        started = time.monotonic()
        room_id = self._placed(s2_token, course_id, slow_image)["room_id"]
        self.assertLess(time.monotonic() - started, 7)
        self.assertNotIn(("m.room.avatar", ""), self._state(room_id))

    async def test_a_room_with_no_version_follows_the_latest(self):
        _, course_id, activity_id = await self._setting()
        ((_, s1_token),) = await self._students(course_id, 1)
        response = self._place(s1_token, course_id, activity_id, version_id=None)
        self.assertEqual(response.status_code, 200, response.text)
        plan = self._state(response.json()["room_id"])[(PLAN, "")]["content"]
        self.assertNotIn("version_id", plan)

    async def test_ending_and_reopening_a_seat(self):
        _, course_id, activity_id = await self._setting()
        (s1, s1_token), (s2, s2_token), (_, s3_token) = await self._students(
            course_id, 3
        )
        room_id = self._placed(s1_token, course_id, activity_id)["room_id"]
        self.assertEqual(
            self._placed(s2_token, course_id, activity_id)["room_id"], room_id
        )
        before = self._seats(room_id)

        ended = self._seat(s1_token, room_id, "end")
        self.assertEqual(ended.status_code, 200, ended.text)
        seat = ended.json()["seat"]
        self.assertEqual(ended.json()["room_id"], room_id)
        self.assertRegex(
            seat["finished_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$"
        )
        self.assertEqual(seat["archived_at"], seat["finished_at"])
        seats = self._seats(room_id)
        self.assertEqual(seats[f"participant:{s1}"], seat)
        self.assertEqual(seats[f"participant:{s2}"], before[f"participant:{s2}"])

        # Ending again keeps the first time; an ended student still counts.
        self.assertEqual(self._seat(s1_token, room_id, "end").json()["seat"], seat)
        self.assertEqual(self._preview(s3_token, course_id, activity_id)["students"], 2)

        reopened = self._seat(s1_token, room_id, "continue")
        self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertIsNone(reopened.json()["seat"]["finished_at"])
        self.assertIsNone(self._seats(room_id)[f"participant:{s1}"]["archived_at"])

        # Only a seat holder of a discussion room the server made.
        self.assertEqual(self._seat(s3_token, room_id, "end").status_code, 403)
        other_room = await self.create_private_room(s1_token)
        self.assertEqual(self._seat(s1_token, other_room, "end").status_code, 403)
        self.assertEqual(
            self._seat(s1_token, f"!unknown:{SERVER_NAME}", "end").status_code, 404
        )
        self.assertEqual(self._seat(s1_token, room_id, "finish").status_code, 400)

    async def test_who_is_refused(self):
        _, course_id, activity_id = await self._setting()
        ((_, member_token),) = await self._students(course_id, 1)
        _, outsider_token = await self.new_user("outsider")

        self.assertEqual(
            self._place(outsider_token, course_id, activity_id).status_code, 403
        )
        # A room the caller is in that is not a course: their own room, or a
        # space without a course plan.
        own_room = await self.create_private_room(member_token)
        response = requests.post(
            self._url("/_matrix/client/v3/createRoom"),
            headers=self._headers(member_token),
            json={"creation_content": {"type": "m.space"}},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        bare_space = response.json()["room_id"]
        for room in (own_room, bare_space):
            refused = self._place(member_token, room, activity_id)
            self.assertEqual(refused.status_code, 403, room)
            self.assertEqual(refused.json()["error"], "Not a course")
        unauthenticated = requests.post(
            self._url(ENDPOINT),
            json={"course_id": course_id, "activity_id": activity_id},
            timeout=10,
        )
        self.assertEqual(unauthenticated.status_code, 401)
        missing = requests.post(
            self._url(ENDPOINT),
            headers=self._headers(member_token),
            json={"course_id": course_id},
            timeout=10,
        )
        self.assertEqual(missing.status_code, 400)

        # Not one of this course's discussions: another course's copy, a
        # roleplay, a discussion under a Mission outside the quest, or nothing.
        other_copy = self._discussion(
            "copy", course_room_id=f"!elsewhere:{SERVER_NAME}"
        )
        roleplay = f"roleplay-{self.tag}"
        self.cms.seed_activity(_row(_plan(roleplay, mode="Roleplay")))
        outside = self._discussion("outside", learningObjectiveRefs=["lo-elsewhere"])
        for activity in (other_copy, roleplay, outside, f"unknown-{self.tag}"):
            response = self._place(member_token, course_id, activity)
            self.assertEqual(response.status_code, 404, activity)

        # The course's own copy is its discussion.
        own_copy = self._discussion("own", course_room_id=course_id)
        self.assertEqual(
            self._place(member_token, course_id, own_copy).status_code, 200
        )

    async def test_the_courses_missions_from_its_plan_and_its_own_list(self):
        await self._start()
        _, teacher_token = await self.new_user("teacher")

        # An older space names its quest as course_plan_id, not uuid.
        older = self._course(teacher_token, plan={"course_plan_id": QUEST})
        ((_, older_token),) = await self._students(older, 1, prefix="o")
        in_quest = self._discussion("in-quest")
        self.assertEqual(self._place(older_token, older, in_quest).status_code, 200)

        # A Mission the quest does not carry is the course's once a course
        # admin's own Can-Do list adds it, and never because a student's does.
        course_id = self._course(
            teacher_token, member_events=("pangea.course_missions",)
        )
        ((_, token),) = await self._students(course_id, 1)
        added = self._discussion("added", learningObjectiveRefs=["lo-added"])
        self.assertEqual(self._place(token, course_id, added).status_code, 404)

        def write_copy(writer_token: str) -> None:
            response = requests.put(
                self._room_url(course_id, "state/pangea.course_missions/"),
                headers=self._headers(writer_token),
                json={
                    "version": 1,
                    "revision": 1,
                    "missions": [
                        {"lo_id": MISSION, "activity_ids": []},
                        {"lo_id": "lo-added", "activity_ids": [added]},
                    ],
                },
                timeout=10,
            )
            self.assertEqual(response.status_code, 200, response.text)

        write_copy(token)
        self.assertEqual(self._place(token, course_id, added).status_code, 404)
        write_copy(teacher_token)
        self.assertEqual(self._place(token, course_id, added).status_code, 200)

    async def test_a_cms_that_is_down_or_slow_answers_502(self):
        _, course_id, _ = await self._setting()
        ((_, token),) = await self._students(course_id, 1)
        down = self._discussion("down")
        self.cms.set_activity_fault(down, status=500)
        slow = self._discussion("slow")
        self.cms.set_activity_fault(slow, delay_seconds=6)
        for activity in (down, slow):
            response = self._place(token, course_id, activity)
            self.assertEqual(response.status_code, 502, activity)

    async def test_removed_and_banned_students_are_refused(self):
        (_, teacher_token), course_id, activity_id = await self._setting()
        (kicked, kicked_token), (banned, banned_token) = await self._students(
            course_id, 2
        )
        room_id = self._placed(kicked_token, course_id, activity_id)["room_id"]
        self.assertEqual(
            self._placed(banned_token, course_id, activity_id)["room_id"], room_id
        )

        self.assertEqual(self._join(teacher_token, room_id).status_code, 200)
        for action, user_id in (("kick", kicked), ("ban", banned)):
            response = requests.post(
                self._room_url(room_id, action),
                headers=self._headers(teacher_token),
                json={"user_id": user_id},
                timeout=10,
            )
            self.assertEqual(response.status_code, 200, response.text)

        # Never put back where they were removed, and never placed elsewhere:
        # Join and the preview both refuse them, so the app can say so.
        for token in (kicked_token, banned_token):
            for response in (
                self._place(token, course_id, activity_id),
                self._preview_response(token, course_id, activity_id),
            ):
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["errcode"], REMOVED)
        self.assertEqual(self._join(kicked_token, room_id).status_code, 403)
        self.assertEqual(self._membership(room_id, kicked), "leave")
        self.assertEqual(self._membership(room_id, banned), "ban")

    async def test_an_unbanned_student_returns_to_their_own_room(self):
        (_, teacher_token), course_id, activity_id = await self._setting()
        ((student, token),) = await self._students(course_id, 1)
        room_id = self._placed(token, course_id, activity_id)["room_id"]
        self.assertEqual(self._join(teacher_token, room_id).status_code, 200)
        for action in ("ban", "unban"):
            response = requests.post(
                self._room_url(room_id, action),
                headers=self._headers(teacher_token),
                json={"user_id": student},
                timeout=10,
            )
            self.assertEqual(response.status_code, 200, response.text)

        self.assertEqual(
            self._preview(token, course_id, activity_id)["room_id"], room_id
        )
        self.assertEqual(
            self._placed(token, course_id, activity_id),
            {"room_id": room_id, "created": False},
        )
        self.assertEqual(self._membership(room_id, student), "join")

    async def test_an_own_room_that_lost_its_bot_is_skipped(self):
        _, course_id, activity_id = await self._setting()
        ((_, token),) = await self._students(course_id, 1)
        room_id = self._placed(token, course_id, activity_id)["room_id"]
        # Only a manual change takes the bot out: here the bot leaves.
        left = requests.post(
            self._room_url(room_id, "leave"),
            headers=self._headers(self._bot_token()),
            json={},
            timeout=10,
        )
        self.assertEqual(left.status_code, 200, left.text)

        self.assertIsNone(self._preview(token, course_id, activity_id)["room_id"])
        placed = self._placed(token, course_id, activity_id)
        self.assertTrue(placed["created"])
        self.assertNotEqual(placed["room_id"], room_id)

    async def test_a_seat_whose_join_never_landed_is_repaired(self):
        _, course_id, activity_id = await self._setting()
        (s1, s1_token), (_, s2_token) = await self._students(course_id, 2)
        room_id = self._placed(s1_token, course_id, activity_id)["room_id"]
        # A seat for a join that never happened, as a crash between the seat
        # and the join would leave one.
        ghost = f"@ghost-{self.tag}:{SERVER_NAME}"
        roles = self._state(room_id)[(ROLES, "")]["content"]
        roles["roles"][f"participant:{ghost}"] = {
            "id": "participant",
            "user_id": ghost,
            "role": "Participant",
        }
        written = requests.put(
            self._room_url(room_id, f"state/{ROLES}/"),
            headers=self._headers(self._bot_token()),
            json=roles,
            timeout=10,
        )
        self.assertEqual(written.status_code, 200, written.text)

        self.assertEqual(self._preview(s2_token, course_id, activity_id)["students"], 1)
        self.assertEqual(
            self._placed(s2_token, course_id, activity_id)["room_id"], room_id
        )
        self.assertNotIn(f"participant:{ghost}", self._seats(room_id))
        self.assertIn(f"participant:{s1}", self._seats(room_id))

    async def test_direct_joins_need_a_seat(self):
        (_, teacher_token), course_id, activity_id = await self._setting()
        (seated, seated_token), (_, other_token) = await self._students(course_id, 2)
        room_id = self._placed(seated_token, course_id, activity_id)["room_id"]

        # A course member without a seat can't skip placement and its count.
        self.assertEqual(self._join(other_token, room_id).status_code, 403)
        # A course admin can read along, and a seat holder who left comes back.
        self.assertEqual(self._join(teacher_token, room_id).status_code, 200)
        left = requests.post(
            self._room_url(room_id, "leave"),
            headers=self._headers(seated_token),
            json={},
            timeout=10,
        )
        self.assertEqual(left.status_code, 200, left.text)
        self.assertEqual(self._join(seated_token, room_id).status_code, 200)

    async def test_a_student_made_room_with_forged_seats_is_never_used(self):
        _, course_id, activity_id = await self._setting()
        (forger, forger_token), (_, s2_token) = await self._students(course_id, 2)
        forged_seats = {
            f"participant:@victim{n}:{SERVER_NAME}": {
                "id": "participant",
                "user_id": f"@victim{n}:{SERVER_NAME}",
                "role": "Participant",
            }
            for n in range(3)
        }
        response = requests.post(
            self._url("/_matrix/client/v3/createRoom"),
            headers=self._headers(forger_token),
            json={
                "preset": "private_chat",
                "visibility": "private",
                "creation_content": {
                    "type": f"p.activity.session:{activity_id}",
                    "pangea.discussion": {
                        "course_id": course_id,
                        "activity_id": activity_id,
                    },
                },
                "initial_state": [
                    {
                        "type": PLAN,
                        "state_key": "",
                        "content": {"activity_id": activity_id, "mode": "discussion"},
                    },
                    {
                        "type": ROLES,
                        "state_key": "",
                        "content": {"roles": forged_seats},
                    },
                    {
                        "type": "m.room.join_rules",
                        "state_key": "",
                        "content": {
                            "join_rule": "knock_restricted",
                            "allow": [
                                {"type": "m.room_membership", "room_id": course_id}
                            ],
                        },
                    },
                ],
            },
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        forged_room = response.json()["room_id"]
        linked = requests.put(
            self._room_url(
                course_id, f"state/m.space.child/{quote(forged_room, safe='')}"
            ),
            headers=self._headers(forger_token),
            json={"via": [SERVER_NAME]},
            timeout=10,
        )
        self.assertEqual(linked.status_code, 200, linked.text)

        self.assertIsNone(self._preview(s2_token, course_id, activity_id)["room_id"])
        placed = self._placed(s2_token, course_id, activity_id)
        self.assertTrue(placed["created"])
        self.assertNotEqual(placed["room_id"], forged_room)
        # The forger's own tap goes to the server's room too.
        self.assertEqual(
            self._placed(forger_token, course_id, activity_id)["room_id"],
            placed["room_id"],
        )

    async def test_the_bot_alone_admits_students(self):
        (_, teacher_token), course_id, activity_id = await self._setting()
        (s1, s1_token), (s2, s2_token) = await self._students(course_id, 2)
        room_id = self._placed(s1_token, course_id, activity_id)["room_id"]

        # Nobody but the bot left in the room: no admin and no student.
        for token in (teacher_token, s1_token):
            self._join(token, room_id)
            response = requests.post(
                self._room_url(room_id, "leave"),
                headers=self._headers(token),
                json={},
                timeout=10,
            )
            self.assertEqual(response.status_code, 200, response.text)
        joined = {
            user_id
            for (event_type, user_id), event in self._state(room_id).items()
            if event_type == "m.room.member"
            and event["content"]["membership"] == "join"
        }
        self.assertEqual(joined, {BOT})

        self.assertEqual(
            self._placed(s2_token, course_id, activity_id)["room_id"], room_id
        )
        self.assertEqual(self._membership(room_id, s2), "join")

    async def test_course_admin_changes_reach_the_rooms(self):
        (teacher_id, teacher_token), course_id, activity_id = await self._setting()
        ((coteacher, coteacher_token),) = await self._students(
            course_id, 1, prefix="co"
        )
        ((_, s1_token),) = await self._students(course_id, 1)
        room_id = self._placed(s1_token, course_id, activity_id)["room_id"]

        def room_users() -> Dict[str, Any]:
            return self._state(room_id)[("m.room.power_levels", "")]["content"]["users"]

        self.assertEqual(
            self._set_power(teacher_token, course_id, coteacher, 100).status_code, 200
        )
        await self._eventually(
            lambda: room_users().get(coteacher) == 50, "the new admin at 50"
        )
        self.assertEqual(room_users()[BOT], 100)

        # A co-teacher who leaves the course stops being its admin.
        left = requests.post(
            self._room_url(course_id, "leave"),
            headers=self._headers(coteacher_token),
            json={},
            timeout=10,
        )
        self.assertEqual(left.status_code, 200, left.text)
        await self._eventually(
            lambda: coteacher not in room_users(), "the former admin removed"
        )
        self.assertEqual(room_users()[teacher_id], 50)


if __name__ == "__main__":
    unittest.main()
