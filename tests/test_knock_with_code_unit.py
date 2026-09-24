from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.email_invite.course_claims import CourseClaim
from synapse_pangea_chat.room_code.get_rooms_with_access_code import RoomCodeMatch
from synapse_pangea_chat.room_code.knock_with_code import KnockWithCode

USER = "@student:my.domain.name"
ROOM_1 = "!room1:my.domain.name"
ROOM_2 = "!room2:my.domain.name"
CODE = "vldcde1"

MODULE = "synapse_pangea_chat.room_code.knock_with_code"


def _claim_store(claim: CourseClaim | None = None, wins: bool = True) -> MagicMock:
    store = MagicMock()
    store.get = AsyncMock(return_value=claim)
    store.claim = AsyncMock(return_value=wins)
    store.mark_notice_sent = AsyncMock()
    return store


def _mailer() -> MagicMock:
    mailer = MagicMock()
    mailer.send_course_claimed = AsyncMock()
    return mailer


def _handler(
    claim_store: MagicMock | None = None, mailer: MagicMock | None = None
) -> KnockWithCode:
    api = MagicMock()
    requester = MagicMock()
    requester.user.to_string.return_value = USER
    api._hs.get_auth.return_value.get_user_by_req = AsyncMock(return_value=requester)
    api._hs.get_clock.return_value.time_msec.return_value = 1_000
    return KnockWithCode(
        api=api,
        config=PangeaChatConfig(),
        claim_store=claim_store or _claim_store(),
        mailer=mailer or _mailer(),
    )


class TestKnockWithCodeResponses(unittest.IsolatedAsyncioTestCase):
    """Response shaping of the handler with collaborators mocked — the e2e
    suite cannot construct an all-invites-failed room (a fully-left room
    stops matching the code query entirely), so the 500 path is pinned
    here (issue #197)."""

    def setUp(self) -> None:
        self.respond = MagicMock()
        patches = [
            patch(f"{MODULE}.respond_with_json", self.respond),
            patch(f"{MODULE}.is_rate_limited", return_value=False),
            patch(
                f"{MODULE}.extract_body_json",
                AsyncMock(return_value={"access_code": CODE}),
            ),
            patch(f"{MODULE}.get_user_room_membership", AsyncMock(return_value=None)),
            # Collaborator, not under test here; its own behavior is pinned in
            # test_blocked_join_gate_unit.
            patch(f"{MODULE}.is_blocked_by_room_admin", AsyncMock(return_value=False)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _response(self) -> tuple[int, dict]:
        self.respond.assert_called_once()
        args = self.respond.call_args.args
        return args[1], args[2]

    async def test_unmatched_code_answers_404_with_errcode(self) -> None:
        with patch(f"{MODULE}.get_rooms_with_access_code", AsyncMock(return_value=[])):
            await _handler()._async_render_POST(MagicMock())
        status, body = self._response()
        self.assertEqual(status, 404)
        self.assertEqual(body["errcode"], "ORG.PANGEA.CODE_NOT_FOUND")

    async def test_all_invites_failed_answers_500_with_failed_rooms(self) -> None:
        matches = [RoomCodeMatch(room_id=ROOM_1, is_admin_code=False)]
        with (
            patch(
                f"{MODULE}.get_rooms_with_access_code", AsyncMock(return_value=matches)
            ),
            patch(
                f"{MODULE}.invite_user_to_room",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            await _handler()._async_render_POST(MagicMock())
        status, body = self._response()
        self.assertEqual(status, 500)
        self.assertEqual(body["errcode"], "ORG.PANGEA.INVITE_FAILED")
        self.assertEqual(body["failed"], [ROOM_1])

    async def test_one_failed_room_does_not_block_the_other(self) -> None:
        matches = [
            RoomCodeMatch(room_id=ROOM_1, is_admin_code=False),
            RoomCodeMatch(room_id=ROOM_2, is_admin_code=False),
        ]

        async def invite(api, user_id, room_id):
            if room_id == ROOM_1:
                raise RuntimeError("boom")

        with (
            patch(
                f"{MODULE}.get_rooms_with_access_code", AsyncMock(return_value=matches)
            ),
            patch(f"{MODULE}.invite_user_to_room", AsyncMock(side_effect=invite)),
        ):
            await _handler()._async_render_POST(MagicMock())
        status, body = self._response()
        self.assertEqual(status, 200)
        self.assertEqual(body["rooms"], [ROOM_2])
        self.assertEqual(body["already_joined"], [])
        self.assertEqual(body["banned"], [])

    async def test_invite_pending_room_is_returned_without_a_second_invite(
        self,
    ) -> None:
        # A user already invited to the room holds what the endpoint issues;
        # the room comes back in `rooms` (the client's own /join succeeds for
        # an invited user) and no second invite is sent (issue #148).
        matches = [RoomCodeMatch(room_id=ROOM_1, is_admin_code=False)]
        invite = AsyncMock()
        with (
            patch(
                f"{MODULE}.get_rooms_with_access_code", AsyncMock(return_value=matches)
            ),
            patch(
                f"{MODULE}.get_user_room_membership", AsyncMock(return_value="invite")
            ),
            patch(f"{MODULE}.invite_user_to_room", invite),
        ):
            await _handler()._async_render_POST(MagicMock())
        status, body = self._response()
        self.assertEqual(status, 200)
        self.assertEqual(body["rooms"], [ROOM_1])
        self.assertEqual(body["already_joined"], [])
        invite.assert_not_called()

    async def test_every_room_blocked_answers_generic_403(self) -> None:
        # All matched rooms refuse via the blocked join gate: a bare
        # M_FORBIDDEN with no room list, so the refusal reveals nothing
        # (blocked-join-gate.instructions.md).
        matches = [RoomCodeMatch(room_id=ROOM_1, is_admin_code=False)]
        invite = AsyncMock()
        with (
            patch(
                f"{MODULE}.get_rooms_with_access_code", AsyncMock(return_value=matches)
            ),
            patch(f"{MODULE}.is_blocked_by_room_admin", AsyncMock(return_value=True)),
            patch(f"{MODULE}.invite_user_to_room", invite),
        ):
            await _handler()._async_render_POST(MagicMock())
        status, body = self._response()
        self.assertEqual(status, 403)
        self.assertEqual(body, {"errcode": "M_FORBIDDEN", "error": "Forbidden"})
        invite.assert_not_called()


class TestClaimingARequestedCourse(unittest.IsolatedAsyncioTestCase):
    """The admin code of a course made by create_course_space
    (knock-with-code.instructions.md, "Claiming a course"): one claimer, and
    the class link goes to the requesting address rather than the claimer."""

    REQUESTED = "teacher@school.example"

    def setUp(self) -> None:
        self.respond = MagicMock()
        self.promote = AsyncMock()
        self.burn = AsyncMock(return_value=True)
        self.invite = AsyncMock()
        self.capture = MagicMock()
        patches = [
            patch(f"{MODULE}.respond_with_json", self.respond),
            patch(f"{MODULE}.is_rate_limited", return_value=False),
            patch(
                f"{MODULE}.extract_body_json",
                AsyncMock(return_value={"access_code": CODE}),
            ),
            patch(f"{MODULE}.get_user_room_membership", AsyncMock(return_value=None)),
            patch(f"{MODULE}.is_blocked_by_room_admin", AsyncMock(return_value=False)),
            patch(
                f"{MODULE}.get_rooms_with_access_code",
                AsyncMock(
                    return_value=[RoomCodeMatch(room_id=ROOM_1, is_admin_code=True)]
                ),
            ),
            patch(f"{MODULE}.invite_user_to_room", self.invite),
            patch(f"{MODULE}.promote_user_to_admin", self.promote),
            patch(f"{MODULE}.burn_admin_code", self.burn),
            patch(f"{MODULE}._capture_exception", self.capture),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _response(self) -> tuple[int, dict]:
        self.respond.assert_called_once()
        args = self.respond.call_args.args
        return args[1], args[2]

    @staticmethod
    def _room_state(handler: KnockWithCode, class_code: str | None) -> None:
        join_rules = MagicMock(type="m.room.join_rules")
        join_rules.content = {"join_rule": "knock"}
        if class_code is not None:
            join_rules.content["access_code"] = class_code
        name = MagicMock(type="m.room.name")
        name.content = {"name": "Spanish 1"}
        api: Any = handler._api
        api.get_room_state = AsyncMock(
            return_value={
                ("m.room.join_rules", ""): join_rules,
                ("m.room.name", ""): name,
            }
        )
        api.is_mine.return_value = True
        profile = MagicMock(display_name="Ms. Rivera")
        api.get_profile_for_user = AsyncMock(return_value=profile)

    async def test_claim_promotes_and_sends_class_link_to_requesting_address(
        self,
    ) -> None:
        store = _claim_store(CourseClaim(self.REQUESTED, None, False))
        mailer = _mailer()
        handler = _handler(store, mailer)
        self._room_state(handler, "cls4abc")

        await handler._async_render_POST(MagicMock())

        status, body = self._response()
        self.assertEqual(status, 200)
        self.assertEqual(body["rooms"], [ROOM_1])
        store.claim.assert_awaited_once_with(ROOM_1, USER, 1_000)
        self.promote.assert_awaited_once()
        self.burn.assert_awaited_once()
        mailer.send_course_claimed.assert_awaited_once()
        sent = mailer.send_course_claimed.await_args.kwargs
        # To the address the course was requested for, never the claimer's.
        self.assertEqual(sent["email_address"], self.REQUESTED)
        self.assertEqual(sent["class_code"], "cls4abc")
        self.assertEqual(sent["class_url"], "https://app.pangea.chat/cls4abc")
        self.assertEqual(sent["claimed_by_user_id"], USER)
        self.assertEqual(sent["claimed_by_display_name"], "Ms. Rivera")
        self.assertEqual(sent["course_title"], "Spanish 1")
        store.mark_notice_sent.assert_awaited_once_with(ROOM_1, USER, 1_000)

    async def test_claim_held_by_someone_else_is_a_spent_code(self) -> None:
        # Two requests read the admin code before the burn landed; the one
        # that lost the claim is neither invited nor promoted, and is told the
        # code does not exist, which is what it will be a moment later.
        store = _claim_store(CourseClaim(self.REQUESTED, "@other:x", False), wins=False)
        mailer = _mailer()

        await _handler(store, mailer)._async_render_POST(MagicMock())

        status, body = self._response()
        self.assertEqual(status, 404)
        self.assertEqual(body["errcode"], "ORG.PANGEA.CODE_NOT_FOUND")
        self.invite.assert_not_called()
        self.promote.assert_not_called()
        self.burn.assert_not_called()
        mailer.send_course_claimed.assert_not_called()

    async def test_admin_code_of_a_client_created_course_sends_nothing(self) -> None:
        # No claim record: the course was not requested, so the admin code
        # behaves as it always has and there is nobody to notify.
        store = _claim_store(None)
        mailer = _mailer()

        await _handler(store, mailer)._async_render_POST(MagicMock())

        status, _ = self._response()
        self.assertEqual(status, 200)
        store.claim.assert_not_called()
        self.promote.assert_awaited_once()
        self.burn.assert_awaited_once()
        mailer.send_course_claimed.assert_not_called()

    async def test_notice_already_sent_is_not_sent_again(self) -> None:
        store = _claim_store(CourseClaim(None, USER, True))
        mailer = _mailer()

        await _handler(store, mailer)._async_render_POST(MagicMock())

        status, _ = self._response()
        self.assertEqual(status, 200)
        mailer.send_course_claimed.assert_not_called()

    async def test_failed_notice_is_captured_and_does_not_fail_the_claim(
        self,
    ) -> None:
        store = _claim_store(CourseClaim(self.REQUESTED, None, False))
        mailer = _mailer()
        mailer.send_course_claimed.side_effect = RuntimeError("smtp down")
        handler = _handler(store, mailer)
        self._room_state(handler, "cls4abc")

        await handler._async_render_POST(MagicMock())

        status, body = self._response()
        self.assertEqual(status, 200)
        self.assertEqual(body["rooms"], [ROOM_1])
        self.capture.assert_called_once()
        # Left unmarked, so the record still says the notice is owed.
        store.mark_notice_sent.assert_not_called()

    async def test_claimed_course_without_a_class_code_is_captured(self) -> None:
        store = _claim_store(CourseClaim(self.REQUESTED, None, False))
        mailer = _mailer()
        handler = _handler(store, mailer)
        self._room_state(handler, None)

        await handler._async_render_POST(MagicMock())

        status, _ = self._response()
        self.assertEqual(status, 200)
        mailer.send_course_claimed.assert_not_called()
        self.capture.assert_called_once()


if __name__ == "__main__":
    unittest.main()
