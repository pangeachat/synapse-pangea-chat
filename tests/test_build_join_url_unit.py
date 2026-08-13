"""Unit tests for the course join link the server emails out.

The link is the only way an invited learner reaches the course, and a wrong
shape fails silently: the app boots, lands on the world map, and nothing says
the join was dropped. So the external form is pinned exactly, and pinned once
for every emitter — the bug behind these tests was two emitters building the
same URL by hand, one migrated to the short code and one left on the retired
``/#/join_with_link?classcode=`` spelling.
"""

import pathlib
import unittest

import synapse_pangea_chat
from synapse_pangea_chat.email_invite.build_join_url import build_join_url


class TestBuildJoinUrl(unittest.TestCase):
    def test_uses_the_configured_app_host_not_a_hardcoded_one(self) -> None:
        """The host follows ``app_base_url`` (ansible sets it per env).

        The bug this pins: a hardcoded ``pangea.chat`` handed every staging
        course a production join link. Staging config resolves to the staging
        app host and prod to the prod one.
        """
        self.assertEqual(
            build_join_url("https://app.staging.pangea.chat", "04wpy5e"),
            "https://app.staging.pangea.chat/04wpy5e",
        )
        self.assertEqual(
            build_join_url("https://app.pangea.chat", "abc123x"),
            "https://app.pangea.chat/abc123x",
        )

    def test_emits_the_short_code_form_not_the_classcode_route(self) -> None:
        """External form is the bare short code ``<app>/<code>`` — the one shape
        the client routes — not the retired ``/#/join_with_link?classcode=``."""
        url = build_join_url("https://app.pangea.chat", "xyz789q")

        self.assertNotIn("join_with_link", url)
        self.assertNotIn("classcode", url)
        self.assertTrue(url.endswith("/xyz789q"))

    def test_trailing_slash_on_base_is_not_doubled(self) -> None:
        self.assertEqual(
            build_join_url("https://app.pangea.chat/", "code42x"),
            "https://app.pangea.chat/code42x",
        )


class TestNoModuleBuildsTheRetiredRoute(unittest.TestCase):
    def test_retired_spelling_appears_nowhere_in_the_module(self) -> None:
        """No module hand-rolls the retired link shape.

        Both emitters go through ``build_join_url``, so fixing the format in one
        place fixes it everywhere — but only as long as nobody writes the old
        spelling out again. That is exactly how the course-invite email stayed
        broken after the sibling emitter was migrated.
        """
        package_root = pathlib.Path(synapse_pangea_chat.__file__).parent
        # The helper itself names the retired shape, to say it is retired.
        helper = package_root / "email_invite" / "build_join_url.py"
        offenders = [
            str(path.relative_to(package_root))
            for path in package_root.rglob("*.py")
            if path != helper
            and (
                "join_with_link" in path.read_text() or "classcode" in path.read_text()
            )
        ]

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
