"""The Tier 1 blocking core: what may block a message before it is sent.

Tier 1 runs inline in the send path and rejects pre-persist, so a false
positive is an innocent learner silenced mid-sentence. The product decision
(D1) is therefore that Tier 1 carries only terms with no benign homograph in
any supported language, and everything ambiguous is left to Tier 2, which sees
the message in context. These tests are what stops the universal set drifting
back the other way.
"""

import importlib.util
import json
import unittest
from pathlib import Path
from typing import Any, Dict, List

from synapse_pangea_chat.moderation.profanity import contains_profanity
from synapse_pangea_chat.moderation.tier1_prefilter import REASON_PROFANITY, check_text
from synapse_pangea_chat.moderation.tier1_terms import (
    MIN_SUBSTRING_NEEDLE_LEN,
    TermRecord,
    matches_phrase,
    matches_tier1,
    needle,
    split_tokens,
    universal_terms,
)

_CORPUS_PATH = Path(__file__).with_name("moderation_corpus.json")
_WORDLIST_PATH = (
    Path(__file__).parents[1]
    / "synapse_pangea_chat"
    / "moderation"
    / "profanity_wordlists.json"
)
_PHONE_REGIONS = ["US"]

# The thirty languages Tier 1's union is applied to, and which of them
# wordfreq carries a "large" list for. Serbian is measured against `sh`.
_LANGS = [
    "ar",
    "bn",
    "ca",
    "cs",
    "da",
    "de",
    "el",
    "en",
    "es",
    "fi",
    "fr",
    "hi",
    "hu",
    "id",
    "it",
    "ja",
    "ko",
    "ms",
    "nl",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sr",
    "tr",
    "uk",
    "ur",
    "vi",
    "zh",
]
_LARGE_LIST = {
    "ar",
    "bn",
    "ca",
    "cs",
    "de",
    "en",
    "es",
    "fi",
    "fr",
    "it",
    "ja",
    "nl",
    "pl",
    "pt",
    "ru",
    "uk",
    "zh",
}
_WORDFREQ_LANG = {"sr": "sh"}

# Deliberately duplicated from the data file rather than read out of it: a
# threshold a test reads from the thing it is testing can be raised until the
# test passes. Both have to be edited, and the edit shows up in review.
_LARGE_LIST_THRESHOLD = 3.0
_SMALL_LIST_THRESHOLD = 0.01

_REASONS_FOR_TIER1 = {"no_collision_measured", "collision_adjudicated"}
_REASONS_FOR_TIER2 = {
    "benign_homograph",
    "benign_sense_in_own_language",
    "mention_not_use",
    "register",
    "romanization",
    "too_short",
    "unadjudicated",
}


def _corpus() -> Dict[str, Any]:
    return json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))


def _controls() -> List[Dict[str, str]]:
    cases = []
    for lang in _corpus()["languages"]:
        for case in lang["negative_controls"]:
            cases.append({**case, "lang": lang["lang_name"]})
    return cases


class TestNegativeControlsSurviveTier1(unittest.TestCase):
    """Not one benign sentence in the corpus may be blocked before send."""

    def test_no_negative_control_is_blocked(self) -> None:
        for case in _controls():
            with self.subTest(lang=case["lang"], sentence=case["sentence"]):
                self.assertNotEqual(
                    check_text(case["sentence"], _PHONE_REGIONS),
                    REASON_PROFANITY,
                    f"Tier 1 blocked a benign {case['lang']} sentence: "
                    f"{case['sentence']!r} - {case['why_benign']}",
                )


class TestRestoredControlsCannotBeRetiredAgain(unittest.TestCase):
    """Eight controls were once deleted because the matcher failed them.

    That is the one move this suite must make impossible: a control is
    evidence about the world, not about the code, so a matcher that fails one
    is a broken matcher. The corpus records each restored sentence, and this
    asserts every one of them is still a live control under some language and
    still passes. Deleting the record fails the test too, because the record
    is what the test iterates.
    """

    def test_every_restored_control_is_live_and_passes(self) -> None:
        restored = _corpus()["curation_decisions"]["restored_controls"]
        self.assertEqual(
            len(restored), 8, "the eight retired controls are all accounted for"
        )
        live = {case["sentence"] for case in _controls()}
        for sentence, reason in restored.items():
            with self.subTest(sentence=sentence):
                self.assertIn(
                    sentence, live, "restored control is not listed under any language"
                )
                self.assertTrue(reason.strip(), "restoration carries no reason")
                self.assertNotEqual(
                    check_text(sentence, _PHONE_REGIONS), REASON_PROFANITY
                )


class TestTier1StillBlocksProfanity(unittest.TestCase):
    """The other half of the bargain. Permissive must not mean inert: if the
    universal set were emptied, every test above would pass and Tier 1 would
    block nothing at all."""

    MUST_BLOCK = [
        ("you are a fucking idiot", "en"),
        ("motherfucker", "en"),
        ("du bist ein Arschloch", "de"),
        ("va te faire enculé", "fr"),
        ("kurwa, co za dzień", "pl"),
        ("иди на хуй", "ru"),
        ("씨발 진짜", "ko"),
        ("你他妈的", "zh"),
    ]

    def test_real_profanity_is_still_rejected_before_send(self) -> None:
        for text, lang in self.MUST_BLOCK:
            with self.subTest(lang=lang, text=text):
                self.assertEqual(
                    check_text(text, _PHONE_REGIONS),
                    REASON_PROFANITY,
                    f"Tier 1 no longer catches {text!r}",
                )


if __name__ == "__main__":
    unittest.main()


class TestEveryTermIsClassified(unittest.TestCase):
    """The wordlist and the classification are one table with two files, and
    a term missing from either side is how a decision gets made by accident."""

    def test_the_two_files_describe_the_same_terms(self) -> None:
        listed = {
            term
            for words in json.loads(_WORDLIST_PATH.read_text(encoding="utf-8")).values()
            for term in words
        }
        classified = {entry["term"] for entry in universal_terms()}
        self.assertEqual(
            listed - classified,
            set(),
            "wordlist terms with no recorded Tier 1 decision",
        )
        self.assertEqual(
            classified - listed,
            set(),
            "classified terms that are no longer in the wordlist",
        )

    def test_every_decision_states_a_reason_from_the_agreed_vocabulary(self) -> None:
        for entry in universal_terms():
            with self.subTest(term=entry["term"]):
                self.assertTrue(entry.get("note", "").strip(), "no reason recorded")
                expected = _REASONS_FOR_TIER1 if entry["tier1"] else _REASONS_FOR_TIER2
                self.assertIn(entry["reason"], expected)

    def test_the_universal_set_is_not_empty(self) -> None:
        """An empty core passes every false-positive test in this file. It is
        also the state D1 forbids: Tier 1 exists, so it must carry something."""
        universal = [e for e in universal_terms() if e["tier1"]]
        self.assertGreater(len(universal), 100)
        covered = {lang for entry in universal for lang in entry["langs"]}
        self.assertGreaterEqual(
            len(covered), 25, "the universal set has to span our languages"
        )


class TestTheCollisionGate(unittest.TestCase):
    """A term may be promoted into Tier 1 only over a recorded, adjudicated
    collision profile. This is the test that fails when someone promotes a
    term that collides with ordinary vocabulary in another language."""

    def test_every_collision_on_a_universal_term_is_adjudicated(self) -> None:
        for entry in universal_terms():
            if not entry["tier1"] or not entry["collisions"]:
                continue
            with self.subTest(term=entry["term"]):
                adjudication = entry.get("adjudication")
                self.assertIsNotNone(
                    adjudication,
                    f"{entry['term']!r} is Tier 1 and collides with "
                    f"{sorted(entry['collisions'])}, with nothing recorded",
                )
                assert adjudication is not None
                self.assertEqual(
                    adjudication["outcome"],
                    "no_benign_sense",
                    "a benign sense anywhere forces the term out of Tier 1",
                )
                covered = adjudication["langs"]
                assert isinstance(covered, list)
                self.assertEqual(
                    sorted(covered),
                    sorted(entry["collisions"]),
                    "the adjudication has to cover every flagged language",
                )
                note = adjudication.get("note", "")
                assert isinstance(note, str)
                self.assertTrue(note.strip())

    def test_a_demoted_term_carries_no_adjudication_to_wave_it_through(self) -> None:
        for entry in universal_terms():
            if entry["tier1"]:
                continue
            with self.subTest(term=entry["term"]):
                self.assertNotIn(
                    "adjudication",
                    entry,
                    "a Tier 2 term needs no adjudication; one here would be a "
                    "half-finished promotion",
                )

    def test_universal_needles_clear_the_length_floor(self) -> None:
        """A short needle is an ordinary word somewhere among thirty
        languages, and whole-token matching does not save it: Romanian `cu` is
        a whole token too."""
        for entry in universal_terms():
            if not entry["tier1"]:
                continue
            with self.subTest(term=entry["term"]):
                self.assertGreaterEqual(
                    len(needle(entry["term"])), MIN_SUBSTRING_NEEDLE_LEN
                )

    def test_recorded_needles_match_what_the_matcher_computes(self) -> None:
        for entry in universal_terms():
            with self.subTest(term=entry["term"]):
                self.assertEqual(entry["needle"], needle(entry["term"]))

    def test_no_universal_term_occurs_in_a_negative_control(self) -> None:
        """The gate's other side: the classification is checked against the
        corpus, not only against a frequency list. Naming the term as well as
        the sentence is the point - it says which row of the table to fix."""
        controls = [
            (case["sentence"], split_tokens(case["sentence"].casefold()))
            for case in _controls()
        ]
        for entry in universal_terms():
            if not entry["tier1"]:
                continue
            term_needle = needle(entry["term"])
            for sentence, tokens in controls:
                if entry["match"] == "substring":
                    hit = term_needle in "".join(tokens)
                elif entry["match"] == "phrase":
                    hit = matches_phrase(tokens, {term_needle})
                else:
                    hit = term_needle in tokens
                with self.subTest(term=entry["term"], sentence=sentence):
                    self.assertFalse(
                        hit,
                        f"{entry['term']!r} is a Tier 1 term and occurs in the "
                        f"benign sentence {sentence!r}",
                    )


class TestTheFrequencyTripwire(unittest.TestCase):
    """The recorded collision numbers are re-measured here, so a term cannot
    be promoted with a collision profile that was never taken or has gone
    stale.

    wordfreq is a tripwire and never a decider: a high score forces a written
    adjudication, and a low score approves nothing. Automatic demotion on a
    high score was rejected because `fuck` scores 4.85 in Danish and 4.29 in
    German as a borrowed swear - it would strip the most universal terms out
    of Tier 1 while leaving genuinely ambiguous ones in.
    """

    def setUp(self) -> None:
        """Missing data is a failure, never a skip. A gate that quietly does
        not run is the shape every softening in this repo has taken."""
        if importlib.util.find_spec("wordfreq") is None:
            self.fail(
                "the collision gate needs wordfreq[cjk]; install the dev "
                "extras with `pip install -e '.[dev]'`. Skipping it instead "
                "would leave the wordlist ungated"
            )

    @staticmethod
    def _measure(entry: TermRecord) -> Dict[str, float]:
        import warnings

        from wordfreq import zipf_frequency

        forms = {entry["needle"]}
        if entry["match"] == "phrase":
            forms.add(entry["term"].casefold())
        hits = {}
        for lang in _LANGS:
            if lang in entry["langs"]:
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                best = max(
                    zipf_frequency(form, _WORDFREQ_LANG.get(lang, lang))
                    for form in forms
                    if form
                )
            floor = (
                _LARGE_LIST_THRESHOLD if lang in _LARGE_LIST else _SMALL_LIST_THRESHOLD
            )
            if best >= floor:
                hits[lang] = round(best, 2)
        return hits

    def test_the_recorded_collisions_are_what_wordfreq_reports(self) -> None:
        for entry in universal_terms():
            with self.subTest(term=entry["term"]):
                self.assertEqual(
                    entry["collisions"],
                    self._measure(entry),
                    f"the recorded collision profile for {entry['term']!r} is "
                    f"not what wordfreq reports; re-measure before changing "
                    f"any tier",
                )

    def test_the_policy_records_the_thresholds_this_test_applies(self) -> None:
        policy = json.loads(
            (
                Path(__file__).parents[1]
                / "synapse_pangea_chat"
                / "moderation"
                / "tier1_universal.json"
            ).read_text(encoding="utf-8")
        )["_policy"]["measured_with"]
        self.assertEqual(policy["thresholds"]["large_list"], _LARGE_LIST_THRESHOLD)
        self.assertEqual(sorted(policy["languages"]), sorted(_LANGS))
        self.assertEqual(sorted(policy["large_list_languages"]), sorted(_LARGE_LIST))


class TestTier1TakesNoLanguage(unittest.TestCase):
    """D2: language identification may only ever make Tier 1 more permissive.

    The way that is guaranteed is that there is nothing to identify with -
    neither entry point accepts a language, so no caller can select behaviour
    by guessing one. This asserts the signatures, which is the part a future
    change would have to break first.
    """

    def test_neither_entry_point_accepts_a_language(self) -> None:
        import inspect

        self.assertEqual(list(inspect.signature(matches_tier1).parameters), ["text"])
        self.assertEqual(
            list(inspect.signature(check_text).parameters), ["text", "phone_regions"]
        )


class TestTheMatchingRules(unittest.TestCase):
    """Each collision class that produced a measured false positive, named by
    its mechanism, so a regression says which rule came back."""

    def test_a_needle_does_not_match_a_word_that_starts_with_it(self) -> None:
        self.assertFalse(matches_tier1("Мы изучаем хуйский язык."))
        self.assertTrue(matches_tier1("иди на хуй"))

    def test_a_multi_word_term_does_not_match_inside_a_word(self) -> None:
        self.assertFalse(matches_tier1("Pedestrian crossing is ahead"))
        self.assertFalse(contains_profanity("Pedestrian crossing is ahead"))

    def test_indic_vowel_signs_are_letters_not_diacritics(self) -> None:
        """`रोड` (road) and `रंडी` (a slur) differ only in their marks, so
        stripping the marks made an ordinary sentence about traffic match."""
        self.assertFalse(matches_tier1("रोड पर ट्रैफिक है"))
        self.assertFalse(contains_profanity("रोड पर ट्रैफिक है"))

    def test_diacritics_do_not_merge_two_different_words(self) -> None:
        self.assertFalse(matches_tier1("Pica je jednotka v typografii"))
        self.assertTrue(matches_tier1("Nadával jej do piče."))

    def test_a_short_needle_does_not_match_an_ordinary_word(self) -> None:
        self.assertFalse(matches_tier1("Merg cu tine"))

    def test_letters_split_apart_are_still_caught(self) -> None:
        for text in ("f u c k you", "f.u.c.k", "fuuuuck"):
            with self.subTest(text=text):
                self.assertTrue(matches_tier1(text))
