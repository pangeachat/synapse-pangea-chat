"""Unit tests for the ``pangea.course_plan`` content create_course_space writes.

That content is the whole of what Matrix carries about a server-created course:
the catalog reads the plan id and the target language straight off it, with no
CMS call on the read path. A key-name slip here does not fail loudly — it
silently drops the space out of Browse — so the write shape is pinned exactly,
and separately checked against the catalog's own extractors so the two halves
cannot drift apart.
"""

import unittest
from typing import Any

from synapse_pangea_chat.email_invite.create_course_space import (
    build_course_plan_content,
)
from synapse_pangea_chat.public_courses.get_public_courses import (
    extract_l2,
    extract_plan_id,
)


class TestBuildCoursePlanContent(unittest.TestCase):
    def test_plan_id_is_written_under_uuid(self) -> None:
        """``uuid``, not ``course_plan_id``.

        The catalog reads ``uuid`` first and falls back to ``course_plan_id``,
        so writing the fallback key still works — which is exactly why the
        mismatch survived unnoticed. Pinning the key keeps the write on the
        primary one instead of drifting onto the compatibility path.
        """
        content = build_course_plan_content("plan-abc", None)

        self.assertEqual(content["uuid"], "plan-abc")
        self.assertNotIn("course_plan_id", content)

    def test_plan_id_value_is_preserved_exactly(self) -> None:
        """No trimming, casing, or normalisation — the id is an opaque token."""
        for plan_id in (
            "plan-abc",
            "PLAN-ABC",
            "  padded-id  ",
            "67f1a2b3c4d5e6f7a8b9c0d1",
            "plan/with/slashes",
        ):
            with self.subTest(plan_id=plan_id):
                self.assertEqual(
                    build_course_plan_content(plan_id, None)["uuid"], plan_id
                )

    def test_l2_present_when_target_language_supplied(self) -> None:
        for language in ("es", "fr", "es-MX", "zh-Hant"):
            with self.subTest(language=language):
                content = build_course_plan_content("plan-abc", language)
                self.assertEqual(content["l2"], language)

    def test_l2_is_trimmed(self) -> None:
        content = build_course_plan_content("plan-abc", "  es-MX \n")

        self.assertEqual(content["l2"], "es-MX")

    def test_l2_absent_when_target_language_missing_or_unusable(self) -> None:
        """Absent, not null and not empty.

        ``extract_l2`` treats an empty string as absent, so an empty ``l2``
        would behave the same on the read path today — but it would also be a
        second shape for "no language", and the backfill that repairs these
        spaces looks for a missing key.
        """
        unusable: tuple[Any, ...] = (
            None,
            "",
            "   ",
            "\t\n",
            123,
            0,
            True,
            False,
            ["es"],
            {"code": "es"},
        )
        for target_language in unusable:
            with self.subTest(target_language=repr(target_language)):
                content = build_course_plan_content("plan-abc", target_language)
                self.assertNotIn("l2", content)

    def test_content_carries_nothing_else(self) -> None:
        """Only the two fields the catalog reads.

        The CMS stays authoritative for everything else about a plan; anything
        extra written here is a second copy that immediately starts going
        stale.
        """
        self.assertEqual(set(build_course_plan_content("plan-abc", None)), {"uuid"})
        self.assertEqual(
            set(build_course_plan_content("plan-abc", "es")), {"uuid", "l2"}
        )


class TestCatalogReadsWhatWeWrite(unittest.TestCase):
    """The write shape, checked through the catalog's own extractors.

    ``extract_plan_id`` / ``extract_l2`` are the single definition of how a
    ``pangea.course_plan`` content is read. Asserting through them rather than
    against literal key names means a future change to that rule either keeps
    these passing or fails here — a test that restated the keys would keep
    passing while the catalog stopped agreeing.
    """

    def test_written_plan_id_is_the_one_the_catalog_reads(self) -> None:
        content = build_course_plan_content("plan-abc", "es")

        self.assertEqual(extract_plan_id(content), "plan-abc")

    def test_written_l2_is_the_one_the_catalog_reads(self) -> None:
        content = build_course_plan_content("plan-abc", "  es-MX  ")

        self.assertEqual(extract_l2(content), "es-MX")

    def test_catalog_sees_no_language_when_none_was_supplied(self) -> None:
        content = build_course_plan_content("plan-abc", None)

        self.assertIsNone(extract_l2(content))

    def test_space_created_without_a_plan_id_is_not_a_course(self) -> None:
        """``course_plan_id`` defaults to ``""`` in the handler.

        Empty is absent to the catalog, so such a space is published but not
        eligible. That is the intended outcome rather than a half-course in
        Browse with no plan behind it, and it is worth pinning: a change that
        made the empty id read as present would put unusable rooms in the
        catalog.
        """
        content = build_course_plan_content("", "es")

        self.assertIsNone(extract_plan_id(content))


if __name__ == "__main__":
    unittest.main()


class TestSendClaimLink(unittest.IsolatedAsyncioTestCase):
    """A failed claim-link email is captured and reported, never raised: the
    space and its claim already exist (create-course-space.instructions.md)."""

    def _resource(self) -> Any:
        from unittest.mock import AsyncMock, MagicMock

        from synapse_pangea_chat.config import PangeaChatConfig
        from synapse_pangea_chat.email_invite.create_course_space import (
            CreateCourseSpace,
        )

        mailer = MagicMock()
        mailer.send_course_ready = AsyncMock()
        return (
            CreateCourseSpace(MagicMock(), PangeaChatConfig(), MagicMock(), mailer),
            mailer,
        )

    async def _run(self, resource: Any) -> bool:
        return await resource._send_claim_link(
            room_id="!r:x",
            teacher_email="teacher@school.example",
            title="Spanish 1",
            description="Lessons 1 to 6",
            request_summary="Spanish 1 practice",
            claim_url="https://app.pangea.chat/adm1nab",
            claim_code="adm1nab",
        )

    async def test_sends_the_claim_link(self) -> None:
        resource, mailer = self._resource()

        self.assertTrue(await self._run(resource))

        sent = mailer.send_course_ready.await_args.kwargs
        self.assertEqual(sent["email_address"], "teacher@school.example")
        self.assertEqual(sent["claim_url"], "https://app.pangea.chat/adm1nab")
        self.assertEqual(sent["claim_code"], "adm1nab")
        self.assertEqual(sent["request_summary"], "Spanish 1 practice")

    async def test_failed_send_is_captured_and_reported(self) -> None:
        from unittest.mock import patch

        resource, mailer = self._resource()
        mailer.send_course_ready.side_effect = RuntimeError("smtp down")

        with patch(
            "synapse_pangea_chat.email_invite.create_course_space._capture_exception"
        ) as capture:
            self.assertFalse(await self._run(resource))

        capture.assert_called_once()


class TestClaimEmailTemplates(unittest.TestCase):
    """The rendered emails: the first carries the claim link and nothing for
    students; the second carries the class link."""

    @staticmethod
    def _env() -> Any:
        import jinja2

        from synapse_pangea_chat.email_invite.course_claim_emails import (
            TEMPLATES_DIR,
        )

        return jinja2.Environment(
            loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
            autoescape=jinja2.select_autoescape(["html"]),
        )

    def test_course_ready_carries_the_claim_link_and_no_class_code(self) -> None:
        env = self._env()
        for name in ("course_ready.html", "course_ready.txt"):
            with self.subTest(template=name):
                out = env.get_template(name).render(
                    app_name="Pangea Chat",
                    course_title="Spanish 1",
                    course_description="Lessons 1 to 6",
                    request_summary="Spanish 1 practice",
                    claim_url="https://app.pangea.chat/adm1nab",
                    claim_code="adm1nab",
                )
                self.assertIn("https://app.pangea.chat/adm1nab", out)
                self.assertIn("Spanish 1", out)
                self.assertNotIn("class code", out.lower())

    def test_course_ready_carries_only_the_link_and_says_the_tap_leads_to_sign_in(
        self,
    ) -> None:
        """No printed code and no store badges (decided 2026-10-09): the link is
        the only claim path in mail, and the email says the tap leads to
        sign-up or log-in so an app-less teacher is not surprised."""
        env = self._env()
        for name in ("course_ready.html", "course_ready.txt"):
            for claims_by_address in (True, False):
                with self.subTest(template=name, claims_by_address=claims_by_address):
                    out = env.get_template(name).render(
                        app_name="Pangea Chat",
                        course_title="Spanish 1",
                        course_description="",
                        request_summary=None,
                        claim_url="https://app.pangea.chat/adm1nab",
                        claim_code="adm1nab",
                        claims_by_address=claims_by_address,
                    )
                    words = " ".join(out.split())
                    self.assertIn("sign up or log in", words)
                    without_link = out.replace("https://app.pangea.chat/adm1nab", "")
                    self.assertNotIn("adm1nab", without_link)
                    self.assertNotIn("course code", words.lower())
                    self.assertNotIn("apps.apple.com", out)
                    self.assertNotIn("play.google.com", out)

    def test_course_ready_is_the_canonical_quest_card(self) -> None:
        """Parity with the course_invite/teacher_invite copy in the production
        CMS's engagement copy collection: the cover when the request carried
        one, the title and the "Start my quest" button inside the card, one
        medium per email, and the re-tap line for a teacher who installed the
        app first."""
        env = self._env()
        for image in ("https://cdn.example/cover.jpg", ""):
            with self.subTest(image=bool(image)):
                html = env.get_template("course_ready.html").render(
                    app_name="Pangea Chat",
                    course_title="Spanish 1",
                    course_description="Lessons 1 to 6",
                    request_summary=None,
                    claim_url="https://app.pangea.chat/adm1nab",
                    course_image_url=image,
                )
                flat = " ".join(html.split())
                self.assertIn("Your quest is ready", flat)
                self.assertEqual(
                    ('<img src="https://cdn.example/cover.jpg"' in html), bool(image)
                )
                self.assertNotIn("<video", html)
                self.assertNotIn("youtube", html.lower())
                card = html[html.index("<table") : html.index("</table>")]
                self.assertIn("Spanish 1", card)
                self.assertIn(">Start my quest</a>", card)
                self.assertIn("Tap the button again after you sign in", flat)
                self.assertNotIn("Open your course", flat)
                text = env.get_template("course_ready.txt").render(
                    app_name="Pangea Chat",
                    course_title="Spanish 1",
                    course_description="",
                    request_summary=None,
                    claim_url="https://app.pangea.chat/adm1nab",
                    course_image_url=image,
                )
                self.assertIn("Start my quest:", text)
                self.assertIn("Tap the link again after you sign in", text)

    def test_course_claimed_carries_class_link_and_claimer(self) -> None:
        env = self._env()
        for name in ("course_claimed.html", "course_claimed.txt"):
            with self.subTest(template=name):
                out = env.get_template(name).render(
                    app_name="Pangea Chat",
                    course_title="Spanish 1",
                    class_url="https://app.pangea.chat/cls4abc",
                    class_code="cls4abc",
                    class_qr_src="cid:class-qr.1@host",
                )
                self.assertIn("https://app.pangea.chat/cls4abc", out)
                self.assertIn("cls4abc", out)
                self.assertNotIn("Apple Inc.", out)
                if name.endswith(".html"):
                    self.assertIn('src="cid:class-qr.1@host"', out)
                else:
                    self.assertNotIn("cid:", out)

    def test_html_escapes_request_text(self) -> None:
        out = (
            self._env()
            .get_template("course_ready.html")
            .render(
                app_name="Pangea Chat",
                course_title="<b>x</b>",
                course_description="",
                request_summary="<script>alert(1)</script>",
                claim_url="https://app.pangea.chat/adm1nab",
                claim_code="adm1nab",
            )
        )
        self.assertNotIn("<script>", out)
        self.assertNotIn("<b>x</b>", out)


class TestClaimEmailsCarryOnlyTheLink(unittest.IsolatedAsyncioTestCase):
    """The first email and a reminder carry the claim link and nothing else
    that claims: no printed code, no store links, no badge trademark line
    (decided 2026-10-09; create-course-space.instructions.md)."""

    async def test_ready_and_reminder_print_no_code_and_no_store_links(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from synapse_pangea_chat.email_invite import course_claim_emails

        env = TestClaimEmailTemplates._env()
        api = MagicMock()
        api._hs.config.email.email_app_name = "Pangea Chat"
        api.read_templates.side_effect = lambda names, custom_template_directory: [
            env.get_template(name) for name in names
        ]
        mailer = course_claim_emails.CourseClaimMailer(api)
        send = AsyncMock()
        mailer._send = send  # type: ignore[method-assign]
        claim_url = "https://app.pangea.chat/adm1nab"
        claim_code = "adm1nab"

        for image in ("", "https://cdn.example/cover.jpg"):
            await mailer.send_course_ready(
                email_address="teacher@school.example",
                course_title="Spanish 1",
                course_description="",
                request_summary=None,
                claim_url=claim_url,
                claim_code=claim_code,
                course_image_url=image,
            )
        await mailer.send_course_reminder(
            email_address="teacher@school.example",
            subject="Your course is waiting",
            body="Open it to become its teacher.",
            cta_label="Open your course",
            claim_url=claim_url,
            claim_code=claim_code,
        )

        self.assertEqual(send.await_count, 3)
        self.assertEqual(
            send.await_args_list[0].kwargs["subject"], "Your quest is ready: Spanish 1"
        )
        self.assertIn("cdn.example/cover.jpg", send.await_args_list[1].kwargs["html"])
        self.assertNotIn("cdn.example", send.await_args_list[0].kwargs["html"])
        for sent in send.await_args_list:
            for part in ("html", "text"):
                out = sent.kwargs[part]
                with self.subTest(subject=sent.kwargs["subject"], part=part):
                    self.assertIn(claim_url, out)
                    without_link = out.replace(claim_url, "")
                    self.assertNotIn(claim_code, without_link)
                    self.assertNotIn("course code", without_link.lower())
                    self.assertNotIn("apps.apple.com", out)
                    self.assertNotIn("play.google.com", out)
                    self.assertNotIn("trademark of Apple", out)


class TestMailerBound(unittest.IsolatedAsyncioTestCase):
    """Every claim email is bounded: Synapse's mailer does not time out a
    stalled SMTP transaction (course_claim_emails)."""

    async def test_a_stalled_send_raises_instead_of_hanging(self) -> None:
        from unittest.mock import MagicMock, patch

        from twisted.internet import defer

        from synapse_pangea_chat.email_invite import course_claim_emails

        api = MagicMock()
        api.read_templates.return_value = [MagicMock() for _ in range(6)]
        mailer = course_claim_emails.CourseClaimMailer(api)

        with patch.object(
            course_claim_emails,
            "timeout_deferred",
            return_value=defer.fail(defer.TimeoutError()),
        ) as bounded:
            with self.assertRaises(defer.TimeoutError):
                await mailer.send_course_ready(
                    email_address="teacher@school.example",
                    course_title="Spanish 1",
                    course_description="",
                    request_summary=None,
                    claim_url="https://app.pangea.chat/adm1nab",
                    claim_code="adm1nab",
                )

        self.assertEqual(
            bounded.call_args.kwargs["timeout"],
            course_claim_emails.SEND_TIMEOUT_SECONDS,
        )


class TestShareKitQrCode(unittest.IsolatedAsyncioTestCase):
    """The share kit carries the class link's QR code as an inline image
    (create-course-space.instructions.md): attached where the HTML asks for it,
    and sent through Synapse's own mailer, whose internals are checked here
    against the Synapse version the tests run."""

    CLASS_URL = "https://app.pangea.chat/cls4abc"

    def _mailer(self) -> tuple[Any, Any]:
        from unittest.mock import AsyncMock, MagicMock

        from synapse.handlers.send_email import SendEmailHandler

        from synapse_pangea_chat.email_invite import course_claim_emails

        hs = MagicMock()
        email_config = hs.config.email
        email_config.email_notif_from = "%(app)s <noreply@pangea.example>"
        email_config.email_smtp_host = "smtp.example"
        email_config.email_smtp_port = 25
        email_config.email_smtp_user = None
        email_config.email_smtp_pass = None
        email_config.require_transport_security = False
        email_config.enable_smtp_tls = True
        email_config.force_tls = False
        email_config.email_tlsname = None
        handler = SendEmailHandler(hs)
        sendmail = AsyncMock()
        handler._sendmail = sendmail  # type: ignore[method-assign]

        env = TestClaimEmailTemplates._env()
        api = MagicMock()
        api._hs.config.email.email_app_name = "Pangea Chat"
        api._hs.get_send_email_handler.return_value = handler
        api.read_templates.side_effect = lambda names, custom_template_directory: [
            env.get_template(name) for name in names
        ]
        return course_claim_emails.CourseClaimMailer(api), sendmail

    async def _send_claimed(self, rendered: dict[str, str] | None) -> Any:
        import email
        from unittest.mock import patch

        from synapse_pangea_chat.email_invite import course_claim_emails

        mailer, sendmail = self._mailer()
        with patch.object(
            course_claim_emails,
            "timeout_deferred",
            side_effect=lambda deferred, timeout, clock: deferred,
        ):
            await mailer.send_course_claimed(
                email_address="teacher@school.example",
                course_title="Spanish 1",
                class_url=self.CLASS_URL,
                class_code="cls4abc",
                rendered=rendered,
            )
        sendmail.assert_awaited_once()
        args = sendmail.await_args.args
        self.assertEqual(
            args[1:5],
            ("smtp.example", 25, "noreply@pangea.example", "teacher@school.example"),
        )
        return email.message_from_bytes(args[5])

    def _assert_qr_attached(self, message: Any) -> None:
        from synapse_pangea_chat.email_invite.course_claim_emails import class_qr_png

        self.assertEqual(message.get_content_type(), "multipart/alternative")
        plain, related = message.get_payload()
        self.assertEqual(plain.get_content_type(), "text/plain")
        self.assertEqual(related.get_content_type(), "multipart/related")
        html_part, image = related.get_payload()
        self.assertEqual(html_part.get_content_type(), "text/html")
        self.assertEqual(image.get_content_type(), "image/png")
        self.assertTrue(image["Content-Disposition"].startswith("inline"))
        content_id = image["Content-ID"].strip("<>")
        html = html_part.get_payload(decode=True).decode()
        self.assertIn(f'src="cid:{content_id}"', html)
        self.assertNotIn("{{class_qr}}", html)
        # The image is the QR code of the link the email prints.
        self.assertEqual(image.get_payload(decode=True), class_qr_png(self.CLASS_URL))
        self.assertNotIn("cid:", plain.get_payload(decode=True).decode())
        # The headers Synapse's own mailer sets.
        self.assertEqual(message["From"], "Pangea Chat <noreply@pangea.example>")
        self.assertEqual(message["To"], "teacher@school.example")
        self.assertEqual(message["Auto-Submitted"], "auto-generated")
        self.assertEqual(message["X-Auto-Response-Suppress"], "All")
        self.assertIsNotNone(message["Message-ID"])
        self.assertIsNotNone(message["Date"])

    async def test_a_caller_kit_with_the_slot_carries_the_qr_code(self) -> None:
        message = await self._send_claimed(
            {
                "subject": "Invite your students to Spanish 1",
                "html": '<p><a href="{{class_url}}">{{class_code}}</a></p>'
                '<img src="{{class_qr}}" width="200" height="200" alt="QR code">',
                "text": "{{class_url}} {{class_code}}",
            }
        )
        self.assertEqual(message["Subject"], "Invite your students to Spanish 1")
        self._assert_qr_attached(message)

    async def test_a_caller_kit_without_the_slot_sends_with_no_image(self) -> None:
        """A kit stored before the slot existed still sends, through Synapse's
        own mailer, with no image part."""
        message = await self._send_claimed(
            {
                "subject": "Invite your students to Spanish 1",
                "html": '<p><a href="{{class_url}}">{{class_code}}</a></p>',
                "text": "{{class_url}} {{class_code}}",
            }
        )
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        types = [part.get_content_type() for part in message.walk()]
        self.assertEqual(types, ["multipart/alternative", "text/plain", "text/html"])
        self.assertIn(
            self.CLASS_URL, message.get_payload()[1].get_payload(decode=True).decode()
        )

    async def test_the_module_template_carries_the_qr_code(self) -> None:
        message = await self._send_claimed(None)
        self.assertEqual(message["Subject"], "Invite your students to Spanish 1")
        self._assert_qr_attached(message)

    def test_the_transport_the_mailer_borrows_takes_what_it_passes(self) -> None:
        """``_sendmail`` is Synapse-internal: a rename or a changed keyword
        fails here rather than in a claimed teacher's inbox."""
        import inspect

        from synapse.handlers import send_email

        params = inspect.signature(send_email._sendmail).parameters
        for name in (
            "username",
            "password",
            "require_auth",
            "require_tls",
            "enable_tls",
            "force_tls",
            "tlsname",
        ):
            self.assertIn(name, params)


class TestCourseCopyAtCreation(unittest.IsolatedAsyncioTestCase):
    """The course copy rides in the space's initial state, sent by the
    requester at power level 100; without one the course is created as before
    (create-course-space.instructions.md, Content Studio CONTRACT)."""

    COPY = {
        "version": 1,
        "revision": 1,
        "missions": [{"lo_id": "lo-a", "activity_ids": ["act-1"]}],
    }

    async def _create(self, copy: Any) -> tuple[Any, Any, Any]:
        from unittest.mock import AsyncMock, MagicMock, patch

        from synapse_pangea_chat.config import PangeaChatConfig
        from synapse_pangea_chat.email_invite.create_course_space import (
            CreateCourseSpace,
        )

        api = MagicMock()
        api.server_name = "x"
        requester = MagicMock()
        requester.user.to_string.return_value = "@bot:x"
        api._hs.get_auth.return_value.get_user_by_req = AsyncMock(
            return_value=requester
        )
        create_room = AsyncMock(return_value=("!room:x", None, None))
        api._hs.get_room_creation_handler.return_value.create_room = create_room
        claim_store = MagicMock()
        claim_store.record = AsyncMock()
        resource = CreateCourseSpace(
            api,
            PangeaChatConfig(cms_base_url="http://cms", cms_service_api_key="k"),
            claim_store,
            MagicMock(),
        )
        read = AsyncMock(return_value=copy)
        prefix = "synapse_pangea_chat.email_invite.create_course_space."
        with (
            patch(
                prefix + "extract_body_json",
                AsyncMock(return_value={"title": "Spanish 1", "course_plan_id": "q1"}),
            ),
            patch(prefix + "new_unique_code", AsyncMock(side_effect=["c1", "c2"])),
            patch(prefix + "respond_with_json") as respond,
            patch.object(resource._course_missions, "read", read),
        ):
            await resource._async_render_POST(MagicMock())
        return create_room, respond, read

    @staticmethod
    def _state(create_room: Any) -> dict[str, Any]:
        config = create_room.await_args.kwargs["config"]
        return {event["type"]: event for event in config["initial_state"]}

    async def test_copy_is_written_in_initial_state(self) -> None:
        create_room, respond, read = await self._create(self.COPY)

        read.assert_awaited_once_with("q1")
        state = self._state(create_room)
        event = state["pangea.course_missions"]
        self.assertEqual(event["state_key"], "")
        self.assertEqual(event["content"], self.COPY)
        # Sent by the room creator, who holds 100 in the initial power levels,
        # and only that level may write the copy.
        self.assertEqual(
            create_room.await_args.kwargs["requester"].user.to_string(), "@bot:x"
        )
        power = state["m.room.power_levels"]["content"]
        self.assertEqual(power["users"], {"@bot:x": 100})
        self.assertEqual(power["events"]["pangea.course_missions"], 100)
        self.assertEqual(respond.call_args.args[1], 200)

    async def test_no_copy_still_creates_the_course(self) -> None:
        create_room, respond, _ = await self._create(None)

        state = self._state(create_room)
        self.assertNotIn("pangea.course_missions", state)
        self.assertEqual(
            state["m.room.power_levels"]["content"]["events"]["pangea.course_missions"],
            100,
        )
        self.assertEqual(respond.call_args.args[1], 200)
        self.assertEqual(respond.call_args.args[2]["room_id"], "!room:x")


class TestHungCmsStillCreatesTheCourse(unittest.TestCase):
    """A CMS that never answers costs course creation at most 3 s: the copy
    read gives up and the course is created without it. Driven on a fake
    reactor clock, so the test takes no real time."""

    def test_course_created_within_the_cap(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from synapse.util.clock import Clock
        from twisted.internet import defer
        from twisted.internet.testing import MemoryReactorClock

        from synapse_pangea_chat.config import PangeaChatConfig
        from synapse_pangea_chat.email_invite.create_course_space import (
            CreateCourseSpace,
        )

        reactor = MemoryReactorClock()
        api = MagicMock()
        api.server_name = "x"
        api._hs.get_clock.return_value = Clock(reactor, server_name="x")  # type: ignore[arg-type]
        requester = MagicMock()
        requester.user.to_string.return_value = "@bot:x"
        api._hs.get_auth.return_value.get_user_by_req = AsyncMock(
            return_value=requester
        )
        hung: list[defer.Deferred] = []

        async def request(*_args: Any, **_kwargs: Any) -> Any:
            d: defer.Deferred = defer.Deferred()
            hung.append(d)
            return await d

        api.http_client.request = request
        create_room = AsyncMock(return_value=("!room:x", None, None))
        api._hs.get_room_creation_handler.return_value.create_room = create_room
        claim_store = MagicMock()
        claim_store.record = AsyncMock()
        resource = CreateCourseSpace(
            api,
            PangeaChatConfig(cms_base_url="http://cms", cms_service_api_key="k"),
            claim_store,
            MagicMock(),
        )
        prefix = "synapse_pangea_chat.email_invite.create_course_space."
        with (
            patch(
                prefix + "extract_body_json",
                AsyncMock(return_value={"title": "Spanish 1", "course_plan_id": "q1"}),
            ),
            patch(prefix + "new_unique_code", AsyncMock(side_effect=["c1", "c2"])),
            patch(prefix + "respond_with_json") as respond,
            self.assertLogs(
                "synapse.module.synapse_pangea_chat.email_invite.course_missions",
                "WARNING",
            ),
        ):
            done = defer.ensureDeferred(resource._async_render_POST(MagicMock()))
            self.assertEqual(len(hung), 1)
            # Exact binary fractions: the clock lands on 3.0 s, not near it.
            reactor.advance(2.75)
            self.assertFalse(done.called)
            create_room.assert_not_awaited()
            reactor.advance(0.25)
            self.assertTrue(done.called)

        self.assertTrue(hung[0].called)  # the hung request was cancelled
        create_room.assert_awaited_once()
        assert create_room.await_args is not None
        config = create_room.await_args.kwargs["config"]
        self.assertNotIn(
            "pangea.course_missions", {e["type"] for e in config["initial_state"]}
        )
        self.assertEqual(respond.call_args.args[1], 200)
