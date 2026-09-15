"""The exempt-sender matcher, tested as the security boundary it is.

An exempt sender skips both moderation tiers, so the two properties that
matter are that the match is whole-string and that no operator-supplied
value can turn the send path into a denial of service.
"""

import fnmatch
import itertools
import time
import unittest

from synapse_pangea_chat.moderation.exempt import (
    ExemptGlobError,
    glob_match,
    legacy_key_error,
    matches_every_sender,
    suggest_glob,
    validate_glob,
)


class TestGlobMatch(unittest.TestCase):
    def test_exact_glob_is_anchored_at_both_ends(self) -> None:
        self.assertTrue(glob_match("@bot:example.org", "@bot:example.org"))
        # The bug this replaced: `re.match` anchored only the start, so a
        # longer Matrix ID sharing the prefix was exempted too.
        self.assertFalse(glob_match("@bot:example.org", "@bot:example.org.evil.com"))
        self.assertFalse(glob_match("@bot:example.org", "@bot2:example.org"))
        self.assertFalse(glob_match("@bot:example.org", "x@bot:example.org"))

    def test_star_does_not_cross_the_end_anchor(self) -> None:
        self.assertTrue(glob_match("@bot*:example.org", "@bot:example.org"))
        self.assertTrue(glob_match("@bot*:example.org", "@bot-staging:example.org"))
        self.assertFalse(
            glob_match("@bot*:example.org", "@botimposter:example.org.evil.com")
        )

    def test_question_mark_matches_exactly_one_character(self) -> None:
        self.assertTrue(glob_match("@bot?:example.org", "@bota:example.org"))
        self.assertFalse(glob_match("@bot?:example.org", "@bot:example.org"))
        self.assertFalse(glob_match("@bot?:example.org", "@botab:example.org"))

    def test_trailing_and_leading_stars(self) -> None:
        self.assertTrue(glob_match("*", "@anyone:anywhere.example"))
        self.assertTrue(glob_match("*:example.org", "@anyone:example.org"))
        self.assertTrue(glob_match("@bot*", "@bot:example.org"))
        self.assertFalse(glob_match("*:example.org", "@anyone:example.com"))

    def test_star_matches_the_empty_run(self) -> None:
        self.assertTrue(glob_match("@bot*:example.org", "@bot:example.org"))
        self.assertTrue(glob_match("**", ""))

    def test_agrees_with_fnmatch_on_the_supported_grammar(self) -> None:
        """A differential check: the matcher is hand-rolled to keep the send
        path off the regex engine, so it has to mean what `fnmatch` means for
        every construct the grammar allows."""
        globs = [
            "@bot:example.org",
            "@bot*:example.org",
            "@*:example.org",
            "@bot?:example.org",
            "@*bot*:example.org",
            "@a*b*c:example.org",
            "*",
            "**",
            "@bot*",
            "?",
            "@?*?:example.org",
        ]
        values = [
            "",
            "@bot:example.org",
            "@bot2:example.org",
            "@bota:example.org",
            "@botimposter:example.org.evil.com",
            "@abc:example.org",
            "@axbxc:example.org",
            "@a:example.org",
            "x",
            "@bot:example.org.evil.com",
            "@bot:example.orgx",
        ]
        for glob in globs:
            for value in values:
                with self.subTest(glob=glob, value=value):
                    self.assertEqual(
                        glob_match(glob, value),
                        fnmatch.fnmatchcase(value, glob),
                    )

    def test_agrees_with_fnmatch_exhaustively_over_a_small_alphabet(self) -> None:
        """Hand-picked pairs are the weaker half of a differential test: the
        first version of this matcher agreed with `fnmatch` on every pair
        above and disagreed on 546 pairs here, because none of the values
        above contained a `*`. Matrix IDs can. Every string of length 0..4
        over an alphabet that includes both wildcards is checked on both
        sides - roughly 200k pairs, and fast enough to keep in the suite."""
        alphabet = "a*?:"
        strings = [""]
        for length in range(1, 5):
            strings.extend(
                "".join(parts) for parts in itertools.product(alphabet, repeat=length)
            )
        mismatches = [
            (glob, value)
            for glob in strings
            for value in strings
            if glob_match(glob, value) != fnmatch.fnmatchcase(value, glob)
        ]
        self.assertEqual(mismatches, [], f"{len(mismatches)} pairs disagree")

    def test_pathological_glob_resolves_quickly(self) -> None:
        """The reason the grammar is globs at all. Under the regex predecessor
        an operator could configure `@(a+)+:example.org`, which backtracks
        catastrophically against a long non-matching Matrix ID and blocks the
        reactor inside the pre-persist send path, where the module's
        fail-open handling cannot reach it."""
        glob = "@" + "*a" * 12 + ":example.org"
        value = "@" + "a" * 240 + ":example.com"
        started = time.monotonic()
        self.assertFalse(glob_match(glob, value))
        self.assertLess(time.monotonic() - started, 0.05)


class TestValidateGlob(unittest.TestCase):
    def test_accepts_matrix_id_shapes(self) -> None:
        for glob in (
            "@bot:example.org",
            "@bot*:example.org",
            "@bot?:example.org",
            "@pangea-bot_2:matrix.example.org:8448",
            "@a.b_c=d/e+f-g:example.org",
            "*",
        ):
            with self.subTest(glob=glob):
                validate_glob(glob)

    def test_rejects_regex_metacharacters(self) -> None:
        for glob in (
            r"@bot.*:example\.org",
            "@bot[0-9]:example.org",
            "@(bot|admin):example.org",
            "^@bot:example.org$",
            "@bot+:example.org",
        ):
            with self.subTest(glob=glob):
                # `+` is a legal Matrix localpart character, so it is the one
                # value in this list the grammar accepts; it is here to prove
                # the rejection is character-based and not shape-guessing.
                if glob == "@bot+:example.org":
                    validate_glob(glob)
                    continue
                with self.assertRaises(ExemptGlobError):
                    validate_glob(glob)

    def test_rejects_empty_and_whitespace(self) -> None:
        for glob in ("", "   ", "@bot:example.org ", " @bot:example.org", "@bot :x"):
            with self.subTest(glob=glob):
                with self.assertRaises(ExemptGlobError):
                    validate_glob(glob)

    def test_error_names_the_offending_character(self) -> None:
        with self.assertRaises(ExemptGlobError) as caught:
            validate_glob(r"@bot:example\.org")
        self.assertIn("\\\\", repr(str(caught.exception)))


class TestMatchesEverySender(unittest.TestCase):
    def test_detects_the_wholesale_exemption(self) -> None:
        self.assertTrue(matches_every_sender("*"))
        self.assertTrue(matches_every_sender("**"))

    def test_does_not_flag_a_scoped_glob(self) -> None:
        # Exempting a whole homeserver is broad, but it is not the same claim
        # and the operator asked for it by naming the server.
        self.assertFalse(matches_every_sender("@*:example.org"))
        self.assertFalse(matches_every_sender("?"))
        self.assertFalse(matches_every_sender(""))


class TestMigrationMessage(unittest.TestCase):
    def test_suggestion_turns_a_regex_into_the_glob_it_meant(self) -> None:
        self.assertEqual(suggest_glob(r"@bot.*:example\.org"), "@bot*:example.org")
        self.assertEqual(suggest_glob(r"@bot:.*"), "@bot:*")
        self.assertEqual(suggest_glob(r"^@bot:example\.org$"), "@bot:example.org")
        self.assertEqual(suggest_glob(r"@bot.:example\.org"), "@bot?:example.org")

    def test_error_lists_every_configured_value(self) -> None:
        message = legacy_key_error([r"@bot.*:example\.org", r"@admin.*:example\.org"])
        self.assertIn("exempt_user_id_globs", message)
        self.assertIn("@bot*:example.org", message)
        self.assertIn("@admin*:example.org", message)


if __name__ == "__main__":
    unittest.main()
