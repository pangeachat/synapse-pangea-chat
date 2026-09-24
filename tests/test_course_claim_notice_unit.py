"""The claim notice: sent under a lease, once, and kept owed when a send fails
(knock-with-code.instructions.md, "Claiming a course")."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.email_invite.course_claim_notice import (
    NOTICE_LEASE_MS,
    ClaimNoticeAbandoned,
    CourseClaimNotifier,
)
from synapse_pangea_chat.email_invite.course_claims import (
    MAX_NOTICE_ATTEMPTS,
    NoticeReservation,
)

ROOM = "!course:my.domain.name"
CLAIMER = "@rivera:my.domain.name"
REQUESTED = "teacher@school.example"
MODULE = "synapse_pangea_chat.email_invite.course_claim_notice"


def _notifier(
    reservation: NoticeReservation | None, class_code: str | None = "cls4abc"
) -> tuple[CourseClaimNotifier, MagicMock, MagicMock]:
    api: Any = MagicMock()
    api._hs.get_clock.return_value.time_msec.return_value = 1_000
    join_rules = MagicMock(type="m.room.join_rules")
    join_rules.content = {"join_rule": "knock"}
    if class_code is not None:
        join_rules.content["access_code"] = class_code
    name = MagicMock(type="m.room.name")
    name.content = {"name": "Spanish 1"}
    api.get_room_state = AsyncMock(return_value={"j": join_rules, "n": name})
    store = MagicMock()
    store.reserve_notice = AsyncMock(return_value=reservation)
    store.mark_notice_sent = AsyncMock()
    store.outstanding_notices = AsyncMock(return_value=[])
    mailer = MagicMock()
    mailer.send_course_claimed = AsyncMock()
    return CourseClaimNotifier(api, PangeaChatConfig(), store, mailer), store, mailer


class TestNotify(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.capture = MagicMock()
        p = patch(f"{MODULE}._capture_exception", self.capture)
        p.start()
        self.addCleanup(p.stop)

    async def test_sends_to_the_reserved_address_then_marks_it_sent(self) -> None:
        notifier, store, mailer = _notifier(NoticeReservation(REQUESTED, 1))

        await notifier.notify(ROOM, CLAIMER)

        store.reserve_notice.assert_awaited_once_with(
            ROOM, CLAIMER, 1_000, NOTICE_LEASE_MS
        )
        sent = mailer.send_course_claimed.await_args.kwargs
        # To the address the course was requested for, never the claimer's.
        self.assertEqual(sent["email_address"], REQUESTED)
        self.assertEqual(sent["class_code"], "cls4abc")
        self.assertEqual(sent["class_url"], "https://app.pangea.chat/cls4abc")
        self.assertEqual(sent["course_title"], "Spanish 1")
        store.mark_notice_sent.assert_awaited_once_with(ROOM, CLAIMER, 1_000)
        self.capture.assert_not_called()

    async def test_no_reservation_sends_nothing(self) -> None:
        # Not owed, already sent, or another request or worker holds the lease.
        notifier, store, mailer = _notifier(None)

        await notifier.notify(ROOM, CLAIMER)

        mailer.send_course_claimed.assert_not_called()
        store.mark_notice_sent.assert_not_called()

    async def test_failed_send_is_captured_and_left_owed(self) -> None:
        notifier, store, mailer = _notifier(NoticeReservation(REQUESTED, 1))
        mailer.send_course_claimed.side_effect = RuntimeError("smtp down")

        await notifier.notify(ROOM, CLAIMER)

        self.capture.assert_called_once()
        store.mark_notice_sent.assert_not_called()

    async def test_last_attempt_failing_is_reported_as_abandoned(self) -> None:
        notifier, _, mailer = _notifier(
            NoticeReservation(REQUESTED, MAX_NOTICE_ATTEMPTS)
        )
        mailer.send_course_claimed.side_effect = RuntimeError("smtp down")

        await notifier.notify(ROOM, CLAIMER)

        captured = [call.args[0] for call in self.capture.call_args_list]
        self.assertTrue(any(isinstance(e, ClaimNoticeAbandoned) for e in captured))

    async def test_missing_class_code_is_captured(self) -> None:
        notifier, store, mailer = _notifier(
            NoticeReservation(REQUESTED, 1), class_code=None
        )

        await notifier.notify(ROOM, CLAIMER)

        mailer.send_course_claimed.assert_not_called()
        store.mark_notice_sent.assert_not_called()
        self.capture.assert_called_once()

    async def test_reservation_failure_never_raises(self) -> None:
        notifier, store, _ = _notifier(None)
        store.reserve_notice.side_effect = RuntimeError("db down")

        await notifier.notify(ROOM, CLAIMER)

        self.capture.assert_called_once()

    async def test_retry_notifies_every_owed_claim(self) -> None:
        notifier, store, _ = _notifier(None)
        store.outstanding_notices.return_value = [(ROOM, CLAIMER), ("!b:x", "@b:x")]

        with patch.object(notifier, "notify", AsyncMock()) as notify:
            await notifier.retry_outstanding()

        self.assertEqual(
            [call.args for call in notify.await_args_list],
            [(ROOM, CLAIMER), ("!b:x", "@b:x")],
        )


if __name__ == "__main__":
    unittest.main()
