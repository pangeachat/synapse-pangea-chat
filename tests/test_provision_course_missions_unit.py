"""The v2 deferred path writes the course copy when the claim creates the space.

Runs the real invitation store and provisioner over an in-memory database, with
Synapse's room creation and room state stubbed, as test_claim_by_email_unit
does. The copy is built by the same reader v1 create_course_space uses; a
failed read (None) creates the course without it.
"""

import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.email_invite.course_claims import CourseClaimStore
from synapse_pangea_chat.email_invite.provision_course import CourseProvisioner
from tests.moderation_doubles import DbPoolDouble
from tests.test_claim_by_email_unit import SPEC, TEACHER, _claimed_room_state

COPY = {
    "version": 1,
    "revision": 1,
    "missions": [{"lo_id": "lo-a", "activity_ids": ["act-1"]}],
}


class TestProvisionCourseCopy(unittest.IsolatedAsyncioTestCase):
    async def _claim(self, copy: Any) -> tuple[Any, Any, str]:
        api = MagicMock()
        api.server_name = "x"
        api._hs.get_datastores.return_value.main.db_pool = DbPoolDouble()
        api._hs.get_clock.return_value.time_msec.return_value = 10
        create_room = AsyncMock(return_value=("!room:x", None, None))
        api._hs.get_room_creation_handler.return_value.create_room = create_room
        api.get_room_state = AsyncMock(return_value=_claimed_room_state(TEACHER))
        claims = CourseClaimStore(api._hs)
        await claims._ensure_table()
        reader = MagicMock()
        reader.read = AsyncMock(return_value=copy)
        provisioner = CourseProvisioner(
            api, claims.invitations, claims, MagicMock(), course_missions=reader
        )
        ident, _ = await claims.invitations.prepare(
            "@operator:x", "request-1", SPEC, "teacher@school.example", "abc2def", 1
        )
        invitation = await claims.invitations.get(ident)
        with (
            patch(
                "synapse_pangea_chat.email_invite.provision_course.new_unique_code",
                AsyncMock(return_value="cl4sscd"),
            ),
            patch("synapse.logging.context.run_in_background"),
        ):
            room = await provisioner.claim(invitation, TEACHER)
        return create_room, reader, room

    @staticmethod
    def _state(create_room: Any) -> dict[str, Any]:
        config = create_room.await_args.kwargs["config"]
        return {event["type"]: event for event in config["initial_state"]}

    async def test_claim_writes_the_copy_sent_by_the_claimant(self) -> None:
        create_room, reader, room = await self._claim(COPY)

        self.assertEqual(room, "!room:x")
        reader.read.assert_awaited_once_with("quest")
        state = self._state(create_room)
        event = state["pangea.course_missions"]
        self.assertEqual(event["state_key"], "")
        self.assertEqual(event["content"], COPY)
        self.assertEqual(
            create_room.await_args.kwargs["requester"].user.to_string(), TEACHER
        )
        power = state["m.room.power_levels"]["content"]
        self.assertEqual(power["users"], {TEACHER: 100})
        self.assertEqual(power["events"]["pangea.course_missions"], 100)

    async def test_failed_read_still_creates_the_course(self) -> None:
        create_room, reader, room = await self._claim(None)

        self.assertEqual(room, "!room:x")
        reader.read.assert_awaited_once_with("quest")
        self.assertNotIn("pangea.course_missions", self._state(create_room))
