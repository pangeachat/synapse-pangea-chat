"""Course default chats: the hook keeps Announcements and Introductions in
line with their space, and the repair endpoint does the same on demand.

Design: .github/instructions/course-default-chats.instructions.md
"""

import importlib.util
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

import requests

from .base_e2e import SERVER_NAME, BaseSynapseE2ETest

ANNOUNCEMENTS = "announcementsChat"
INTRODUCTIONS = "introductionChat"

# The client's restricted preset for Announcements: members read, power 50
# posts, power 100 changes power levels.
ANNOUNCEMENTS_POWER_LEVELS = {
    "ban": 50,
    "kick": 50,
    "invite": 50,
    "redact": 50,
    "events": {"m.room.power_levels": 100, "m.room.pinned_events": 50},
    "events_default": 50,
    "state_default": 50,
    "users_default": 0,
}

# The client's preset for Introductions: anyone posts, power 50 invites (so
# a member at the default level cannot let anyone in), power 100 changes
# power levels.
INTRODUCTIONS_POWER_LEVELS = {
    **ANNOUNCEMENTS_POWER_LEVELS,
    "events_default": 0,
}


class TestCourseDefaultChatsE2E(BaseSynapseE2ETest):
    _alias_counter = 0

    # -- request helpers -------------------------------------------------

    def _headers(self, token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _room_url(self, room_id: str, suffix: str) -> str:
        return f"{self.server_url}/_matrix/client/v3/rooms/{quote(room_id, safe='')}{suffix}"

    def _state_url(self, room_id: str, event_type: str, state_key: str = "") -> str:
        return self._room_url(
            room_id, f"/state/{quote(event_type, safe='')}/{quote(state_key, safe='')}"
        )

    def _endpoint(self) -> str:
        return f"{self.server_url}/_synapse/client/pangea/v1/sync_course_default_chats"

    async def _create_space(self, token: str, *, members_can_link: bool = False) -> str:
        body: Dict[str, Any] = {
            "visibility": "private",
            "preset": "public_chat",
            "creation_content": {"type": "m.space"},
            "name": "Test Course",
        }
        if members_can_link:
            # As production spaces are: any member may attach a child, so
            # learners' activity sessions can fan out into their courses.
            body["power_level_content_override"] = {"events": {"m.space.child": 0}}
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            headers=self._headers(token),
            json=body,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["room_id"]

    async def _create_child(
        self,
        token: str,
        space_id: str,
        *,
        alias_prefix: Optional[str],
        power_levels: Optional[Dict[str, Any]] = None,
    ) -> str:
        """A child chat as the client creates it: restricted to the space's
        members, and for a default chat an alias carrying the client's prefix."""
        type(self)._alias_counter += 1
        body: Dict[str, Any] = {
            "visibility": "private",
            "preset": "public_chat",
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
        if alias_prefix is not None:
            space_localpart = space_id.split(":", 1)[0][1:]
            body[
                "room_alias_name"
            ] = f"{alias_prefix}_{space_localpart}_{self.tag}_{self._alias_counter}"
        if power_levels is not None:
            body["power_level_content_override"] = power_levels
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            headers=self._headers(token),
            json=body,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["room_id"]

    async def _create_announcements(self, token: str, space_id: str) -> str:
        return await self._create_child(
            token,
            space_id,
            alias_prefix=ANNOUNCEMENTS,
            power_levels=ANNOUNCEMENTS_POWER_LEVELS,
        )

    async def _create_introductions(self, token: str, space_id: str) -> str:
        return await self._create_child(
            token,
            space_id,
            alias_prefix=INTRODUCTIONS,
            power_levels=INTRODUCTIONS_POWER_LEVELS,
        )

    async def _link_child(self, token: str, space_id: str, child_id: str) -> None:
        response = requests.put(
            self._state_url(space_id, "m.space.child", child_id),
            headers=self._headers(token),
            json={"via": [SERVER_NAME]},
        )
        self.assertEqual(response.status_code, 200, response.text)

    async def _join(self, room_id: str, token: str) -> None:
        """Join as a client would, waiting out the per-room join limit
        (burst ten, then one a second) when a test joins a whole class."""
        for _ in range(30):
            response = requests.post(
                f"{self.server_url}/_matrix/client/v3/join/{quote(room_id, safe='')}",
                headers=self._headers(token),
            )
            if response.status_code != 429:
                break
            time.sleep(response.json().get("retry_after_ms", 1000) / 1000)
        self.assertEqual(response.status_code, 200, response.text)

    async def _membership(
        self, room_id: str, user_id: str, token: str
    ) -> Optional[str]:
        response = requests.get(
            self._state_url(room_id, "m.room.member", user_id),
            headers=self._headers(token),
        )
        if response.status_code != 200:
            return None
        return response.json().get("membership")

    async def _power_users(self, room_id: str, token: str) -> Dict[str, int]:
        response = requests.get(
            self._state_url(room_id, "m.room.power_levels"),
            headers=self._headers(token),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return dict(response.json().get("users", {}))

    async def _set_power(
        self, room_id: str, user_id: str, level: int, admin_token: str
    ) -> None:
        response = requests.get(
            self._state_url(room_id, "m.room.power_levels"),
            headers=self._headers(admin_token),
        )
        self.assertEqual(response.status_code, 200, response.text)
        content = response.json()
        users = dict(content.get("users", {}))
        users[user_id] = level
        content["users"] = users
        response = requests.put(
            self._state_url(room_id, "m.room.power_levels"),
            headers=self._headers(admin_token),
            json=content,
        )
        self.assertEqual(response.status_code, 200, response.text)

    async def _membership_action(
        self, action: str, room_id: str, user_id: str, token: str
    ) -> None:
        """Kick or ban as a client would, waiting out the per-user action
        limit when a test does it to a whole class."""
        for _ in range(30):
            response = requests.post(
                self._room_url(room_id, f"/{action}"),
                headers=self._headers(token),
                json={"user_id": user_id},
            )
            if response.status_code != 429:
                break
            time.sleep(response.json().get("retry_after_ms", 1000) / 1000)
        self.assertEqual(response.status_code, 200, response.text)

    async def _send_message_status(self, room_id: str, token: str) -> int:
        response = requests.put(
            self._room_url(room_id, f"/send/m.room.message/{time.time_ns()}"),
            headers=self._headers(token),
            json={"msgtype": "m.text", "body": "hello"},
        )
        return response.status_code

    async def _wait_joined(self, room_id: str, user_ids: List[str], token: str) -> None:
        async def joined() -> bool:
            for user_id in user_ids:
                if await self._membership(room_id, user_id, token) != "join":
                    return False
            return True

        await self._wait_until_async(joined, f"{user_ids} joined to {room_id}")

    async def _wait_until_async(
        self, condition: Callable[[], Any], what: str, timeout: float = 20.0
    ) -> None:
        """The hook does its work in the background, so state is eventual."""
        deadline = time.monotonic() + timeout
        while True:
            if await condition():
                return
            if time.monotonic() > deadline:
                self.fail(f"Timed out waiting for {what}")
            time.sleep(0.25)

    # -- the hook ----------------------------------------------------------

    async def test_linking_a_default_chat_joins_members_and_mirrors_admins(self):
        await self.homeserver()
        teacher_id, teacher = await self.new_user("teacher")
        co_id, co = await self.new_user("co")
        s1_id, s1 = await self.new_user("s1")
        s2_id, s2 = await self.new_user("s2")

        space_id = await self._create_space(teacher)
        for token in (co, s1, s2):
            await self._join(space_id, token)
        await self._set_power(space_id, co_id, 100, teacher)

        chat_id = await self._create_announcements(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)

        await self._wait_joined(chat_id, [co_id, s1_id, s2_id], teacher)
        users = await self._power_users(chat_id, teacher)
        self.assertEqual(users.get(teacher_id), 100)
        self.assertEqual(users.get(co_id), 100)
        self.assertNotIn(s1_id, users)
        self.assertNotIn(s2_id, users)
        # What the issue was about: the co-instructor can post, a student can't.
        self.assertEqual(await self._send_message_status(chat_id, co), 200)
        self.assertEqual(await self._send_message_status(chat_id, s1), 403)

    async def test_joining_the_space_later_lands_in_both_chats(self):
        await self.homeserver()
        _, teacher = await self.new_user("teacher")
        s3_id, s3 = await self.new_user("s3")

        space_id = await self._create_space(teacher)
        announcements_id = await self._create_announcements(teacher, space_id)
        await self._link_child(teacher, space_id, announcements_id)
        introductions_id = await self._create_child(
            teacher, space_id, alias_prefix=INTRODUCTIONS
        )
        await self._link_child(teacher, space_id, introductions_id)

        await self._join(space_id, s3)

        await self._wait_joined(announcements_id, [s3_id], teacher)
        await self._wait_joined(introductions_id, [s3_id], teacher)

    async def test_promotion_and_demotion_follow_the_space(self):
        await self.homeserver()
        teacher_id, teacher = await self.new_user("teacher")
        s1_id, s1 = await self.new_user("s1")

        space_id = await self._create_space(teacher)
        await self._join(space_id, s1)
        chat_id = await self._create_announcements(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)
        await self._wait_joined(chat_id, [s1_id], teacher)

        await self._set_power(space_id, s1_id, 100, teacher)

        async def promoted() -> bool:
            return (await self._power_users(chat_id, teacher)).get(s1_id) == 100

        await self._wait_until_async(promoted, "s1 promoted in the chat")
        self.assertEqual(await self._send_message_status(chat_id, s1), 200)

        # Matrix lets an admin lower only their own level, so an admin who
        # stops being one steps down themselves.
        await self._set_power(space_id, s1_id, 0, s1)

        async def demoted() -> bool:
            return (await self._power_users(chat_id, teacher)).get(s1_id, 0) < 100

        await self._wait_until_async(demoted, "s1 demoted in the chat")
        users = await self._power_users(chat_id, teacher)
        self.assertEqual(users.get(teacher_id), 100)
        self.assertEqual(await self._send_message_status(chat_id, s1), 403)

    async def test_bots_and_banned_members_are_skipped(self):
        await self.homeserver()
        _, teacher = await self.new_user("teacher")
        banned_id, banned = await self.new_user("banned")
        s2_id, s2 = await self.new_user("s2")
        bot_localpart = f"bot-{self.tag}"
        await self.register_user("", "", bot_localpart, "pw", False)
        bot_id, bot = await self.login_user(bot_localpart, "pw")

        space_id = await self._create_space(teacher)
        for token in (banned, s2, bot):
            await self._join(space_id, token)
        await self._set_power(space_id, bot_id, 100, teacher)

        chat_id = await self._create_announcements(teacher, space_id)
        await self._membership_action("ban", chat_id, banned_id, teacher)
        await self._link_child(teacher, space_id, chat_id)

        await self._wait_joined(chat_id, [s2_id], teacher)
        self.assertEqual(await self._membership(chat_id, banned_id, teacher), "ban")
        self.assertIsNone(await self._membership(chat_id, bot_id, teacher))
        self.assertNotIn(bot_id, await self._power_users(chat_id, teacher))

    async def test_other_children_and_other_rooms_are_ignored(self):
        await self.homeserver()
        _, teacher = await self.new_user("teacher")
        s1_id, s1 = await self.new_user("s1")

        space_id = await self._create_space(teacher)
        await self._join(space_id, s1)
        plain_child_id = await self._create_child(teacher, space_id, alias_prefix=None)
        await self._link_child(teacher, space_id, plain_child_id)
        # A plain room (not a space) with a default-chat-looking child
        # linked to it: the rule applies to spaces only.
        plain_room_id = await self.create_private_room(teacher)
        chat_of_plain_room = await self._create_child(
            teacher, plain_room_id, alias_prefix=ANNOUNCEMENTS
        )
        await self._link_child(teacher, plain_room_id, chat_of_plain_room)

        # The positive control: a real default chat of the space syncs.
        chat_id = await self._create_announcements(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)
        await self._wait_joined(chat_id, [s1_id], teacher)

        self.assertIsNone(await self._membership(plain_child_id, s1_id, teacher))
        self.assertIsNone(await self._membership(chat_of_plain_room, s1_id, teacher))

    async def test_a_prefixed_room_from_a_student_is_not_a_default_chat(self):
        """Any member can link a child. A student's room wearing the alias
        prefix is left alone and named in the report; the teacher's is synced."""
        await self.homeserver()
        _, operator = await self.new_user("operator", admin=True)
        teacher_id, teacher = await self.new_user("teacher")
        s1_id, s1 = await self.new_user("s1")

        space_id = await self._create_space(teacher, members_can_link=True)
        await self._join(space_id, s1)
        fake_id = await self._create_child(s1, space_id, alias_prefix=ANNOUNCEMENTS)
        await self._link_child(s1, space_id, fake_id)
        time.sleep(3)
        self.assertIsNone(await self._membership(fake_id, teacher_id, s1))
        self.assertEqual((await self._power_users(fake_id, s1)).get(s1_id), 100)

        response = requests.post(
            self._endpoint(),
            headers=self._headers(operator),
            json={"room_id": space_id, "dry_run": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        chats = {c["chat_id"]: c for c in response.json()["chats"]}
        self.assertTrue(chats[fake_id]["ignored"])
        self.assertEqual(chats[fake_id]["joined"], [])

        # The positive control: the teacher's own default chat still syncs.
        chat_id = await self._create_announcements(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)
        await self._wait_joined(chat_id, [s1_id], teacher)
        self.assertIsNone(await self._membership(fake_id, teacher_id, s1))

    async def test_a_space_whose_admins_all_left_is_reported_as_such(self):
        await self.homeserver()
        _, operator = await self.new_user("operator", admin=True)
        _, teacher = await self.new_user("teacher")
        s1_id, s1 = await self.new_user("s1")

        space_id = await self._create_space(teacher)
        await self._join(space_id, s1)
        chat_id = await self._create_announcements(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)
        await self._wait_joined(chat_id, [s1_id], teacher)
        response = requests.post(
            self._room_url(space_id, "/leave"), headers=self._headers(teacher)
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = requests.post(
            self._endpoint(),
            headers=self._headers(operator),
            json={"room_id": space_id, "dry_run": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        chat = response.json()["chats"][0]
        self.assertEqual(chat["ignored"], "the space has no joined admin")

    async def test_a_class_larger_than_the_join_burst_is_joined(self):
        """Synapse's per-room join limit allows a burst of ten; the sync
        waits the limiter out for the rest instead of dropping them."""
        await self.homeserver()
        _, teacher = await self.new_user("teacher")
        students = [await self.new_user(f"s{i}") for i in range(12)]

        space_id = await self._create_space(teacher)
        for _, token in students:
            await self._join(space_id, token)
        chat_id = await self._create_announcements(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)

        await self._wait_joined(chat_id, [user_id for user_id, _ in students], teacher)
        # The limiter did fire and was waited out, not bypassed.
        self.assertTrue(
            any("course_default_chats waiting" in line for line in self.server_log()),
            "expected the sync to wait out the per-room join limit",
        )

    async def test_profile_change_does_not_rejoin_a_removed_member(self):
        """Matrix sends a member's join again on a displayname change; that
        is not a join, so a kicked member stays out until a full sync."""
        await self.homeserver()
        _, teacher = await self.new_user("teacher")
        s1_id, s1 = await self.new_user("s1")

        space_id = await self._create_space(teacher)
        await self._join(space_id, s1)
        chat_id = await self._create_announcements(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)
        await self._wait_joined(chat_id, [s1_id], teacher)
        await self._membership_action("kick", chat_id, s1_id, teacher)

        response = requests.put(
            self._state_url(space_id, "m.room.member", s1_id),
            headers=self._headers(s1),
            json={"membership": "join", "displayname": "Renamed"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        time.sleep(3)
        self.assertEqual(await self._membership(chat_id, s1_id, teacher), "leave")

        # The positive control: a full sync (any power change on the space)
        # does put a removed member back, as the design says.
        await self._set_power(space_id, s1_id, 50, teacher)
        await self._wait_joined(chat_id, [s1_id], teacher)

    async def test_demotion_never_strands_the_chat(self):
        """The teacher has left Announcements and the co-teacher who created
        it steps down in the space. The teacher is joined again before the
        co-teacher is demoted, so someone can still let members in."""
        await self.homeserver()
        teacher_id, teacher = await self.new_user("teacher")
        co_id, co = await self.new_user("co")
        s1_id, s1 = await self.new_user("s1")

        space_id = await self._create_space(teacher)
        await self._join(space_id, co)
        await self._set_power(space_id, co_id, 100, teacher)
        chat_id = await self._create_announcements(co, space_id)
        await self._link_child(co, space_id, chat_id)
        await self._wait_joined(chat_id, [teacher_id], teacher)
        response = requests.post(
            self._room_url(chat_id, "/leave"), headers=self._headers(teacher)
        )
        self.assertEqual(response.status_code, 200, response.text)

        await self._set_power(space_id, co_id, 0, co)

        await self._wait_joined(chat_id, [teacher_id], co)

        async def settled() -> bool:
            users = await self._power_users(chat_id, co)
            return users.get(teacher_id) == 100 and users.get(co_id, 0) < 100

        await self._wait_until_async(settled, "co-teacher demoted after the rejoin")
        self.assertEqual(await self._membership(chat_id, co_id, co), "join")
        self.assertEqual(await self._send_message_status(chat_id, teacher), 200)
        # Not stranded: a student joining the space still lands in the chat.
        await self._join(space_id, s1)
        await self._wait_joined(chat_id, [s1_id], teacher)

    async def test_a_chat_nobody_can_act_for_is_reported_not_repaired(self):
        await self.homeserver()
        _, operator = await self.new_user("operator", admin=True)
        teacher_id, teacher = await self.new_user("teacher")
        s1_id, s1 = await self.new_user("s1")

        space_id = await self._create_space(teacher)
        chat_id = await self._create_announcements(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)
        response = requests.post(
            self._room_url(chat_id, "/leave"), headers=self._headers(teacher)
        )
        self.assertEqual(response.status_code, 200, response.text)
        await self._join(space_id, s1)

        response = requests.post(
            self._endpoint(),
            headers=self._headers(operator),
            json={"room_id": space_id, "dry_run": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        chat = response.json()["chats"][0]
        self.assertTrue(chat["admin_sender_missing"])
        self.assertEqual(chat["joined"], [])
        # Both space members are outside the chat now, and nobody can let
        # either in: the teacher who left it, and the student who never was.
        self.assertCountEqual(
            [(f["user_id"], f["step"]) for f in chat["failed"]],
            [(s1_id, "join"), (teacher_id, "join")],
        )
        self.assertTrue(all("NoChatAdminError" in f["error"] for f in chat["failed"]))
        self.assertIsNone(await self._membership(chat_id, s1_id, teacher))
        self.assertTrue(
            any(
                "no joined member can let members in" in line
                for line in self.server_log()
            )
        )

    # -- the repair endpoint -------------------------------------------------

    async def test_repair_endpoint_brings_a_space_back_in_line(self):
        await self.homeserver()
        _, operator = await self.new_user("operator", admin=True)
        teacher_id, teacher = await self.new_user("teacher")
        co_id, co = await self.new_user("co")
        s1_id, s1 = await self.new_user("s1")

        space_id = await self._create_space(teacher)
        await self._join(space_id, co)
        await self._join(space_id, s1)
        await self._set_power(space_id, co_id, 100, teacher)
        chat_id = await self._create_announcements(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)
        await self._wait_joined(chat_id, [co_id, s1_id], teacher)

        # Put the chat out of line without touching the space: a kick, and
        # the co-teacher stepping down inside the chat (the chat is not a
        # space, so neither triggers the hook).
        await self._membership_action("kick", chat_id, s1_id, teacher)
        await self._set_power(chat_id, co_id, 0, co)
        self.assertEqual(await self._membership(chat_id, s1_id, teacher), "leave")

        response = requests.post(
            self._endpoint(),
            headers=self._headers(operator),
            json={"room_id": space_id},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(
            response.json(), {"room_id": space_id, "dry_run": False, "scheduled": True}
        )
        await self._wait_joined(chat_id, [s1_id], teacher)

        async def promoted() -> bool:
            return (await self._power_users(chat_id, teacher)).get(co_id) == 100

        await self._wait_until_async(promoted, "co-teacher promoted by the repair")
        response = requests.post(
            self._endpoint(),
            headers=self._headers(operator),
            json={"room_id": space_id, "dry_run": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        chat = response.json()["chats"][0]
        self.assertEqual(chat["chat_id"], chat_id)
        self.assertEqual(
            (chat["promoted"], chat["demoted"], chat["joined"]), ([], [], [])
        )
        self.assertEqual(chat["failed"], [])

    async def test_dry_run_does_not_wait_for_a_running_sync(self):
        """An operator polls a repair with dry runs. The repair holds the chat's
        lock for as long as the paced joins take, so a dry run that waited for
        it would sit longer than the reverse proxy allows a request."""
        await self.homeserver()
        _, operator = await self.new_user("operator", admin=True)
        _, teacher = await self.new_user("teacher")
        students = [await self.new_user(f"s{i}") for i in range(12)]

        space_id = await self._create_space(teacher)
        chat_id = await self._create_announcements(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)
        for _, token in students:
            await self._join(space_id, token)
        await self._wait_joined(chat_id, [user_id for user_id, _ in students], teacher)
        for user_id, _ in students:
            await self._membership_action("kick", chat_id, user_id, teacher)

        response = requests.post(
            self._endpoint(),
            headers=self._headers(operator),
            json={"room_id": space_id},
        )
        self.assertEqual(response.status_code, 202, response.text)
        started = time.monotonic()
        response = requests.post(
            self._endpoint(),
            headers=self._headers(operator),
            json={"room_id": space_id, "dry_run": True},
        )
        elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 200, response.text)
        # It answered while the paced joins were still running (the last two
        # of twelve wait out the per-room limit), not after they finished.
        self.assertGreater(len(response.json()["chats"][0]["joined"]), 0)
        self.assertLess(elapsed, 1.5)
        await self._wait_joined(chat_id, [user_id for user_id, _ in students], teacher)

    async def test_repair_endpoint_refuses_non_admins_and_bad_input(self):
        await self.homeserver()
        _, operator = await self.new_user("operator", admin=True)
        _, teacher = await self.new_user("teacher")
        plain_room_id = await self.create_private_room(teacher)
        space_id = await self._create_space(teacher)

        for token, body, status in (
            (teacher, {"room_id": space_id}, 403),
            (operator, {"room_id": plain_room_id}, 400),
            (operator, {"room_id": "nope"}, 400),
            # One space per call: no room id is not "every space".
            (operator, {}, 400),
            (operator, {"room_id": space_id, "dry_run": "yes"}, 400),
        ):
            response = requests.post(
                self._endpoint(), headers=self._headers(token), json=body
            )
            self.assertEqual(response.status_code, status, (body, response.text))

    async def test_repair_endpoint_dry_run_reports_without_changing(self):
        await self.homeserver()
        _, operator = await self.new_user("operator", admin=True)
        _, teacher = await self.new_user("teacher")
        s1_id, s1 = await self.new_user("s1")

        space_id = await self._create_space(teacher)
        await self._join(space_id, s1)
        chat_id = await self._create_announcements(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)
        await self._wait_joined(chat_id, [s1_id], teacher)
        await self._membership_action("kick", chat_id, s1_id, teacher)

        response = requests.post(
            self._endpoint(),
            headers=self._headers(operator),
            json={"room_id": space_id, "dry_run": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["chats"][0]["joined"], [s1_id])
        self.assertEqual(await self._membership(chat_id, s1_id, teacher), "leave")

    async def test_repair_script_lists_then_repairs(self):
        """The operator script: the dry-run walk finds the space, and
        --apply repairs exactly the spaces it listed, waiting for the
        background work to finish."""
        await self.homeserver()
        _, operator = await self.new_user("operator", admin=True)
        _, teacher = await self.new_user("teacher")
        s1_id, s1 = await self.new_user("s1")

        space_id = await self._create_space(teacher)
        await self._join(space_id, s1)
        chat_id = await self._create_announcements(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)
        await self._wait_joined(chat_id, [s1_id], teacher)
        await self._membership_action("kick", chat_id, s1_id, teacher)

        script = _load_repair_script()
        self.assertIn(space_id, list(script.walk_spaces(self.server_url, operator)))
        before = script.dry_run(self.server_url, operator, space_id)
        self.assertTrue(script.needs_attention(before))
        self.assertEqual(await self._membership(chat_id, s1_id, teacher), "leave")

        after = script.repair(self.server_url, operator, space_id, wait_seconds=30)
        self.assertFalse(script.needs_attention(after))
        self.assertEqual(await self._membership(chat_id, s1_id, teacher), "join")

    # -- the recovery endpoint -----------------------------------------------

    def _recover_endpoint(self) -> str:
        return (
            f"{self.server_url}/_synapse/client/pangea/v1/recover_course_default_chat"
        )

    def _recover(self, token: str, body: Dict[str, Any]) -> requests.Response:
        return requests.post(
            self._recover_endpoint(), headers=self._headers(token), json=body
        )

    async def _leave(self, room_id: str, token: str) -> None:
        response = requests.post(
            self._room_url(room_id, "/leave"), headers=self._headers(token)
        )
        self.assertEqual(response.status_code, 200, response.text)

    async def _dead_introductions(self) -> Dict[str, Any]:
        """A course whose Introductions chat nobody in it can act for: the
        teacher created it, the co-teacher was mirrored in as an admin, both
        then left the chat, and the student still in it holds no power."""
        await self.homeserver()
        _, operator = await self.new_user("operator", admin=True)
        teacher_id, teacher = await self.new_user("teacher")
        co_id, co = await self.new_user("co")
        s1_id, s1 = await self.new_user("s1")

        space_id = await self._create_space(teacher)
        await self._join(space_id, co)
        await self._join(space_id, s1)
        await self._set_power(space_id, co_id, 100, teacher)
        chat_id = await self._create_introductions(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)
        await self._wait_joined(chat_id, [co_id, s1_id], teacher)

        async def co_mirrored() -> bool:
            return (await self._power_users(chat_id, teacher)).get(co_id) == 100

        await self._wait_until_async(co_mirrored, "co-teacher mirrored as admin")
        await self._leave(chat_id, co)
        await self._leave(chat_id, teacher)
        return {
            "operator": operator,
            "teacher": (teacher_id, teacher),
            "co": (co_id, co),
            "s1": (s1_id, s1),
            "space_id": space_id,
            "chat_id": chat_id,
        }

    async def test_recovery_restores_a_space_admin_and_the_repair_lets_the_class_in(
        self,
    ):
        course = await self._dead_introductions()
        operator, space_id, chat_id = (
            course["operator"],
            course["space_id"],
            course["chat_id"],
        )
        teacher_id, teacher = course["teacher"]
        _, s1 = course["s1"]
        # A student who joins the course now cannot get into the chat, and a
        # new space admin cannot be mirrored into it.
        s2_id, s2 = await self.new_user("s2")
        new_id, new = await self.new_user("new")
        await self._join(space_id, s2)
        await self._join(space_id, new)
        await self._set_power(space_id, new_id, 100, teacher)
        time.sleep(2)
        self.assertIsNone(await self._membership(chat_id, s2_id, s1))
        self.assertIsNone(await self._membership(chat_id, new_id, s1))

        # The teacher who left still holds admin power there: only the join.
        response = self._recover(
            operator,
            {
                "room_id": chat_id,
                "space_id": space_id,
                "user_id": teacher_id,
                "dry_run": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            (response.json()["join"], response.json()["promote"]), (True, False)
        )
        # A space admin who was never in the chat: the join and the promotion.
        body = {"room_id": chat_id, "space_id": space_id, "user_id": new_id}
        response = self._recover(operator, {**body, "dry_run": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            (response.json()["join"], response.json()["promote"]), (True, True)
        )
        # A dry run writes nothing.
        self.assertIsNone(await self._membership(chat_id, new_id, s1))

        response = self._recover(operator, body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {**body, "dry_run": False, "joined": True, "promoted": True},
        )
        self.assertEqual(await self._membership(chat_id, new_id, s1), "join")
        self.assertEqual((await self._power_users(chat_id, s1)).get(new_id), 100)
        # The restored admin really holds the room: the server lists it among
        # their rooms, and they can do what only an admin can.
        joined_rooms = requests.get(
            f"{self.server_url}/_matrix/client/v3/joined_rooms",
            headers=self._headers(new),
        ).json()["joined_rooms"]
        self.assertIn(chat_id, joined_rooms)
        await self._set_power(chat_id, s2_id, 0, new)
        self.assertTrue(
            any(
                "course_default_chats recovered" in line and new_id in line
                for line in self.server_log()
            )
        )

        # Recovery done: the chat is an ordinary default chat, so a second
        # recovery is refused and the repair lets the class in, including the
        # teacher who had left.
        response = self._recover(operator, body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["reason"], "chat_has_admin")
        response = requests.post(
            self._endpoint(),
            headers=self._headers(operator),
            json={"room_id": space_id},
        )
        self.assertEqual(response.status_code, 202, response.text)
        await self._wait_joined(chat_id, [s2_id, teacher_id], new)

    async def test_recovery_refuses_to_restore_a_student(self):
        course = await self._dead_introductions()
        operator, space_id, chat_id = (
            course["operator"],
            course["space_id"],
            course["chat_id"],
        )
        s1_id, s1 = course["s1"]
        co_id, _ = course["co"]

        response = self._recover(
            operator, {"room_id": chat_id, "space_id": space_id, "user_id": s1_id}
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["errcode"], "ORG.PANGEA.RECOVERY_REFUSED")
        self.assertEqual(response.json()["reason"], "not_space_admin")
        self.assertEqual((await self._power_users(chat_id, s1)).get(s1_id, 0), 0)
        # The positive control: the co-teacher, a space admin, is accepted.
        response = self._recover(
            operator,
            {
                "room_id": chat_id,
                "space_id": space_id,
                "user_id": co_id,
                "dry_run": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    async def test_recovery_refuses_a_space_admin_banned_from_the_chat(self):
        await self.homeserver()
        _, operator = await self.new_user("operator", admin=True)
        teacher_id, teacher = await self.new_user("teacher")
        banned_id, banned = await self.new_user("banned")
        s1_id, s1 = await self.new_user("s1")

        space_id = await self._create_space(teacher)
        await self._join(space_id, s1)
        chat_id = await self._create_introductions(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)
        await self._wait_joined(chat_id, [s1_id], teacher)
        await self._membership_action("ban", chat_id, banned_id, teacher)
        await self._join(space_id, banned)
        await self._set_power(space_id, banned_id, 100, teacher)
        await self._leave(chat_id, teacher)

        body = {"room_id": chat_id, "space_id": space_id, "user_id": banned_id}
        response = self._recover(operator, body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["reason"], "banned")
        self.assertEqual(await self._membership(chat_id, banned_id, s1), "ban")
        # The positive control: the teacher, not banned, is accepted.
        response = self._recover(
            operator, {**body, "user_id": teacher_id, "dry_run": True}
        )
        self.assertEqual(response.status_code, 200, response.text)

    async def test_recovery_refuses_a_healthy_chat_non_admins_and_bad_input(self):
        await self.homeserver()
        _, operator = await self.new_user("operator", admin=True)
        teacher_id, teacher = await self.new_user("teacher")
        space_id = await self._create_space(teacher)
        chat_id = await self._create_announcements(teacher, space_id)
        await self._link_child(teacher, space_id, chat_id)
        plain_child_id = await self._create_child(teacher, space_id, alias_prefix=None)
        await self._link_child(teacher, space_id, plain_child_id)
        good = {"room_id": chat_id, "space_id": space_id, "user_id": teacher_id}

        for token, body, status, reason in (
            (teacher, good, 403, None),
            (operator, {**good, "dry_run": "yes"}, 400, None),
            (operator, {**good, "user_id": "nope"}, 400, None),
            (operator, {**good, "user_id": "@someone:elsewhere.org"}, 400, None),
            (operator, {**good, "space_id": chat_id}, 400, None),
            (operator, {**good, "room_id": "nope"}, 400, None),
            # The teacher is still in the chat: the repair is the tool.
            (operator, good, 409, "chat_has_admin"),
            (operator, {**good, "room_id": plain_child_id}, 409, "not_a_default_chat"),
        ):
            response = self._recover(token, body)
            self.assertEqual(response.status_code, status, (body, response.text))
            if reason is not None:
                self.assertEqual(response.json()["reason"], reason, body)


def _load_repair_script() -> Any:
    """scripts/ is not a package; the script is loaded by path."""
    path = (
        Path(__file__).resolve().parent.parent
        / "scripts"
        / "repair_course_default_chats.py"
    )
    spec = importlib.util.spec_from_file_location("repair_course_default_chats", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
