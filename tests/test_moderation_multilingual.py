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

from synapse_pangea_chat.moderation.profanity import contains_profanity
from synapse_pangea_chat.moderation.tier1_prefilter import REASON_PROFANITY, check_text
from synapse_pangea_chat.moderation.tier1_terms import matches_tier1

from .moderation_doubles import Tier2MatcherProbe

_CORPUS_PATH = Path(__file__).with_name("moderation_corpus.json")
# Regions only affect phone matching; profanity cases are region-independent.
_PHONE_REGIONS = ["US"]


def _corpus() -> Dict[str, Any]:
    return json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))


class TestCorpusIsNotEmpty(unittest.TestCase):
    """Every per-language loop below iterates a list from the corpus, so an
    empty list makes its assertions pass without running any of them. Emptying
    one language's cases is the shape a softening takes here - the suite stays
    green and the coverage is gone - so the lists are required to be
    non-empty before anything iterates them."""

    REQUIRED_CASES = ("profanities", "evasions", "negative_controls")

    def test_every_language_carries_cases_of_every_kind(self) -> None:
        languages = _corpus()["languages"]
        self.assertTrue(languages, "the corpus lists no languages at all")
        for lang in languages:
            for kind in self.REQUIRED_CASES:
                with self.subTest(lang=lang["lang_code"], kind=kind):
                    self.assertTrue(
                        lang.get(kind),
                        f"{lang['lang_name']} has no {kind}, so every "
                        f"assertion over them passes without running",
                    )


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

    def test_each_case_is_handled_by_the_tier_the_corpus_records(self) -> None:
        """Every case names the tier that handles it, and both halves are
        asserted.

        `tier: 1` must be rejected before send AND attributed to the profanity
        rule, not to an unrelated one (a phone match would misreport why).
        `tier: 2` must NOT be rejected before send - that is the whole point of
        demoting it - and must still be seen by the Tier 2 matcher. Written
        this way, moving a term between tiers cannot be done quietly: the
        recorded tier and the code have to agree, in both directions.
        """
        tier2 = Tier2MatcherProbe(self)
        for lang in _corpus()["languages"]:
            for case in lang["profanities"]:
                with self.subTest(
                    lang=lang["lang_code"], term=case["term"], tier=case["tier"]
                ):
                    verdict = check_text(case["sentence"], _PHONE_REGIONS)
                    if case["tier"] == 1:
                        self.assertEqual(verdict, REASON_PROFANITY)
                    else:
                        self.assertNotEqual(
                            verdict,
                            REASON_PROFANITY,
                            f"{case['term']!r} is recorded as Tier 2 but Tier 1 "
                            f"blocked it; see tier1_universal.json",
                        )
                        # Through the production Tier-2 handler: the claim is
                        # about what the system does with the message, not
                        # about what a helper returns.
                        self.assertTrue(
                            tier2.matcher_hit(case["sentence"]),
                            f"{case['term']!r} left Tier 1 and Tier 2 does not "
                            f"catch it either, so nothing catches it",
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

    def test_each_evasion_is_handled_by_the_tier_the_corpus_records(self) -> None:
        """Asserting the evasions against Tier 2 alone would have let every
        leetspeak spelling drop out of Tier 1 unnoticed - `n1gger`,
        `4rschloch`, `v1ttu` are all still classified as universal terms, and
        removing them from the blocking core kept a Tier-2-only assertion
        green. Same two-sided rule as the profanity cases."""
        for lang in _corpus()["languages"]:
            for case in lang["evasions"]:
                with self.subTest(
                    lang=lang["lang_code"], term=case["term"], tier=case["tier"]
                ):
                    if case["tier"] == 1:
                        self.assertTrue(
                            matches_tier1(case["term"]),
                            f"{case['term']!r} is recorded as caught before send "
                            f"and no longer is",
                        )
                    else:
                        self.assertFalse(matches_tier1(case["term"]))


class TestNegativeControls(unittest.TestCase):
    """Benign learner messages are never blocked. Equal weight to the above.

    Blocking is Tier 1's, and `test_moderation_tier1_terms.py` asserts every
    control against it. What is asserted here is the Tier 2 matcher, which
    does not block but does decide what the LLM is asked about. A control the
    recall-oriented matcher still flags has to say so in the corpus and say
    why, and the assertion runs in both directions - an unmarked control must
    pass it, and a marked one must really fail it - so the marker cannot be
    sprinkled over a regression.
    """

    def test_benign_sentences_are_not_flagged_by_the_tier2_matcher(self) -> None:
        for lang in _corpus()["languages"]:
            for case in lang["negative_controls"]:
                marked = case.get("tier2_matcher") == "flags"
                with self.subTest(lang=lang["lang_code"], sentence=case["sentence"]):
                    if marked:
                        self.assertTrue(
                            case.get("tier2_note", "").strip(),
                            "a control excused from this assertion must say why",
                        )
                        self.assertTrue(
                            contains_profanity(case["sentence"]),
                            f"{case['sentence']!r} is recorded as flagged by the "
                            f"Tier 2 matcher but is not; drop the marker",
                        )
                        continue
                    self.assertFalse(
                        contains_profanity(case["sentence"]),
                        f"false positive in {lang['lang_name']}: "
                        f"{case['sentence']!r} — {case['why_benign']}",
                    )

    def test_no_benign_sentence_is_blocked_before_send(self) -> None:
        """The one that actually matters: Tier 1 rejects pre-persist."""
        for lang in _corpus()["languages"]:
            for case in lang["negative_controls"]:
                with self.subTest(lang=lang["lang_code"], sentence=case["sentence"]):
                    self.assertFalse(
                        matches_tier1(case["sentence"]),
                        f"Tier 1 blocked a benign {lang['lang_name']} sentence: "
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
        for bucket in ("drop_term", "allowlist", "restored_controls"):
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


class TestTheMatcherScanIsBounded(unittest.TestCase):
    """The Tier-2 matcher now runs on every Tier-2 message, inside a worker on
    the single-threaded reactor, so what it costs on attacker-chosen input is
    a property of the homeserver and not of the matcher.

    Asserted as an INVARIANT rather than as a time: every rejoining the scan
    produces is a prefix of some needle. That is what makes the scan
    proportional to the message instead of to the wordlist - without it, a
    10,000-character message of short tokens cost about 88 ms of reactor
    time, per message, because every starting token ran a window out to the
    longest needle whatever the message said.
    """

    def test_every_rejoining_could_still_become_a_needle(self) -> None:
        from synapse_pangea_chat.moderation.profanity import (
            _needle_prefixes,
            _short_token_runs,
        )

        prefixes = _needle_prefixes()
        tokens = ["a", "b", "c", "d", "e", "f", "u", "c", "k", "x", "y"] * 40
        runs = _short_token_runs(tokens)
        self.assertTrue(runs, "the scan produced nothing to check")
        for run in runs:
            with self.subTest(run=run):
                self.assertIn(run, prefixes)
