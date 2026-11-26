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
VIDEO = {
    "url": "https://youtu.be/Zhg1_NGdxXQ",
    "title": "Immersive Homework for Language Learning",
    "thumbnail_url": "https://assets.pangea.chat/email/video.png",
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

    async def test_a_video_is_passed_to_the_email(self) -> None:
        resource, _, mailer = _resource(ReminderTarget("t@school.example", False))

        await self._post(resource, {**BODY, "video": VIDEO})

        status, _ = self._response()
        self.assertEqual(status, 200)
        self.assertEqual(mailer.send_course_reminder.await_args.kwargs["video"], VIDEO)

    async def test_no_video_sends_none(self) -> None:
        resource, _, mailer = _resource(ReminderTarget("t@school.example", False))

        await self._post(resource)

        self.assertIsNone(mailer.send_course_reminder.await_args.kwargs["video"])

    async def test_an_invalid_video_is_refused(self) -> None:
        invalid = {
            "not an object": "https://youtu.be/x",
            "http url": {**VIDEO, "url": "http://youtu.be/x"},
            "http thumbnail": {**VIDEO, "thumbnail_url": "http://a.example/t.png"},
            "javascript url": {**VIDEO, "url": "javascript:alert(1)"},
            "no title": {k: v for k, v in VIDEO.items() if k != "title"},
            "long title": {**VIDEO, "title": "x" * 101},
        }
        for case, video in invalid.items():
            with self.subTest(case=case):
                self.respond.reset_mock()
                resource, store, mailer = _resource(
                    ReminderTarget("t@school.example", False)
                )

                await self._post(resource, {**BODY, "video": video})

                status, _ = self._response()
                self.assertEqual(status, 400)
                store.add_code.assert_not_called()
                mailer.send_course_reminder.assert_not_called()

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
        }
        html = env.get_template("course_reminder.html").render(**values)
        text = env.get_template("course_reminder.txt").render(**values)

        self.assertEqual(values["paragraphs"], ["First <b>line</b> wraps.", "Second."])
        self.assertNotIn("<b>line</b>", html)
        self.assertIn('href="https://app.pangea.chat/n3wcod1"', html)
        self.assertIn("Open your course", html)
        self.assertIn("https://app.pangea.chat/n3wcod1", text)
        self.assertIn("Second.", text)
        self.assertNotIn("Watch:", html)
        self.assertNotIn("Watch:", text)

    def test_a_video_is_its_thumbnail_linking_out(self) -> None:
        import jinja2

        from synapse_pangea_chat.email_invite.course_claim_emails import (
            TEMPLATES_DIR,
        )

        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
            autoescape=jinja2.select_autoescape(["html"]),
        )
        values = {
            "app_name": "Pangea Chat",
            "subject": "Reminder",
            "paragraphs": ["Watch this."],
            "cta_label": "Open your course",
            "claim_url": "https://app.pangea.chat/n3wcod1",
            "video": {**VIDEO, "title": "Homework <b>demo</b>"},
        }
        html = env.get_template("course_reminder.html").render(**values)
        text = env.get_template("course_reminder.txt").render(**values)

        self.assertIn('src="https://assets.pangea.chat/email/video.png"', html)
        self.assertEqual(html.count('href="https://youtu.be/Zhg1_NGdxXQ"'), 2)
        self.assertNotIn("<b>demo</b>", html)
        self.assertLess(html.index("youtu.be"), html.index("n3wcod1"))
        self.assertIn("Watch: Homework <b>demo</b>\nhttps://youtu.be/Zhg1_NGdxXQ", text)


if __name__ == "__main__":
    unittest.main()
