"""The v2 claim reminder and the claim emails it sends: the module template,
a caller-rendered email with the claim link as a slot, the v2 preparation without
an email, and the share kit stored for the claim (create-course-space.instructions.md).
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from synapse_pangea_chat.config import PangeaChatConfig


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
            "html must contain",
            str(validate_rendered("<p>x</p>", "go {{cta_url}}")),
        )
        self.assertIn(
            "text must contain",
            str(validate_rendered('<a href="{{cta_url}}">go</a>', "go")),
        )
        self.assertIn(
            "unsubscribe",
            str(
                validate_rendered(
                    '<a href="{{cta_url}}">go</a><a href="{{unsubscribe_url}}">u</a>',
                    "go {{cta_url}}",
                )
            ),
        )
        self.assertIn(
            "cta2_url",
            str(
                validate_rendered(
                    '<a href="{{cta_url}}">go</a>', "go {{cta_url}} {{cta2_url}}"
                )
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
        invitations.delivery_address = AsyncMock(return_value="t@x.org")
        invitations.set_delivery_address = AsyncMock(return_value=True)
        invitations.store_claimed_email = AsyncMock()
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

    def _body(self, **extra: Any) -> dict:
        return {
            "invitation_id": "inv-1",
            "subject": "s",
            "body": "b",
            "cta_label": "Accept",
            "html": '<a href="{{cta_url}}">Accept</a>',
            "text": "Accept: {{cta_url}}",
            **extra,
        }

    async def test_send_to_becomes_the_delivery_address(self) -> None:
        """A corrected address (#936): this reminder goes there, and so does every
        later send; the requesting address is left alone."""
        r = self._resource()
        await r.remind(self._body(send_to="New@X.org"))
        r.invitations.set_delivery_address.assert_awaited_once_with(
            "inv-1", "new@x.org"
        )
        self.assertEqual(r.send.await_args.args[2], "new@x.org")
        r.send.reset_mock()
        await r.remind(self._body())
        self.assertEqual(r.send.await_args.args[2], "t@x.org")

    async def test_a_bad_send_to_is_refused_before_anything_is_written(self) -> None:
        from synapse.api.errors import SynapseError

        r = self._resource()
        for bad in ("nobody", 7, "a@" + "x" * 400):
            with self.subTest(bad=bad), self.assertRaises(SynapseError) as caught:
                await r.remind(self._body(send_to=bad))
            self.assertEqual(caught.exception.code, 400)
        r.invitations.set_delivery_address.assert_not_awaited()
        r.send.assert_not_awaited()

    async def test_an_unsubscribe_link_must_be_shown_in_both_parts(self) -> None:
        from synapse.api.errors import SynapseError

        link = "https://api.pangea.chat/cms/u/abc?x=1&y=2"
        r = self._resource()
        await r.remind(
            self._body(
                html=f'<a href="{{{{cta_url}}}}">A</a><a href="{link.replace("&", "&amp;")}">U</a>',
                text=f"A: {{{{cta_url}}}} U: {link}",
                unsubscribe_url=link,
            )
        )
        self.assertEqual(r.send.await_args.kwargs["rendered"]["unsubscribe_url"], link)
        for extra in (
            {"unsubscribe_url": link},  # not shown in either part
            {
                "unsubscribe_url": "http://api.pangea.chat/u",
                "text": "http://api.pangea.chat/u {{cta_url}}",
            },
        ):
            with self.subTest(extra=extra), self.assertRaises(SynapseError) as caught:
                await r.remind(self._body(**extra))
            self.assertEqual(caught.exception.code, 400)

    async def test_a_claimed_email_stores_its_destinations(self) -> None:
        """The claimed email's links are destinations, signed at the claim; a
        dashboard destination may leave its course for the claim to fill."""
        from synapse.api.errors import SynapseError

        r = self._resource()
        claimed = {
            "subject": "Spanish 1 is ready",
            "html": '<a href="{{cta_url}}">Open</a> <a href="{{cta2_url}}">Help</a>',
            "text": "Open: {{cta_url}} Help: {{cta2_url}}",
            "destination": {"kind": "dashboard"},
            "secondary_destination": {
                "kind": "external",
                "url": "https://calendar.app.google/abc",
            },
        }
        await r.remind(self._body(claimed_email=claimed))
        stored = r.invitations.store_claimed_email.await_args.args[1]
        self.assertEqual(stored["destination"], {"kind": "dashboard"})
        self.assertEqual(
            stored["secondary_destination"],
            {"kind": "external", "url": "https://calendar.app.google/abc"},
        )
        for broken in (
            {
                **claimed,
                "secondary_destination": None,
            },  # {{cta2_url}} with no destination
            {**claimed, "destination": None, "secondary_destination": None},
            {
                **claimed,
                "destination": {"kind": "external", "url": "https://evil.example/"},
            },
            {**claimed, "destination": {"kind": "course", "course_room_id": "nope"}},
        ):
            with self.assertRaises(SynapseError) as caught:
                await r.remind(self._body(claimed_email=broken))
            self.assertEqual(caught.exception.code, 400)


class TestV2PrepareWithoutEmail(unittest.IsolatedAsyncioTestCase):
    def _resource(self):
        from synapse_pangea_chat.email_invite.course_invitation_api import (
            CourseInvitationAPI,
        )

        api: Any = MagicMock()
        api._hs.get_clock.return_value.time_msec.return_value = 7
        invitations = MagicMock()
        invitations.prepare = AsyncMock(return_value=("inv-9", True))
        invitations.status = AsyncMock(
            return_value={"invitation_id": "inv-9", "status": "prepared"}
        )
        resource = CourseInvitationAPI(
            api, PangeaChatConfig(), MagicMock(), invitations, MagicMock(), "prepare"
        )
        resource.code = AsyncMock(return_value="c0de123")
        resource.send = AsyncMock()
        return resource

    async def test_send_email_false_prepares_and_sends_nothing(self) -> None:
        r = self._resource()
        body = {
            "request_key": "k",
            "teacher_email": "t@x.org",
            "title": "T",
            "course_plan_id": "plan",
            "target_language": "es",
        }
        out = await r.prepare("@op:x", {**body, "send_email": False})
        self.assertEqual(out["status"], "prepared")
        r.send.assert_not_awaited()
        await r.prepare("@op:x", body)
        r.send.assert_awaited_once()
        from synapse.api.errors import SynapseError

        with self.assertRaises(SynapseError):
            await r.prepare("@op:x", {**body, "send_email": "no"})


class TestClaimedEmailSlots(unittest.TestCase):
    def test_the_claimed_email_carries_the_class_link_and_nothing_else_to_fill(
        self,
    ) -> None:
        from synapse_pangea_chat.email_invite.course_claim_emails import (
            fill_class_slots,
            validate_claimed,
        )

        def problem(html: str, text: str) -> str:
            return validate_claimed(html, text) or ""

        self.assertIsNone(
            validate_claimed('<a href="{{class_url}}">Join</a>', "Join: {{class_url}}")
        )
        self.assertIn("{{class_url}}", problem("<p>no link</p>", "{{class_url}}"))
        self.assertIn(
            "{{cta_url}}",
            problem("{{class_url}} {{cta_url}}", "{{class_url}}"),
        )
        self.assertIn(
            "{{unsubscribe_url}}",
            problem("{{class_url}}", "{{class_url}} {{unsubscribe_url}}"),
        )
        html, text = fill_class_slots(
            '<a href="{{class_url}}">{{class_code}}</a>{{receiving_reason}}',
            "{{class_url}} {{postal_address}}",
            class_url="https://app.example/a&b",
            class_code="c1",
            receiving_reason="<r>",
            postal_address="1 St",
        )
        self.assertEqual(html, '<a href="https://app.example/a&amp;b">c1</a>&lt;r&gt;')
        self.assertEqual(text, "https://app.example/a&b 1 St")
        # The QR code is an image source in the HTML; plain text cannot show it.
        self.assertIsNone(
            validate_claimed(
                '<a href="{{class_url}}">Join</a><img src="{{class_qr}}">',
                "Join: {{class_url}}",
            )
        )
        self.assertIn(
            "{{class_qr}}",
            problem("{{class_url}}", "{{class_url}} {{class_qr}}"),
        )
        html, text = fill_class_slots(
            '<img src="{{class_qr}}"><a href="{{class_url}}">x</a>',
            "{{class_url}}",
            class_url="https://app.example/c1",
            class_code="c1",
            receiving_reason="",
            postal_address="",
            class_qr_src="cid:class-qr.1@host",
        )
        self.assertEqual(
            html,
            '<img src="cid:class-qr.1@host"><a href="https://app.example/c1">x</a>',
        )


if __name__ == "__main__":
    unittest.main()
