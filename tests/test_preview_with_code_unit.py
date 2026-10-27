from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.preview_with_code.preview_with_code import PreviewWithCode
from synapse_pangea_chat.room_code.code_index import RoomCodeMatch

USER = "@student:my.domain.name"
ROOM_1 = "!room1:my.domain.name"
ROOM_2 = "!room2:my.domain.name"
CODE = "vldcde1"

MODULE = "synapse_pangea_chat.preview_with_code.preview_with_code"


def _claim_store() -> MagicMock:
    store = MagicMock()
    store.get = AsyncMock(return_value=None)
    store.invitations.for_code = AsyncMock(return_value=None)
    # No student invitation holds the code unless a test says so.
    store.students.for_code = AsyncMock(return_value=None)
    return store


def _handler(claim_store: MagicMock, provisioner: MagicMock | None = None):
    api = MagicMock()
    requester = MagicMock()
    requester.user.to_string.return_value = USER
    api._hs.get_auth.return_value.get_user_by_req = AsyncMock(return_value=requester)
    return PreviewWithCode(
        api=api,
        config=PangeaChatConfig(),
        claim_store=claim_store,
        provisioner=provisioner,
    )


class TestPreviewWithCodeOpensAsAdmin(unittest.IsolatedAsyncioTestCase):
    """Each previewed room says whether the code makes the requester a teacher,
    so the app asks students, not teachers, to share their analytics before
    the join (course-analytics-access.instructions.md)."""

    def setUp(self) -> None:
        self.respond = MagicMock()
        self.rooms_for_code = AsyncMock(return_value=([], set()))
        for p in [
            patch(f"{MODULE}.respond_with_json", self.respond),
            patch(f"{MODULE}.is_rate_limited", return_value=False),
            patch(
                f"{MODULE}.extract_body_json",
                AsyncMock(return_value={"access_code": CODE}),
            ),
            patch(
                f"{MODULE}.get_room_preview_for_code",
                AsyncMock(side_effect=lambda room_id, **_: {"room_id": room_id}),
            ),
            patch(f"{MODULE}.rooms_for_code", self.rooms_for_code),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def _rooms(self) -> list[dict]:
        status, body = self.respond.call_args.args[1], self.respond.call_args.args[2]
        self.assertEqual(status, 200, body)
        return body["rooms"]

    async def test_a_class_code_opens_the_course_as_a_student(self) -> None:
        self.rooms_for_code.return_value = (
            [RoomCodeMatch(room_id=ROOM_1, is_admin_code=False)],
            set(),
        )
        await _handler(_claim_store())._async_render_POST(MagicMock())
        self.assertEqual(self._rooms(), [{"room_id": ROOM_1, "opens_as_admin": False}])

    async def test_an_admin_code_or_a_claim_opens_the_course_as_a_teacher(
        self,
    ) -> None:
        self.rooms_for_code.return_value = (
            [
                RoomCodeMatch(room_id=ROOM_1, is_admin_code=True),
                # A claim code that is also the room's join-rules code.
                RoomCodeMatch(room_id=ROOM_2, is_admin_code=False),
            ],
            {ROOM_2},
        )
        await _handler(_claim_store())._async_render_POST(MagicMock())
        self.assertEqual(
            [r["opens_as_admin"] for r in self._rooms()],
            [True, True],
        )

    async def test_a_student_invitation_code_previews_its_course(self) -> None:
        store = _claim_store()
        store.students.for_code = AsyncMock(
            return_value={"invitation_id": "inv-1", "room_id": ROOM_1}
        )
        await _handler(store)._async_render_POST(MagicMock())
        self.assertEqual(self._rooms(), [{"room_id": ROOM_1, "opens_as_admin": False}])
        self.rooms_for_code.assert_not_awaited()

    async def test_a_teacher_invitation_code_opens_its_course_as_a_teacher(
        self,
    ) -> None:
        store = _claim_store()
        store.invitations.for_code = AsyncMock(
            return_value={"status": "completed", "room_id": ROOM_1}
        )
        provisioner = MagicMock()
        provisioner.authorized = AsyncMock(return_value=True)
        await _handler(store, provisioner)._async_render_POST(MagicMock())
        self.assertEqual(self._rooms(), [{"room_id": ROOM_1, "opens_as_admin": True}])


if __name__ == "__main__":
    unittest.main()
