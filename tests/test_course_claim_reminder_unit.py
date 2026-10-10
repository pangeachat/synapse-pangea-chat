"""The claim reminder endpoint: who may send one, which courses refuse, and
that nothing it answers carries the code (create-course-space.instructions.md,
"Claim reminders")."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.email_invite.course_claim_reminder import (
    SendCourseClaimReminder,
)
from synapse_pangea_chat.email_invite.course_claims import ReminderTarget

MODULE = "synapse_pangea_chat.email_invite.course_claim_reminder"
ROOM = "!course:x"
BODY = {
    "room_id": ROOM,
    "subject": "Your Spanish course is waiting",
    "body": "Your course is ready.\n\nOpen it to become its teacher.",
    "cta_label": "Open your course",
}


def _resource(
    target: ReminderTarget | None, admin: bool = True
) -> tuple[SendCourseClaimReminder, MagicMock, MagicMock]:
    api: Any = MagicMock()
    requester = MagicMock()
    requester.user.to_string.return_value = "@bot:x"
    api._hs.get_auth.return_value.get_user_by_req = AsyncMock(return_value=requester)
    api._hs.get_clock.return_value.time_msec.return_value = 7
    api.is_user_admin = AsyncMock(return_value=admin)
    store = MagicMock()
    store.reminder_target = AsyncMock(return_value=target)
    store.add_code = AsyncMock()
    store.remove_code = AsyncMock()
    mailer = MagicMock()
    mailer.send_course_reminder = AsyncMock()
    return (
        SendCourseClaimReminder(api, PangeaChatConfig(), store, mailer),
        store,
        mailer,
    )


class TestSendCourseClaimReminder(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.respond = MagicMock()
        self.capture = MagicMock()
        patches = [
            patch(f"{MODULE}.respond_with_json", self.respond),
            patch(f"{MODULE}._capture_exception", self.capture),
            patch(f"{MODULE}.new_unique_code", AsyncMock(return_value="n3wcod1")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _response(self) -> tuple[int, dict]:
        self.respond.assert_called_once()
        args = self.respond.call_args.args
        return args[1], args[2]

    async def _post(self, resource: SendCourseClaimReminder, body: Any = BODY) -> None:
        with patch(f"{MODULE}.extract_body_json", AsyncMock(return_value=body)):
            await resource._async_render_POST(MagicMock())

    async def test_mints_a_code_and_sends_it_without_answering_it(self) -> None:
        resource, store, mailer = _resource(ReminderTarget("t@school.example", False))

        await self._post(resource)

        status, body = self._response()
        self.assertEqual((status, body), (200, {"sent": True}))
        store.add_code.assert_awaited_once_with(ROOM, "n3wcod1", 7)
        sent = mailer.send_course_reminder.await_args.kwargs
        self.assertEqual(sent["email_address"], "t@school.example")
        self.assertEqual(sent["claim_url"], "https://app.pangea.chat/n3wcod1")
        self.assertEqual(sent["claim_code"], "n3wcod1")
        self.assertEqual(sent["subject"], BODY["subject"])
        self.assertEqual(sent["cta_label"], BODY["cta_label"])
        self.assertNotIn("n3wcod1", str(body))

    async def test_only_a_server_admin_may_send(self) -> None:
        resource, store, mailer = _resource(
            ReminderTarget("t@school.example", False), admin=False
        )

        await self._post(resource)

        status, _ = self._response()
        self.assertEqual(status, 403)
        store.add_code.assert_not_called()
        mailer.send_course_reminder.assert_not_called()

    async def test_a_room_without_a_claim_record_is_refused(self) -> None:
        resource, store, _ = _resource(None)

        await self._post(resource)

        status, body = self._response()
        self.assertEqual(status, 404)
        self.assertEqual(body["errcode"], "ORG.PANGEA.NO_CLAIM_RECORD")
        store.add_code.assert_not_called()

    async def test_a_claimed_course_is_refused(self) -> None:
        resource, store, _ = _resource(ReminderTarget(None, True))

        await self._post(resource)

        status, body = self._response()
        self.assertEqual(status, 409)
        self.assertEqual(body["errcode"], "ORG.PANGEA.COURSE_CLAIMED")
        store.add_code.assert_not_called()

    async def test_a_course_without_an_address_is_refused(self) -> None:
        resource, store, _ = _resource(ReminderTarget(None, False))

        await self._post(resource)

        status, body = self._response()
        self.assertEqual(status, 422)
        self.assertEqual(body["errcode"], "ORG.PANGEA.NO_REQUESTING_ADDRESS")
        store.add_code.assert_not_called()

    async def test_missing_message_parts_are_refused(self) -> None:
        for missing in ("subject", "body", "cta_label"):
            with self.subTest(missing=missing):
                self.respond.reset_mock()
                resource, store, _ = _resource(
                    ReminderTarget("t@school.example", False)
                )
                body = {k: v for k, v in BODY.items() if k != missing}

                await self._post(resource, body)

                status, _ = self._response()
                self.assertEqual(status, 400)
                store.add_code.assert_not_called()

    async def test_a_failed_send_withdraws_the_code(self) -> None:
        resource, store, mailer = _resource(ReminderTarget("t@school.example", False))
        mailer.send_course_reminder.side_effect = RuntimeError("smtp down")

        await self._post(resource)

        status, body = self._response()
        self.assertEqual(
            (status, body), (502, {"sent": False, "reason": "send_failed"})
        )
        store.remove_code.assert_awaited_once_with("n3wcod1")
        self.capture.assert_called_once()


class TestReminderTemplate(unittest.TestCase):
    def test_paragraphs_are_escaped_and_the_link_is_the_button(self) -> None:
        import jinja2

        from synapse_pangea_chat.email_invite.course_claim_emails import (
            TEMPLATES_DIR,
            reminder_paragraphs,
        )

        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
            autoescape=jinja2.select_autoescape(["html"]),
        )
        values = {
            "app_name": "Pangea Chat",
            "subject": "Reminder",
            "paragraphs": reminder_paragraphs("First <b>line</b>\nwraps.\n \nSecond."),
            "cta_label": "Open your course",
            "claim_url": "https://app.pangea.chat/n3wcod1",
            "claim_code": "n3wcod1",
        }
        html = env.get_template("course_reminder.html").render(**values)
        text = env.get_template("course_reminder.txt").render(**values)

        self.assertEqual(values["paragraphs"], ["First <b>line</b> wraps.", "Second."])
        self.assertNotIn("<b>line</b>", html)
        self.assertIn('href="https://app.pangea.chat/n3wcod1"', html)
        self.assertIn("Open your course", html)
        self.assertIn("https://app.pangea.chat/n3wcod1", text)
        self.assertIn("Second.", text)


if __name__ == "__main__":
    unittest.main()


class TestCallerRenderedReminder(unittest.TestCase):
    def test_validation_wants_the_claim_slot_in_both_parts_and_no_refusal_or_second_link(
        self,
    ) -> None:
        from synapse_pangea_chat.email_invite.course_claim_emails import (
            validate_rendered,
        )

        self.assertIsNone(
            validate_rendered('<a href="{{cta_url}}">go</a>', "go {{cta_url}}")
        )
        self.assertIn(
            "html must contain", str(validate_rendered("<p>x</p>", "go {{cta_url}}")
        )
        self.assertIn(
            "text must contain", str(validate_rendered('<a href="{{cta_url}}">go</a>', "go")
        )
        self.assertIn(
            "unsubscribe",
            str(validate_rendered(
                '<a href="{{cta_url}}">go</a><a href="{{unsubscribe_url}}">u</a>',
                "go {{cta_url}}",
            ),
        )
        self.assertIn(
            "cta2_url",
            str(validate_rendered(
                '<a href="{{cta_url}}">go</a>', "go {{cta_url}} {{cta2_url}}"
            ),
        )

    def test_slots_are_filled_literally_and_escaped_in_html(self) -> None:
        from synapse_pangea_chat.email_invite.course_claim_emails import (
            fill_claim_slots,
        )

        html, text = fill_claim_slots(
            '<a href="{{cta_url}}">go</a><p>{{receiving_reason}}</p><p>{{postal_address}}</p>',
            "go {{cta_url}}\n{{receiving_reason}}\n{{postal_address}}",
            claim_url="https://app.x/abc?x=1&y=2",
            receiving_reason="You asked <us>",
            postal_address="1 Main St",
        )
        self.assertIn('href="https://app.x/abc?x=1&amp;y=2"', html)
        self.assertIn("You asked &lt;us&gt;", html)
        self.assertIn("go https://app.x/abc?x=1&y=2", text)
        self.assertIn("1 Main St", text)
        self.assertNotIn("{{", html + text)


class TestV2RemindCallerRendered(unittest.IsolatedAsyncioTestCase):
    def _resource(self):
        from synapse_pangea_chat.email_invite.course_invitation_api import (
            CourseInvitationAPI,
        )

        api: Any = MagicMock()
        invitations = MagicMock()
        invitations.get = AsyncMock(
            return_value={
                "invitation_id": "inv-1",
                "status": "prepared",
                "requested_email": "t@x.org",
                "specification": {},
            }
        )
        invitations.status = AsyncMock(
            return_value={"invitation_id": "inv-1", "status": "prepared"}
        )
        resource = CourseInvitationAPI(
            api, PangeaChatConfig(), MagicMock(), invitations, MagicMock(), "reminder"
        )
        resource.code = AsyncMock(return_value="c0de123")
        resource.send = AsyncMock()
        return resource

    async def test_html_and_text_ride_to_the_mailer_with_the_claim_slot(self) -> None:
        r = self._resource()
        await r.remind(
            {
                "invitation_id": "inv-1",
                "subject": "s",
                "body": "b",
                "cta_label": "Accept",
                "html": '<a href="{{cta_url}}">Accept</a>',
                "text": "Accept: {{cta_url}}",
                "receiving_reason": "You were invited.",
            }
        )
        rendered = r.send.await_args.kwargs["rendered"]
        self.assertEqual(
            (rendered["html"], rendered["text"], rendered["receiving_reason"]),
            (
                '<a href="{{cta_url}}">Accept</a>',
                "Accept: {{cta_url}}",
                "You were invited.",
            ),
        )

    async def test_a_rendered_email_without_the_slot_or_with_a_refusal_link_is_refused(
        self,
    ) -> None:
        from synapse.api.errors import SynapseError

        r = self._resource()
        for html, text in (
            ("<p>no slot</p>", "Accept: {{cta_url}}"),
            ('<a href="{{cta_url}}">a</a>', "no slot"),
            (
                '<a href="{{cta_url}}">a</a><a href="{{unsubscribe_url}}">u</a>',
                "Accept: {{cta_url}}",
            ),
        ):
            with self.subTest(html=html), self.assertRaises(SynapseError) as caught:
                await r.remind(
                    {
                        "invitation_id": "inv-1",
                        "subject": "s",
                        "body": "b",
                        "cta_label": "Accept",
                        "html": html,
                        "text": text,
                    }
                )
            self.assertEqual(caught.exception.code, 400)
        r.send.assert_not_awaited()
        await r.remind(
            {
                "invitation_id": "inv-1",
                "subject": "s",
                "body": "b",
                "cta_label": "Accept",
            }
        )
        self.assertNotIn("html", r.send.await_args.kwargs["rendered"])
