from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.room_code.get_rooms_with_access_code import RoomCodeMatch
from synapse_pangea_chat.room_code.knock_with_code import KnockWithCode

USER = "@student:my.domain.name"
ROOM_1 = "!room1:my.domain.name"
ROOM_2 = "!room2:my.domain.name"
CODE = "vldcde1"

MODULE = "synapse_pangea_chat.room_code.knock_with_code"


def _handler() -> KnockWithCode:
    api = MagicMock()
    requester = MagicMock()
    requester.user.to_string.return_value = USER
    api._hs.get_auth.return_value.get_user_by_req = AsyncMock(return_value=requester)
    return KnockWithCode(api=api, config=PangeaChatConfig())


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


if __name__ == "__main__":
    unittest.main()
