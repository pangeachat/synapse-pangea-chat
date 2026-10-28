"""Course session admins: a new activity session linked into a course joins
the course's admins, listed in their pangea.admin_review account data and
muted, and touches nobody and nothing else.
"""

import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

import requests

from .base_e2e import SERVER_NAME, BaseSynapseE2ETest

SESSION_TYPE = "p.activity.session:act-1"


class TestCourseSessionAdminsE2E(BaseSynapseE2ETest):
    # -- request helpers -------------------------------------------------

    def _headers(self, token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _state_url(self, room_id: str, event_type: str, state_key: str = "") -> str:
        return (
            f"{self.server_url}/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
            f"/state/{quote(event_type, safe='')}/{quote(state_key, safe='')}"
        )

    def _create_room(self, token: str, body: Dict[str, Any]) -> str:
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            headers=self._headers(token),
            json={"visibility": "private", **body},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["room_id"]

    def _create_course(self, token: str) -> str:
        """A course space as production has it: any member may link a child,
        so learners' sessions fan out into their courses."""
        return self._create_room(
            token,
            {
                "preset": "public_chat",
                "creation_content": {"type": "m.space"},
                "power_level_content_override": {"events": {"m.space.child": 0}},
            },
        )

    def _create_child(
        self, token: str, space_id: str, create_type: Optional[str]
    ) -> str:
        """A room restricted to the course's members, as the client creates a
        session (``create_type`` set) or any other course chat."""
        body: Dict[str, Any] = {
            "preset": "private_chat",
            "initial_state": [
                {
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "content": {
                        "join_rule": "knock_restricted",
                        "allow": [{"type": "m.room_membership", "room_id": space_id}],
                    },
                }
            ],
        }
        if create_type is not None:
            body["creation_content"] = {"type": create_type}
        return self._create_room(token, body)

    def _link(
        self, token: str, space_id: str, child_id: str, via: Optional[list] = None
    ) -> None:
        response = requests.put(
            self._state_url(space_id, "m.space.child", child_id),
            headers=self._headers(token),
            json={"via": via or [SERVER_NAME]},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _join(self, room_id: str, token: str) -> None:
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/join/{quote(room_id, safe='')}",
            headers=self._headers(token),
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _set_power(self, room_id: str, user_id: str, level: int, token: str) -> None:
        url = self._state_url(room_id, "m.room.power_levels")
        content = requests.get(url, headers=self._headers(token)).json()
        content["users"] = {**content.get("users", {}), user_id: level}
        response = requests.put(url, headers=self._headers(token), json=content)
        self.assertEqual(response.status_code, 200, response.text)

    def _membership(self, room_id: str, user_id: str, token: str) -> Optional[str]:
        """``user_id``'s membership, read by a joined member; None when the
        user never had a member event (a 404, asserted, not any failure)."""
        response = requests.get(
            self._state_url(room_id, "m.room.member", user_id),
            headers=self._headers(token),
        )
        if response.status_code == 404:
            return None
        self.assertEqual(response.status_code, 200, response.text)
        return response.json().get("membership")

    def _review_list(self, user_id: str, token: str) -> list:
        response = requests.get(
            f"{self.server_url}/_matrix/client/v3/user/{quote(user_id, safe='')}"
            "/account_data/pangea.admin_review",
            headers=self._headers(token),
        )
        if response.status_code == 404:
            return []
        self.assertEqual(response.status_code, 200, response.text)
        return response.json().get("room_ids", [])

    def _mute_rule(self, room_id: str, token: str) -> Optional[Dict[str, Any]]:
        response = requests.get(
            f"{self.server_url}/_matrix/client/v3/pushrules/global/override/"
            f"{quote(room_id, safe='')}",
            headers=self._headers(token),
        )
        if response.status_code == 404:
            return None
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _wait_until(
        self, condition: Callable[[], bool], what: str, timeout: float = 20.0
    ) -> None:
        """The hook does its work in the background, so state is eventual."""
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"Timed out waiting for {what}")
            time.sleep(0.25)

    def _wait_handled(self, marker: str) -> None:
        """Wait for the module's own log line about a link, so a negative
        assertion is made after the link was handled, not before."""
        self._wait_until(
            lambda: any(marker in line for line in self.server_log()),
            f"the log line {marker!r}",
        )

    def _admitted(self, room_id: str, user_id: str, token: str, reader: str) -> bool:
        """Joined, on their review list and muted: the whole outcome, polled
        together because the mute is written after the join."""
        rule = self._mute_rule(room_id, token)
        return (
            self._membership(room_id, user_id, reader) == "join"
            and room_id in self._review_list(user_id, token)
            and rule is not None
            and rule.get("actions") == []
            and rule.get("conditions")
            == [{"kind": "event_match", "key": "room_id", "pattern": room_id}]
        )

    async def _course(self):
        """A course with a teacher, a co-teacher at admin power and two
        students; answers (space_id, users) where users maps name to
        (user_id, token)."""
        await self.homeserver()
        users = {
            name: await self.new_user(name) for name in ("teacher", "co", "s1", "s2")
        }
        teacher_token = users["teacher"][1]
        space_id = self._create_course(teacher_token)
        for name in ("co", "s1", "s2"):
            self._join(space_id, users[name][1])
        self._set_power(space_id, users["co"][0], 100, teacher_token)
        return space_id, users

    # -- tests -------------------------------------------------------------

    async def test_a_new_session_joins_the_course_admins_hidden_and_muted(self):
        space_id, users = await self._course()
        s1_id, s1 = users["s1"]
        session_id = self._create_child(s1, space_id, SESSION_TYPE)
        self._link(s1, space_id, session_id)

        for name in ("teacher", "co"):
            user_id, token = users[name]
            self._wait_until(
                lambda: self._admitted(session_id, user_id, token, s1),
                f"{name} joined to the session, listed and muted",
            )
        self._wait_handled(f"course_session_admins {session_id} linked into {space_id}")

        # A course member who is not an admin is never brought in.
        s2_id, s2 = users["s2"]
        self.assertIsNone(self._membership(session_id, s2_id, s1))
        self.assertNotIn(session_id, self._review_list(s2_id, s2))
        # The student who launched it is still a member, and their own session
        # is not on any list of theirs.
        self.assertEqual(self._membership(session_id, s1_id, s1), "join")
        self.assertNotIn(session_id, self._review_list(s1_id, s1))

    async def test_other_rooms_and_unlinked_sessions_are_left_alone(self):
        space_id, users = await self._course()
        s1 = users["s1"][1]
        teacher_id, teacher = users["teacher"]
        chat_id = self._create_child(s1, space_id, None)
        self._link(s1, space_id, chat_id)
        self._wait_handled(
            f"course_session_admins {chat_id} linked into {space_id}: "
            "not an activity session"
        )
        # Never linked, so nothing can touch it.
        unlinked_id = self._create_child(s1, space_id, SESSION_TYPE)

        self.assertIsNone(self._membership(chat_id, teacher_id, s1))
        self.assertIsNone(self._membership(unlinked_id, teacher_id, s1))
        listed = self._review_list(teacher_id, teacher)
        self.assertNotIn(chat_id, listed)
        self.assertNotIn(unlinked_id, listed)

    async def test_a_link_sent_again_is_not_a_backfill(self):
        space_id, users = await self._course()
        s1 = users["s1"][1]
        s2_id, _ = users["s2"]
        teacher_id, teacher = users["teacher"]
        session_id = self._create_child(s1, space_id, SESSION_TYPE)
        self._link(s1, space_id, session_id)
        self._wait_until(
            lambda: self._admitted(session_id, teacher_id, teacher, s1),
            "the teacher joined to the session",
        )
        # s2 becomes an admin only after the session was shared; sending the
        # link again (a new event replacing the first) must not join them.
        self._set_power(space_id, s2_id, 100, teacher)
        self._link(s1, space_id, session_id, via=[SERVER_NAME, "other.example"])
        self._wait_handled(
            f"course_session_admins {session_id} linked into {space_id} again"
        )
        self.assertIsNone(self._membership(session_id, s2_id, s1))

        # A second new session joins the teacher too, and both stay listed:
        # adding one never drops another from the list.
        second_id = self._create_child(s1, space_id, SESSION_TYPE)
        self._link(s1, space_id, second_id)
        self._wait_until(
            lambda: self._admitted(second_id, teacher_id, teacher, s1),
            "the teacher joined to the second session",
        )
        listed = self._review_list(teacher_id, teacher)
        self.assertIn(session_id, listed)
        self.assertIn(second_id, listed)

    async def test_a_session_that_does_not_admit_the_course_is_ignored(self):
        # Any course member may link a child, so a learner could link a
        # session-typed room that is public, invite-only or restricted to
        # another course; none of them pulls the course's admins in.
        space_id, users = await self._course()
        s1 = users["s1"][1]
        teacher_id, teacher = users["teacher"]
        other_space = self._create_course(s1)
        public_id = self._create_room(
            s1, {"preset": "public_chat", "creation_content": {"type": SESSION_TYPE}}
        )
        invite_id = self._create_room(
            s1, {"preset": "private_chat", "creation_content": {"type": SESSION_TYPE}}
        )
        elsewhere_id = self._create_child(s1, other_space, SESSION_TYPE)
        for room_id in (public_id, invite_id, elsewhere_id):
            self._link(s1, space_id, room_id)
            self._wait_handled(
                f"course_session_admins {room_id} linked into {space_id}: "
                "its join rule does not admit the course"
            )
            self.assertIsNone(self._membership(room_id, teacher_id, s1))
            self.assertNotIn(room_id, self._review_list(teacher_id, teacher))
