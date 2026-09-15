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
    TermRecord,
    _is_letter_of_an_alphabet,
    matches_phrase,
    matches_tier1,
    needle,
    needle_floor,
    split_spans,
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

_REASONS_FOR_TIER1 = {"reviewed", "collision_adjudicated"}
_REVIEW_BASES = {
    "not_a_word_in_any_orthography",
    "curator_attested",
    "native_review",
}
_REASONS_FOR_TIER2 = {
    "benign_homograph",
    "benign_sense_in_own_language",
    "mention_not_use",
    "register",
    "romanization",
    "awaiting_review",
    "no_word_boundary",
    "too_short",
    "unadjudicated",
}


def _corpus() -> Dict[str, Any]:
    return json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))


def _policy() -> Dict[str, Any]:
    return json.loads(
        (
            Path(__file__).parents[1]
            / "synapse_pangea_chat"
            / "moderation"
            / "tier1_universal.json"
        ).read_text(encoding="utf-8")
    )["_policy"]


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


class TestTheTier2FlagMarkerIsARecordedList(unittest.TestCase):
    """A negative control may say "Tier 2 still flags this" - Tier 2 does not
    block, so that is a note, not an excuse. But the set of controls allowed
    to say it is recorded in `curation_decisions`, so adding one is an edit to
    the decisions record rather than a field quietly dropped onto a case."""

    TIER2_LAXNESS = {
        "term_is_on_the_tier2_list",
        "tier2_folds_diacritics",
        "tier2_rejoins_fragments",
        "tier2_substring_matches_cjk",
        "tier2_joins_a_phrase_across_any_separator",
    }

    def test_each_marker_names_a_known_tier2_mechanism(self) -> None:
        """Not free text. Tier 2 is lax in five specific ways, and a control
        excused from its assertion has to point at one of them - so excusing
        a regression means claiming a mechanism that is not what happened."""
        for case in _controls():
            if case.get("tier2_matcher") != "flags":
                continue
            with self.subTest(sentence=case["sentence"]):
                self.assertIn(case.get("tier2_reason"), self.TIER2_LAXNESS)

    def test_only_the_recorded_sentences_carry_the_marker(self) -> None:
        corpus = _corpus()
        allowed = set(corpus["curation_decisions"]["tier2_known_flags"])
        marked = {
            case["sentence"]
            for case in _controls()
            if case.get("tier2_matcher") == "flags"
        }
        self.assertEqual(
            marked,
            allowed,
            "a control was excused from the Tier 2 assertion without being "
            "recorded in curation_decisions.tier2_known_flags",
        )


class TestTheCorpusAndTheClassificationAgree(unittest.TestCase):
    """Two recorded descriptions of the same decision have to match.

    Removing `n1gger` from the loaded core and changing only its corpus tier
    to 2 kept the whole suite green, while the classification still called it
    universal. The corpus records what happens; the classification records
    what was decided; this asserts they say the same thing.
    """

    def test_each_evasion_case_agrees_with_its_classification(self) -> None:
        classified = {entry["term"]: entry for entry in universal_terms()}
        checked = 0
        for lang in _corpus()["languages"]:
            for case in lang["evasions"]:
                entry = classified.get(case["term"])
                if entry is None:
                    continue  # an evasion spelling that is not itself listed
                checked += 1
                with self.subTest(term=case["term"]):
                    self.assertEqual(
                        case["tier"] == 1,
                        bool(entry["tier1"]),
                        f"the corpus and tier1_universal.json disagree about "
                        f"{case['term']!r}",
                    )
        self.assertGreater(checked, 30, "most evasion spellings are listed terms")


class TestTier1StillBlocksProfanity(unittest.TestCase):
    """The other half of the bargain. Permissive must not mean inert: if the
    universal set were emptied, every test above would pass and Tier 1 would
    block nothing at all."""

    MUST_BLOCK = [
        ("you are a fucking idiot", "en"),
        ("motherfucker", "en"),
        ("what a cunt", "en"),
        ("du 4rschloch", "de"),
        ("h1jo de puta", "es"),
        ("v1ttu", "fi"),
        ("бл9дь", "ru"),
        ("n1gger", "en"),
    ]

    def test_everything_tier1_stopped_blocking_is_caught_by_tier2(self) -> None:
        """The whole bargain, asserted over the whole corpus.

        Tier 1 now blocks plain words in English only, because English is the
        only language anyone on this change can attest. That is a large
        recall loss at the blocking tier, and it is acceptable only because
        Tier 2 catches every one of them - which is what this asserts, case
        by case, rather than assuming.
        """
        moved = [
            case
            for lang in _corpus()["languages"]
            for kind in ("profanities", "evasions")
            for case in lang[kind]
            if case["tier"] == 2
        ]
        self.assertGreater(len(moved), 100, "the corpus still has cases to check")
        for case in moved:
            text = case.get("sentence", case["term"])
            with self.subTest(term=case["term"]):
                self.assertFalse(matches_tier1(text))
                self.assertTrue(
                    contains_profanity(text),
                    f"{case['term']!r} left Tier 1 and Tier 2 does not catch it",
                )

    def test_the_evasion_spellings_are_still_rejected_before_send(self) -> None:
        """Obfuscated spellings are the one thing Tier 1 can block in every
        language without a reviewer, because a digit inside a word is not an
        orthography anywhere. If that stopped working, the blocking tier
        would be English-only in practice as well as in principle."""
        caught = [
            case
            for lang in _corpus()["languages"]
            for case in lang["evasions"]
            if case["tier"] == 1
        ]
        self.assertGreaterEqual(len(caught), 20)
        for case in caught:
            with self.subTest(term=case["term"]):
                self.assertTrue(matches_tier1(case["term"]))

    def test_real_profanity_is_still_rejected_before_send(self) -> None:
        for text, lang in self.MUST_BLOCK:
            with self.subTest(lang=lang, text=text):
                self.assertEqual(
                    check_text(text, _PHONE_REGIONS),
                    REASON_PROFANITY,
                    f"Tier 1 no longer catches {text!r}",
                )


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
        self.assertGreater(len(universal), 20)
        self.assertTrue(
            any(entry["review"]["basis"] == "curator_attested" for entry in universal),
            "an obfuscation-only core would block `n1gger` and not `nigger`",
        )


class TestPromotionNeedsPositiveEvidence(unittest.TestCase):
    """The rule that closed the class three rounds of review kept reopening.

    A term used to reach Tier 1 by not being objected to: no measurable
    frequency elsewhere, therefore universal. Adversarial review then found
    `faggot` (a British dish), `hoer` (one who hoes), Greek `kariola` (a bed),
    Turkish `kaltak` (a saddle frame), Portuguese `viado` (striped fabric),
    Korean `byeongsin` (the year 丙申) and `Chuj` (a Mayan language) among
    terms that scored zero in every language the gate could measure. Absence
    of a score is not evidence, so promotion now requires some.
    """

    def test_every_promoted_term_records_its_evidence(self) -> None:
        for entry in universal_terms():
            if not entry["tier1"]:
                continue
            with self.subTest(term=entry["term"]):
                review = entry.get("review")
                self.assertIsNotNone(review, "promoted with no review record")
                assert review is not None
                self.assertIn(review["basis"], _REVIEW_BASES)
                note = review.get("note", "")
                assert isinstance(note, str)
                self.assertTrue(note.strip())

    def test_the_obfuscation_basis_is_verified_not_asserted(self) -> None:
        """`not_a_word_in_any_orthography` means the needle carries a digit,
        and that is checked in both directions: the basis cannot be claimed
        for an ordinary word, and an ordinary word cannot be promoted by
        claiming it."""
        for entry in universal_terms():
            if not entry["tier1"]:
                continue
            has_digit = any(char.isdigit() for char in needle(entry["term"]))
            claims = entry["review"]["basis"] == "not_a_word_in_any_orthography"
            with self.subTest(term=entry["term"]):
                self.assertEqual(claims, has_digit)

    def test_curator_attestation_is_limited_to_the_recorded_words(self) -> None:
        """The curator speaks one of the thirty languages. The words they
        attest are listed in the policy, so a new one is an edit a reviewer
        sees next to the sentence saying how little that attestation covers."""
        policy = _policy()
        self.assertEqual(policy["curator_languages"], ["en"])
        attested = {
            needle(entry["term"])
            for entry in universal_terms()
            if entry["tier1"] and entry["review"]["basis"] == "curator_attested"
        }
        self.assertEqual(attested, set(policy["curator_attested"]))
        for word in attested:
            with self.subTest(word=word):
                self.assertTrue(word.isascii() and word.isalpha())

    def test_a_native_review_names_a_reviewer(self) -> None:
        for entry in universal_terms():
            if not entry["tier1"] or entry["review"]["basis"] != "native_review":
                continue
            with self.subTest(term=entry["term"]):
                self.assertNotIn(
                    "curator",
                    str(entry["review"].get("by", "")),
                    "the curator is not a native reviewer of anything but English",
                )
                self.assertTrue(str(entry["review"].get("date", "")).strip())

    def test_the_languages_tier1_carries_terms_for_are_recorded(self) -> None:
        """Adding a language to Tier 1 is a review task. Recording the list
        makes it one somebody signs for, rather than a side effect."""
        carried = sorted(
            {
                lang
                for entry in universal_terms()
                if entry["tier1"]
                for lang in entry["langs"]
            }
        )
        self.assertEqual(carried, _policy()["tier1_languages"])


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

    def test_universal_needles_clear_the_length_floor_for_their_script(
        self,
    ) -> None:
        """A short needle is an ordinary word somewhere among thirty
        languages, and whole-token matching does not save it: Romanian `cu` is
        a whole token too.

        Per SCRIPT. Applying the two-character ideograph floor to everything
        would let a Latin `cu` through the guard that exists for it.
        """
        floors = {4: [], 3: [], 2: []}  # type: Dict[int, List[str]]
        for entry in universal_terms():
            if not entry["tier1"]:
                continue
            stored = needle(entry["term"])
            with self.subTest(term=entry["term"]):
                self.assertGreaterEqual(len(stored), needle_floor(stored))
            floors[needle_floor(stored)].append(stored)
        self.assertEqual(
            needle_floor("cu"), 4, "Latin needles are held to four characters"
        )
        self.assertEqual(needle_floor("хуй"), 3)
        self.assertEqual(needle_floor("씨발"), 2)
        self.assertTrue(
            floors[4] and floors[3],
            "the alphabet floors are exercised by real terms",
        )
        self.assertEqual(
            floors[2],
            [],
            "nothing written in a script without word spacing is in Tier 1",
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
            (case["sentence"], split_spans(case["sentence"].casefold()))
            for case in _controls()
        ]
        for entry in universal_terms():
            if not entry["tier1"]:
                continue
            term_needle = needle(entry["term"])
            for sentence, spans in controls:
                if entry["match"] == "phrase":
                    hit = matches_phrase(spans, {term_needle})
                else:
                    hit = term_needle in [span.text for span in spans]
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
        self.assertFalse(matches_tier1("Fuckery is a word in some dictionaries"))
        self.assertFalse(matches_tier1("Мы изучаем хуйский язык."))
        self.assertTrue(matches_tier1("fuck off"))

    def test_a_multi_word_term_does_not_match_inside_a_word(self) -> None:
        self.assertFalse(matches_tier1("Pedestrian crossing is ahead"))
        self.assertFalse(contains_profanity("Pedestrian crossing is ahead"))

    def test_a_live_phrase_term_does_not_match_inside_a_word(self) -> None:
        """The sentence above stopped exercising Tier 1's phrase rule the
        moment `pe de` was demoted out of Tier 1, so the rule is also tested
        against a phrase that is actually in the universal set today."""
        phrases = [
            entry
            for entry in universal_terms()
            if entry["tier1"] and entry["match"] == "phrase"
        ]
        self.assertTrue(phrases, "no phrase term left to exercise the rule")
        for entry in phrases:
            stored = needle(entry["term"])
            words = entry["term"].split()
            with self.subTest(term=entry["term"]):
                self.assertFalse(
                    matches_tier1(f"za{stored}za"),
                    "a phrase matched inside a single word",
                )
                if all(word.isalpha() for word in words):
                    self.assertTrue(
                        matches_tier1(entry["term"]),
                        "and it still matches when it is written as its own words",
                    )

    def test_indic_vowel_signs_are_letters_not_diacritics(self) -> None:
        """`रोड` (road) and `रंडी` (a slur) differ only in their marks, so
        stripping the marks made an ordinary sentence about traffic match.

        Asserted on the normalizer as well as on the sentence: `रंडी` is Tier
        2's now, so the sentence alone would stay green even if Tier 1 went
        back to discarding every mark.
        """
        self.assertFalse(matches_tier1("रोड पर ट्रैफिक है"))
        self.assertFalse(contains_profanity("रोड पर ट्रैफिक है"))
        self.assertNotEqual(needle("रोड"), needle("रंडी"))
        self.assertEqual(needle("मादरचोद"), "मादरचोद")
        self.assertEqual(needle("খানকির"), "খানকির")

    def test_diacritics_do_not_merge_two_different_words(self) -> None:
        """Asserted on the normalizer, so it does not depend on which terms
        happen to be promoted: `pica` and `píča` must stay two words."""
        self.assertFalse(matches_tier1("Pica je jednotka v typografii"))
        self.assertNotEqual(needle("pica"), needle("píča"))
        self.assertEqual(needle("fücking"), "fücking")

    def test_a_short_needle_does_not_match_an_ordinary_word(self) -> None:
        self.assertFalse(matches_tier1("Merg cu tine"))

    def test_fragments_are_not_rejoined_across_a_space(self) -> None:
        """A space is a real word boundary, and in the scripts where one
        character is a whole syllable it separates ordinary words.

        `김 씨 발이 아파요` is "Mr Kim's foot hurts" and `민수 씨 발 아파요?`
        is "Minsu, does your foot hurt?" - 씨 is an honorific and 발 a foot.
        Rejoining across a space is therefore restricted to single characters
        of an alphabet, where one character really is one letter. Spaced-out
        evasions in Hangul, kana and the abugidas are Tier 2's.
        """
        for text in ("김 씨 발이 아파요", "민수 씨 발 아파요?", "개 새 끼"):
            with self.subTest(text=text):
                self.assertFalse(matches_tier1(text))
        for text in ("f u c k you", "n 1 g g e r", "c.u.n.t"):
            with self.subTest(text=text):
                self.assertTrue(matches_tier1(text))

    def test_only_letters_of_an_alphabet_are_treated_as_fragments(self) -> None:
        """Asserted on the rule itself, because no term written in one of
        those scripts is in Tier 1 today - there is no reviewer for one - so
        an end-to-end assertion would pass with the rule deleted, and would
        keep passing until the day somebody adds a Korean term back.

        Punctuation is not proof of an evasion either: `민수 씨,발 아파요?` is
        the same ordinary sentence with a comma in it.
        """
        for char in ("f", "z", "х", "α"):
            with self.subTest(char=char):
                self.assertTrue(_is_letter_of_an_alphabet(char))
        for char in ("씨", "발", "ね", "妈", "र", "ة"):
            with self.subTest(char=char):
                self.assertFalse(_is_letter_of_an_alphabet(char))

    def test_tier1_never_matches_inside_a_word(self) -> None:
        """Tier 1 does not substring-match at all, which is why Chinese and
        Japanese terms are Tier 2's.

        A script with no word spacing offers no boundary to respect, so a
        needle fires inside ordinary text however the message is split:
        `操你妈` is spread across 体操 / 你 / 妈妈 in "can your mother do this
        gymnastics routine", with no punctuation anywhere near it. Deciding
        where a Chinese word ends needs segmentation, which is Tier 2's to do.
        """
        for text in ("这套体操你妈妈会做吗？", "做完体操，你妈妈来接你。", "操你妈"):
            with self.subTest(text=text):
                self.assertFalse(matches_tier1(text))
        self.assertTrue(
            contains_profanity("操你妈"), "and Tier 2 still catches the term itself"
        )
        self.assertEqual(
            [
                entry["term"]
                for entry in universal_terms()
                if entry["tier1"] and entry["match"] == "substring"
            ],
            [],
            "no term may be promoted into Tier 1 as a substring needle",
        )

    def test_a_phrase_does_not_form_across_a_sentence_boundary(self) -> None:
        """`Tôi đang tập viết chữ pê. Đê là chữ tiếp theo` is "I am
        practising the letter P. Đ is the next one". A run of words is a
        phrase only within one sentence."""
        self.assertFalse(
            matches_tier1("Tôi đang tập viết chữ pê. Đê là chữ tiếp theo.")
        )

    def test_letters_split_apart_are_still_caught(self) -> None:
        for text in ("f u c k you", "f.u.c.k", "fuuuuck"):
            with self.subTest(text=text):
                self.assertTrue(matches_tier1(text))


if __name__ == "__main__":
    unittest.main()
