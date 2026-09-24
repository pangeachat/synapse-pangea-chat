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


class TestRecordAndEmail(unittest.IsolatedAsyncioTestCase):
    """The claim link goes out only once the requesting address is recorded,
    because the claim is what sends the class link, to that address
    (create-course-space.instructions.md)."""

    def _resource(self) -> Any:
        from unittest.mock import AsyncMock, MagicMock

        from synapse_pangea_chat.config import PangeaChatConfig
        from synapse_pangea_chat.email_invite.create_course_space import (
            CreateCourseSpace,
        )

        api = MagicMock()
        api._hs.get_clock.return_value.time_msec.return_value = 5
        store = MagicMock()
        store.record = AsyncMock()
        mailer = MagicMock()
        mailer.send_course_ready = AsyncMock()
        resource = CreateCourseSpace(api, PangeaChatConfig(), store, mailer)
        return resource, store, mailer

    async def _run(self, resource: Any) -> bool:
        return await resource._record_and_email(
            room_id="!r:x",
            teacher_email="teacher@school.example",
            title="Spanish 1",
            description="Lessons 1 to 6",
            request_summary="Spanish 1 practice",
            admin_code="adm1nab",
            claim_url="https://app.pangea.chat/adm1nab",
        )

    async def test_records_then_sends_the_claim_link(self) -> None:
        resource, store, mailer = self._resource()

        self.assertTrue(await self._run(resource))

        store.record.assert_awaited_once_with(
            "!r:x", "teacher@school.example", "adm1nab", 5
        )
        sent = mailer.send_course_ready.await_args.kwargs
        self.assertEqual(sent["email_address"], "teacher@school.example")
        self.assertEqual(sent["claim_url"], "https://app.pangea.chat/adm1nab")
        self.assertEqual(sent["request_summary"], "Spanish 1 practice")

    async def test_no_record_means_no_email(self) -> None:
        from unittest.mock import patch

        resource, store, mailer = self._resource()
        store.record.side_effect = RuntimeError("db down")

        with patch(
            "synapse_pangea_chat.email_invite.create_course_space._capture_exception"
        ) as capture:
            self.assertFalse(await self._run(resource))

        mailer.send_course_ready.assert_not_called()
        capture.assert_called_once()

    async def test_failed_send_is_captured_and_reported(self) -> None:
        from unittest.mock import patch

        resource, _, mailer = self._resource()
        mailer.send_course_ready.side_effect = RuntimeError("smtp down")

        with patch(
            "synapse_pangea_chat.email_invite.create_course_space._capture_exception"
        ) as capture:
            self.assertFalse(await self._run(resource))

        capture.assert_called_once()


class TestClaimEmailTemplates(unittest.TestCase):
    """The rendered emails: the first carries the claim link and nothing for
    students; the second carries the class link and names the claimer."""

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
                )
                self.assertIn("https://app.pangea.chat/adm1nab", out)
                self.assertIn("Spanish 1", out)
                self.assertNotIn("class code", out.lower())

    def test_course_claimed_carries_class_link_and_claimer(self) -> None:
        env = self._env()
        for name in ("course_claimed.html", "course_claimed.txt"):
            with self.subTest(template=name):
                out = env.get_template(name).render(
                    app_name="Pangea Chat",
                    course_title="Spanish 1",
                    claimed_by_user_id="@rivera:pangea.chat",
                    claimed_by_display_name="Ms. Rivera",
                    class_url="https://app.pangea.chat/cls4abc",
                    class_code="cls4abc",
                )
                self.assertIn("https://app.pangea.chat/cls4abc", out)
                self.assertIn("cls4abc", out)
                self.assertIn("@rivera:pangea.chat", out)
                self.assertIn("Ms. Rivera", out)

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
            )
        )
        self.assertNotIn("<script>", out)
        self.assertNotIn("<b>x</b>", out)
