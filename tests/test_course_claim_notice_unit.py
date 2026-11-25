"""The claim notice: sent under a lease, once, and kept owed when a send fails
(knock-with-code.instructions.md, "Claiming a course")."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.email_invite.course_claim_emails import SEND_TIMEOUT_SECONDS
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
SECRET = b"claim-notice-test-secret"


def _token(url: str) -> dict:
    """The payload of a signed click link, verified."""
    from urllib.parse import parse_qs, urlsplit

    from synapse_pangea_chat.notice_delivery.tokens import verify_token

    parts = urlsplit(url)
    assert parts.path == "/_synapse/client/pangea/v1/n", url
    payload = verify_token(SECRET, parse_qs(parts.query)["t"][0], now_ms=1_000)
    assert payload is not None
    return payload


def _notifier(
    reservation: NoticeReservation | None, class_code: str | None = "cls4abc"
) -> tuple[CourseClaimNotifier, MagicMock, MagicMock]:
    api: Any = MagicMock()
    api._hs.get_clock.return_value.time_msec.return_value = 1_000
    api._hs.config.key.macaroon_secret_key = SECRET
    api._hs.config.server.public_baseurl = "https://hs.example/"
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
    store.invitations.claimed_email_for_room = AsyncMock(return_value=None)
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
        self.assertIsNone(sent["rendered"])  # nothing stored: the module's template

    async def test_the_module_template_links_the_course_on_the_dashboard(self) -> None:
        notifier, _, mailer = _notifier(NoticeReservation(REQUESTED, 1))

        await notifier.notify(ROOM, CLAIMER)

        sent = mailer.send_course_claimed.await_args.kwargs
        cta, cta2 = _token(sent["cta_url"]), _token(sent["cta2_url"])
        self.assertEqual(cta["d"], {"k": "dashboard", "c": ROOM})
        self.assertEqual((cta["u"], cta["l"], cta["o"]), (CLAIMER, "cta", "claim_mail"))
        self.assertEqual(cta2["d"]["k"], "external")
        self.assertTrue(cta2["d"]["u"].startswith("https://calendar.app.google/"))

    async def test_stored_destinations_are_signed_with_the_claimed_course(
        self,
    ) -> None:
        notifier, store, mailer = _notifier(NoticeReservation(REQUESTED, 1))
        store.invitations.claimed_email_for_room = AsyncMock(
            return_value={
                "subject": "s",
                "html": "{{cta_url}}",
                "text": "{{cta_url}}",
                "receiving_reason": "",
                "destination": {"kind": "dashboard"},
                "secondary_destination": None,
                "unsubscribe_url": None,
            }
        )

        await notifier.notify(ROOM, CLAIMER)

        sent = mailer.send_course_claimed.await_args.kwargs
        self.assertEqual(_token(sent["cta_url"])["d"], {"k": "dashboard", "c": ROOM})
        self.assertEqual(sent["cta2_url"], "")

    async def test_a_link_that_cannot_be_signed_leaves_the_notice_owed(self) -> None:
        notifier, store, mailer = _notifier(NoticeReservation(REQUESTED, 1))
        api: Any = notifier._api
        api._hs.config.server.public_baseurl = None

        await notifier.notify(ROOM, CLAIMER)

        mailer.send_course_claimed.assert_not_awaited()
        store.mark_notice_sent.assert_not_awaited()
        self.capture.assert_called()

    async def test_the_callers_stored_email_is_what_goes_out(self) -> None:
        notifier, store, mailer = _notifier(NoticeReservation(REQUESTED, 1))
        stored = {
            "subject": "Invite your students",
            "html": '<a href="{{class_url}}">x</a>',
            "text": "{{class_url}}",
            "receiving_reason": "r",
        }
        store.invitations.claimed_email_for_room = AsyncMock(return_value=stored)

        await notifier.notify(ROOM, CLAIMER)

        store.invitations.claimed_email_for_room.assert_awaited_once_with(ROOM)
        self.assertEqual(
            mailer.send_course_claimed.await_args.kwargs["rendered"], stored
        )

    async def test_a_failed_lookup_leaves_the_notice_owed_and_never_falls_back(
        self,
    ) -> None:
        notifier, store, mailer = _notifier(NoticeReservation(REQUESTED, 1))
        store.invitations.claimed_email_for_room = AsyncMock(
            side_effect=RuntimeError("db")
        )

        await notifier.notify(ROOM, CLAIMER)

        mailer.send_course_claimed.assert_not_called()
        store.mark_notice_sent.assert_not_called()
        self.capture.assert_called_once()

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

    def test_the_lease_outlasts_the_mailers_bound(self) -> None:
        self.assertLess(SEND_TIMEOUT_SECONDS * 1000 * 2, NOTICE_LEASE_MS)

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
