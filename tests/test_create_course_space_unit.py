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
    build_admin_join_url,
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


class TestBuildAdminJoinUrl(unittest.TestCase):
    def test_uses_the_configured_app_host_not_a_hardcoded_one(self) -> None:
        """The host follows ``app_base_url`` (ansible sets it per env).

        The bug this pins: a hardcoded ``pangea.chat`` handed every staging
        course a production join link. Staging config resolves to the staging
        app host and prod to the prod one.
        """
        self.assertEqual(
            build_admin_join_url("https://app.staging.pangea.chat", "04wpy5e"),
            "https://app.staging.pangea.chat/04wpy5e",
        )
        self.assertEqual(
            build_admin_join_url("https://app.pangea.chat", "abc123x"),
            "https://app.pangea.chat/abc123x",
        )

    def test_emits_the_short_code_form_not_the_classcode_route(self) -> None:
        """External form is ``<app>/<code>`` — the CloudFront 302 source — not
        the client's internal ``/#/join_with_link?classcode=`` spelling."""
        url = build_admin_join_url("https://app.pangea.chat", "xyz789q")

        self.assertNotIn("join_with_link", url)
        self.assertNotIn("classcode", url)
        self.assertTrue(url.endswith("/xyz789q"))

    def test_trailing_slash_on_base_is_not_doubled(self) -> None:
        self.assertEqual(
            build_admin_join_url("https://app.pangea.chat/", "code42x"),
            "https://app.pangea.chat/code42x",
        )


if __name__ == "__main__":
    unittest.main()
