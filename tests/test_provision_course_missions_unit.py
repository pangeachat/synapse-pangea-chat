"""The v2 deferred path writes the course space's Can-Do list when the claim
creates the space.

Runs the real invitation store and provisioner over an in-memory database, with
Synapse's room creation and room state stubbed, as test_claim_by_email_unit
does. A read that fails creates the course without the list, captures why, and
schedules a later attempt when one can get past the failure.
"""

import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.email_invite.course_claims import CourseClaimStore
from synapse_pangea_chat.email_invite.course_missions import (
    CMS_UNAVAILABLE,
    QUEST_NOT_FOUND,
    TIMEOUT,
    BuiltCourseMissions,
    CourseMissionsError,
)
from synapse_pangea_chat.email_invite.provision_course import CourseProvisioner
from tests.moderation_doubles import DbPoolDouble
from tests.test_claim_by_email_unit import SPEC, TEACHER, _claimed_room_state

CAN_DO_LIST = {
    "version": 1,
    "revision": 1,
    "missions": [{"lo_id": "lo-a", "activity_ids": ["act-1"]}],
}


class TestProvisionCanDoList(unittest.IsolatedAsyncioTestCase):
    async def _claim(self, can_do_list: Any) -> tuple[Any, Any, str]:
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
        if isinstance(can_do_list, Exception):
            reader.read = AsyncMock(side_effect=can_do_list)
        elif isinstance(can_do_list, BuiltCourseMissions):
            reader.read = AsyncMock(return_value=can_do_list)
        else:
            reader.read = AsyncMock(return_value=BuiltCourseMissions(can_do_list))
        provisioner = CourseProvisioner(
            api, claims.invitations, claims, MagicMock(), course_missions=reader
        )
        provisioner.course_missions_rebuild = MagicMock()
        self.rebuild = provisioner.course_missions_rebuild
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

    async def test_claim_writes_the_can_do_list_sent_by_the_claimant(self) -> None:
        create_room, reader, room = await self._claim(CAN_DO_LIST)

        self.assertEqual(room, "!room:x")
        reader.read.assert_awaited_once_with("quest")
        state = self._state(create_room)
        event = state["pangea.course_missions"]
        self.assertEqual(event["state_key"], "")
        self.assertEqual(event["content"], CAN_DO_LIST)
        self.assertEqual(
            create_room.await_args.kwargs["requester"].user.to_string(), TEACHER
        )
        power = state["m.room.power_levels"]["content"]
        self.assertEqual(power["users"], {TEACHER: 100})
        self.assertEqual(power["events"]["pangea.course_missions"], 100)

    async def test_a_cms_failure_creates_the_course_captures_and_rebuilds_later(
        self,
    ) -> None:
        for code in (CMS_UNAVAILABLE, TIMEOUT):
            with self.subTest(code=code), patch(
                "synapse_pangea_chat.email_invite.course_missions.capture_message"
            ) as capture:
                create_room, reader, room = await self._claim(
                    CourseMissionsError(code, "detail")
                )

                self.assertEqual(room, "!room:x")
                self.assertNotIn("pangea.course_missions", self._state(create_room))
                self.rebuild.schedule.assert_called_once_with("!room:x", "quest")
                capture.assert_called_once()
                self.assertEqual(
                    capture.call_args.args[0], f"Can-Do list not built: {code}"
                )
                self.assertEqual(capture.call_args.kwargs["level"], "error")
                context = capture.call_args.kwargs["context"]
                self.assertEqual(context["quest_id"], "quest")
                self.assertIn("invitation_id", context)
                # Ids only: never the requesting address.
                self.assertNotIn("teacher@school.example", repr(capture.call_args))

    async def test_a_list_partly_ranked_from_the_page_is_written_and_rebuilt(
        self,
    ) -> None:
        built = BuiltCourseMissions(CAN_DO_LIST, [], ["lo-a"])
        create_room, _, _ = await self._claim(built)

        self.assertEqual(
            self._state(create_room)["pangea.course_missions"]["content"], CAN_DO_LIST
        )
        self.rebuild.schedule.assert_called_once_with(
            "!room:x", "quest", expected=CAN_DO_LIST
        )

    async def test_a_quest_that_is_gone_is_captured_and_not_retried(self) -> None:
        with patch(
            "synapse_pangea_chat.email_invite.course_missions.capture_message"
        ) as capture:
            create_room, _, room = await self._claim(
                CourseMissionsError(QUEST_NOT_FOUND)
            )

        self.assertEqual(room, "!room:x")
        self.assertNotIn("pangea.course_missions", self._state(create_room))
        self.rebuild.schedule.assert_not_called()
        capture.assert_called_once()

    async def test_a_failed_read_is_logged_without_sentry(self) -> None:
        with self.assertLogs(
            "synapse.module.synapse_pangea_chat.email_invite.course_missions",
            "ERROR",
        ) as logs:
            await self._claim(CourseMissionsError(QUEST_NOT_FOUND))

        self.assertIn("quest_not_found", logs.output[0])
        self.assertIn("invitation_id=", logs.output[0])
