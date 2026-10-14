"""Student invitation codes (invite-by-email.instructions.md): one code per invited address,
never spent, each join through it recorded against the invitation."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from synapse_pangea_chat.email_invite.course_claims import CourseClaimStore
from synapse_pangea_chat.email_invite.student_invitation_api import StudentInvitationAPI
from synapse_pangea_chat.room_code.code_lookup import code_is_taken
from tests.moderation_doubles import DbPoolDouble

ROOM = "!course:x"


class TestStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        hs = MagicMock()
        hs.get_datastores.return_value.main.db_pool = DbPoolDouble()
        self.claims = CourseClaimStore(hs)
        await self.claims._ensure_table()
        self.store = self.claims.students

    async def test_a_code_names_its_invitation_and_every_use_is_recorded(self):
        ident = await self.store.create(
            ROOM, "ann@school.example", "abc2def", "@t:x", 100
        )
        found = await self.store.for_code("ABC2DEF")  # case-blind, like class codes
        self.assertEqual(
            (found["invitation_id"], found["room_id"], found["uses"]), (ident, ROOM, 0)
        )
        self.assertIsNone(await self.store.for_code("zzz9zzz"))
        await self.store.record_use(ident, "@ann:x", 200)
        await self.store.record_use(ident, "@forwarded:x", 300)
        rows = await self.store.for_room(ROOM)
        self.assertEqual(
            (rows[0]["first_used_by"], rows[0]["first_used_at_ms"], rows[0]["uses"]),
            ("@ann:x", 200, 2),
        )
        self.assertNotIn("email", self.store.public(rows[0]))
        self.assertEqual(rows[0]["email"], "ann@school.example")

    async def test_a_new_class_code_never_collides_with_an_invitation_code(self):
        await self.store.create(ROOM, "ann@school.example", "abc2def", "@t:x", 100)
        room_store = MagicMock()
        from unittest.mock import patch

        with patch(
            "synapse_pangea_chat.room_code.code_lookup.get_rooms_with_access_code",
            AsyncMock(return_value=[]),
        ):
            self.assertTrue(await code_is_taken("abc2def", room_store, self.claims))
            self.assertFalse(await code_is_taken("ghi3jkm", room_store, self.claims))


class TestRead(unittest.IsolatedAsyncioTestCase):
    def _api(self, *, server_admin: bool, level: int) -> MagicMock:
        api = MagicMock()
        api.is_user_admin = AsyncMock(return_value=server_admin)
        event = MagicMock()
        event.content = {"users": {"@t:x": level}}
        api.get_room_state = AsyncMock(
            return_value={("m.room.power_levels", ""): event}
        )
        requester = MagicMock()
        requester.user.to_string.return_value = "@t:x"
        api._hs.get_auth.return_value.get_user_by_req = AsyncMock(
            return_value=requester
        )
        return api

    async def _get(self, api, room: str):
        from unittest.mock import patch

        store = MagicMock()
        store.students.for_room = AsyncMock(
            return_value=[
                {
                    "invitation_id": "i1",
                    "room_id": room,
                    "email": "ann@school.example",
                    "created_by": "@t:x",
                    "created_at_ms": 1,
                    "first_used_by": None,
                    "first_used_at_ms": None,
                    "uses": 0,
                }
            ]
        )
        store.students.public = lambda r: {k: v for k, v in r.items() if k != "email"}
        request = MagicMock()
        request.args = {b"room_id": [room.encode()]} if room else {}
        respond = MagicMock()
        with patch(
            "synapse_pangea_chat.email_invite.student_invitation_api.respond_with_json",
            respond,
        ):
            await StudentInvitationAPI(api, store)._async_render_GET(request)
        return respond.call_args.args[1], respond.call_args.args[2]

    async def test_a_course_admin_or_server_admin_reads_invitations_without_addresses(
        self,
    ):
        status, body = await self._get(self._api(server_admin=False, level=100), ROOM)
        self.assertEqual(status, 200)
        self.assertEqual(body["invitations"][0]["invitation_id"], "i1")
        self.assertNotIn("email", body["invitations"][0])
        status, _ = await self._get(self._api(server_admin=True, level=0), ROOM)
        self.assertEqual(status, 200)
        status, _ = await self._get(self._api(server_admin=False, level=50), ROOM)
        self.assertEqual(status, 403)
        status, _ = await self._get(self._api(server_admin=True, level=100), "")
        self.assertEqual(status, 400)
