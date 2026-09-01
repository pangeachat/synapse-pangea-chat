"""Multilingual + evasion coverage for the Tier 1 profanity check.

Data-driven from `moderation_corpus.json`: 30 languages of real curse words,
obfuscated spellings that must still be caught, and benign sentences that must
NOT be blocked. The corpus was produced by an adversarial (red-team) pass and
then curated — `curation_decisions` inside the fixture records every term
removed from the blocking tier and every retired control, with reasons.

Why the negative controls carry equal weight: Tier 1 blocks a message before
it is sent, so a false positive silences an innocent learner mid-conversation.
A run that catches more profanity by also blocking a benign sentence is a
regression, not an improvement.

Each case is a subTest so one failure names the exact language and string
rather than collapsing the suite.
"""

import json
import unittest
from pathlib import Path
from typing import Any, Dict

from synapse_pangea_chat.moderation.tier1_prefilter import (
    REASON_PROFANITY,
    check_text,
    contains_profanity,
)

_CORPUS_PATH = Path(__file__).with_name("moderation_corpus.json")
# Regions only affect phone matching; profanity cases are region-independent.
_PHONE_REGIONS = ["US"]


def _corpus() -> Dict[str, Any]:
    return json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))


class TestMultilingualProfanity(unittest.TestCase):
    """Every language's real curse words are caught in a natural sentence."""

    def test_profanity_is_caught_in_every_language(self) -> None:
        for lang in _corpus()["languages"]:
            for case in lang["profanities"]:
                with self.subTest(
                    lang=lang["lang_code"], term=case["term"], kind=case["severity"]
                ):
                    self.assertTrue(
                        contains_profanity(case["sentence"]),
                        f"missed {case['severity']} {case['term']!r} in "
                        f"{lang['lang_name']}: {case['sentence']!r}",
                    )

    def test_profanity_reports_the_profanity_reason(self) -> None:
        """A caught message must be attributed to the profanity rule, not to
        an unrelated one (a phone or address match would misreport why)."""
        for lang in _corpus()["languages"]:
            for case in lang["profanities"][:2]:
                with self.subTest(lang=lang["lang_code"], term=case["term"]):
                    self.assertEqual(
                        check_text(case["sentence"], _PHONE_REGIONS),
                        REASON_PROFANITY,
                    )


class TestEvasions(unittest.TestCase):
    """Obfuscated spellings still resolve to their base term."""

    def test_obfuscated_spellings_are_caught(self) -> None:
        for lang in _corpus()["languages"]:
            for case in lang["evasions"]:
                with self.subTest(
                    lang=lang["lang_code"],
                    term=case["term"],
                    technique=case["technique"],
                ):
                    self.assertTrue(
                        contains_profanity(case["term"]),
                        f"{case['technique']} evasion slipped through in "
                        f"{lang['lang_name']}: {case['term']!r} "
                        f"(base {case['base_term']!r})",
                    )


class TestNegativeControls(unittest.TestCase):
    """Benign learner messages are never blocked. Equal weight to the above."""

    def test_benign_sentences_are_not_blocked(self) -> None:
        for lang in _corpus()["languages"]:
            for case in lang["negative_controls"]:
                with self.subTest(lang=lang["lang_code"], sentence=case["sentence"]):
                    self.assertFalse(
                        contains_profanity(case["sentence"]),
                        f"false positive in {lang['lang_name']}: "
                        f"{case['sentence']!r} — {case['why_benign']}",
                    )


class TestEvasionTechniques(unittest.TestCase):
    """Technique-level cases, independent of the generated corpus, so a
    normalizer regression is named by its mechanism rather than by a language.
    Each pairs an evasion with the benign string it must not drag in."""

    CAUGHT = [
        ("fuck you", "plain"),
        ("f u c k you", "letters spaced apart"),
        ("f.u.c.k", "letters split by punctuation"),
        ("f*ck this", "single punctuation split"),
        ("fuuuuck", "elongated repeat"),
        ("f@ggot", "leetspeak substitution"),
        ("fuck​ing", "zero-width space inside the word"),
        ("¡Joder, déjame en paz!", "leading inverted punctuation"),
        ("FUCK YOU", "uppercase"),
        ("fücking", "added diacritic"),
    ]

    NOT_CAUGHT = [
        ("The village of Scunthorpe is in England.", "innocent substring"),
        ("The cockpit was cramped.", "innocent prefix"),
        ("Please complete the class assignment.", "innocent prefix"),
        ("We discussed Niger in geography class.", "country name"),
        ("Homo sapiens is our species.", "scientific term"),
        ("Le salopette bleue est petite.", "French clothing word"),
        ("Kankeronderzoek redt levens.", "Dutch cancer research"),
        ("Pedał roweru jest zepsuty.", "Polish bicycle pedal"),
        ("Babi itu berada di ladang ternakan.", "Malay for pig"),
        ("মাগুরা একটি জেলার নাম।", "Bengali district name"),
    ]

    def test_evasions_are_caught(self) -> None:
        for text, technique in self.CAUGHT:
            with self.subTest(technique=technique, text=text):
                self.assertTrue(contains_profanity(text))

    def test_benign_lookalikes_are_not_caught(self) -> None:
        for text, why in self.NOT_CAUGHT:
            with self.subTest(why=why, text=text):
                self.assertFalse(contains_profanity(text))


class TestCorpusIntegrity(unittest.TestCase):
    """The corpus itself must stay self-consistent: a sentence cannot be both
    a required catch and a required pass, and every curation decision must
    carry a reason a reviewer can weigh."""

    def test_no_sentence_is_both_profanity_and_control(self) -> None:
        for lang in _corpus()["languages"]:
            profane = {c["sentence"] for c in lang["profanities"]}
            benign = {c["sentence"] for c in lang["negative_controls"]}
            with self.subTest(lang=lang["lang_code"]):
                self.assertEqual(profane & benign, set())

    def test_every_curation_decision_states_a_reason(self) -> None:
        decisions = _corpus()["curation_decisions"]
        for bucket in ("drop_term", "allowlist", "drop_control"):
            for key, reason in decisions[bucket].items():
                with self.subTest(bucket=bucket, key=key):
                    self.assertTrue(reason.strip())

    def test_corpus_covers_every_full_support_language(self) -> None:
        """The languages we sell as fully supported must all be represented;
        a language with no cases is a blind spot the suite cannot see."""
        full_support = {
            "ca",
            "de",
            "en",
            "es",
            "fr",
            "it",
            "ja",
            "ko",
            "pt",
            "ru",
            "vi",
            "zh",
        }
        covered = {lang["lang_code"] for lang in _corpus()["languages"]}
        self.assertEqual(full_support - covered, set())


if __name__ == "__main__":
    unittest.main()
