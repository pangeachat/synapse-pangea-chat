from __future__ import annotations

import unittest
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
LOOKUP = "synapse_pangea_chat.room_code.code_lookup"


def _claim_store(
    claim: CourseClaim | None = None,
    wins: bool = True,
    claim_rooms: list[str] | None = None,
) -> MagicMock:
    store = MagicMock()
    # The rooms whose claim code was submitted: by default the claim's own
    # room when there is a claim, none otherwise.
    store.rooms_for_admin_code = AsyncMock(
        return_value=claim_rooms
        if claim_rooms is not None
        else ([ROOM_1] if claim is not None else [])
    )
    store.get = AsyncMock(return_value=claim)
    store.claim = AsyncMock(return_value=wins)
    store.mark_promoted = AsyncMock()
    return store


def _notifier() -> MagicMock:
    notifier = MagicMock()
    notifier.notify = AsyncMock()
    return notifier


def _handler(
    claim_store: MagicMock | None = None, notifier: MagicMock | None = None
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
        notifier=notifier or _notifier(),
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
        with patch(f"{LOOKUP}.get_rooms_with_access_code", AsyncMock(return_value=[])):
            await _handler()._async_render_POST(MagicMock())
        status, body = self._response()
        self.assertEqual(status, 404)
        self.assertEqual(body["errcode"], "ORG.PANGEA.CODE_NOT_FOUND")

    async def test_all_invites_failed_answers_500_with_failed_rooms(self) -> None:
        matches = [RoomCodeMatch(room_id=ROOM_1, is_admin_code=False)]
        with (
            patch(
                f"{LOOKUP}.get_rooms_with_access_code", AsyncMock(return_value=matches)
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
                f"{LOOKUP}.get_rooms_with_access_code", AsyncMock(return_value=matches)
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
                f"{LOOKUP}.get_rooms_with_access_code", AsyncMock(return_value=matches)
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
                f"{LOOKUP}.get_rooms_with_access_code", AsyncMock(return_value=matches)
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
    (knock-with-code.instructions.md, "Claiming a course"): one claimer, and the
    class link is sent once the claimer is promoted."""

    def setUp(self) -> None:
        self.respond = MagicMock()
        self.promote = AsyncMock()
        self.burn = AsyncMock(return_value=True)
        self.invite = AsyncMock()
        patches = [
            patch(f"{MODULE}.respond_with_json", self.respond),
            patch(f"{MODULE}.is_rate_limited", return_value=False),
            patch(
                f"{MODULE}.extract_body_json",
                AsyncMock(return_value={"access_code": CODE}),
            ),
            patch(f"{MODULE}.get_user_room_membership", AsyncMock(return_value=None)),
            patch(f"{MODULE}.is_blocked_by_room_admin", AsyncMock(return_value=False)),
            # A claim code is not in join rules; it is found through the
            # claim record (the store's rooms_for_admin_code).
            patch(f"{LOOKUP}.get_rooms_with_access_code", AsyncMock(return_value=[])),
            patch(f"{MODULE}.invite_user_to_room", self.invite),
            patch(f"{MODULE}.promote_user_to_admin", self.promote),
            patch(f"{MODULE}.burn_admin_code", self.burn),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _response(self) -> tuple[int, dict]:
        self.respond.assert_called_once()
        args = self.respond.call_args.args
        return args[1], args[2]

    @staticmethod
    def _claim(claimed_by: str | None = None) -> CourseClaim:
        return CourseClaim(claimed_by=claimed_by)

    async def test_claim_promotes_spends_the_code_and_sends_the_notice(
        self,
    ) -> None:
        store = _claim_store(self._claim())
        notifier = _notifier()

        await _handler(store, notifier)._async_render_POST(MagicMock())

        status, body = self._response()
        self.assertEqual(status, 200)
        self.assertEqual(body["rooms"], [ROOM_1])
        store.rooms_for_admin_code.assert_awaited_once_with(CODE)
        store.claim.assert_awaited_once_with(ROOM_1, USER, 1_000)
        self.promote.assert_awaited_once()
        # Spent in the claim record; nothing in join rules to burn.
        store.mark_promoted.assert_awaited_once_with(ROOM_1, USER, 1_000)
        self.burn.assert_not_called()
        notifier.notify.assert_awaited_once_with(ROOM_1, USER)

    async def test_claim_held_by_someone_else_is_a_spent_code(self) -> None:
        # Two requests hold the claim code at once; the one that lost the
        # claim is neither invited nor promoted, and is told the code does not
        # exist, which is what it will be a moment later.
        store = _claim_store(self._claim(claimed_by="@other:x"), wins=False)
        notifier = _notifier()

        await _handler(store, notifier)._async_render_POST(MagicMock())

        status, body = self._response()
        self.assertEqual(status, 404)
        self.assertEqual(body["errcode"], "ORG.PANGEA.CODE_NOT_FOUND")
        self.invite.assert_not_called()
        self.promote.assert_not_called()
        notifier.notify.assert_not_called()

    async def test_an_admin_code_in_join_rules_is_an_ordinary_grant(self) -> None:
        # A co-teacher code set on a claimed course, or any client-created
        # course's admin code: found in join rules, not in the claim record.
        # Promote and burn, no claim, no notice.
        store = _claim_store(None)
        notifier = _notifier()

        with patch(
            f"{LOOKUP}.get_rooms_with_access_code",
            AsyncMock(return_value=[RoomCodeMatch(room_id=ROOM_1, is_admin_code=True)]),
        ):
            await _handler(store, notifier)._async_render_POST(MagicMock())

        status, body = self._response()
        self.assertEqual(status, 200)
        self.assertEqual(body["rooms"], [ROOM_1])
        store.get.assert_not_called()
        store.claim.assert_not_called()
        self.promote.assert_awaited_once()
        self.burn.assert_awaited_once()
        notifier.notify.assert_not_called()

    async def test_a_joined_member_holding_the_claim_code_claims(self) -> None:
        # The teacher who tried their class link before opening the claim
        # link, or a claimer retrying a failed promotion. The code is in no
        # state a member can read, so holding it is still the inbox proof.
        store = _claim_store(self._claim())
        notifier = _notifier()

        with patch(
            f"{MODULE}.get_user_room_membership", AsyncMock(return_value="join")
        ):
            await _handler(store, notifier)._async_render_POST(MagicMock())

        status, body = self._response()
        self.assertEqual(status, 200)
        self.assertEqual(body["already_joined"], [ROOM_1])
        self.assertEqual(body["rooms"], [])
        self.invite.assert_not_called()
        self.promote.assert_awaited_once()
        notifier.notify.assert_awaited_once_with(ROOM_1, USER)

    async def test_failed_promotion_is_not_announced_and_keeps_the_code(
        self,
    ) -> None:
        # promote_user_to_admin reports failure by returning False.
        store = _claim_store(self._claim())
        notifier = _notifier()
        self.promote.return_value = False

        await _handler(store, notifier)._async_render_POST(MagicMock())

        status, body = self._response()
        self.assertEqual(status, 500)
        self.assertEqual(body["failed"], [ROOM_1])
        store.mark_promoted.assert_not_called()
        notifier.notify.assert_not_called()

    async def test_failed_spend_is_not_announced(self) -> None:
        store = _claim_store(self._claim())
        store.mark_promoted.side_effect = RuntimeError("db down")
        notifier = _notifier()

        await _handler(store, notifier)._async_render_POST(MagicMock())

        status, _ = self._response()
        self.assertEqual(status, 500)
        notifier.notify.assert_not_called()

    async def test_failed_invite_leaves_the_claim_unannounced(self) -> None:
        # The claim is taken before the invite; if the invite fails, the
        # claimer is not promoted and the code is not spent.
        store = _claim_store(self._claim())
        notifier = _notifier()
        self.invite.side_effect = RuntimeError("boom")

        await _handler(store, notifier)._async_render_POST(MagicMock())

        status, _ = self._response()
        self.assertEqual(status, 500)
        store.mark_promoted.assert_not_called()
        notifier.notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
