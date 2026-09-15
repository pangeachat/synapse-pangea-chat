"""The Tier 1 blocking core: the universal terms, and how they are matched.

Tier 1 runs inline in the send path and rejects before persist, so every
false positive is an innocent learner silenced mid-sentence. The product
decision this module implements (D1) is:

> Tier 1 blocks only terms with no benign homograph in any supported
> language. Everything ambiguous is Tier 2's, which sees the message in
> context.

Two consequences run through the whole module.

**Matching is lossless.** Every normalization that maps two distinct strings
onto one is a way for an ordinary word to become a needle, and with 30
languages in one union the chance that some language owns the collision is
not small. Diacritic stripping alone merged Slovak `pica` (a typographic
unit) with Czech `píča`, and merged Hindi `रोड` (road) with `रंडी` by
discarding the vowel signs that tell them apart. Tier 1 therefore casefolds,
drops invisible characters, and collapses runs of three or more identical
characters - and nothing else. No diacritic stripping, no leetspeak or
homoglyph folding. Those belong to `profanity.py`, the recall-oriented
matcher Tier 2 uses, where a false positive costs an LLM call rather than a
learner's sentence.

**Matching is by whole token.** A needle matched by prefix blocks every word
that begins with it: `cu` inside Romanian `curva`, `хуй` inside the token
`хуйский`. Inflections are carried as their own list entries instead, which
is how the wordlist is already written.

There is **no language identification here, and no language parameter**.
LID is unreliable at chat length and structurally broken on the
code-switched text our learners write, and a wrong guess could only ever
make Tier 1 block something it should not. The universal set is applied as
one union to every message; there is nothing for a language guess to select.

The classification itself - which terms are universal, and the measured
evidence for each - is data, in `tier1_universal.json`, gated by
`tests/test_moderation_tier1_terms.py`.
"""

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, List, NamedTuple, Sequence, Set, TypedDict

_UNIVERSAL_PATH = Path(__file__).with_name("tier1_universal.json")


class Span(NamedTuple):
    """A token, and the separator that stood between it and the one before."""

    text: str
    gap: str

    @property
    def after_space(self) -> bool:
        """A space is a real word boundary; punctuation inside a word is not."""
        return any(char.isspace() for char in self.gap)

    @property
    def after_space_only(self) -> bool:
        """Nothing but whitespace, so the two tokens are consecutive words of
        one sentence - not two words either side of a full stop."""
        return self.gap != "" and self.gap.isspace()


class TermRecord(TypedDict, total=False):
    """One row of `tier1_universal.json`: a term, the decision about it, and
    the measured evidence the decision was taken on."""

    term: str
    langs: List[str]
    match: str
    needle: str
    tier1: bool
    reason: str
    note: str
    #: Present only on a promoted term: the positive evidence for promoting
    #: it. `basis` is one of `not_a_word_in_any_orthography`,
    #: `curator_attested` or `native_review`.
    review: Dict[str, str]
    collisions: Dict[str, float]
    adjudication: Dict[str, object]


# Invisible characters carry no meaning; an evader puts them inside a word.
_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]")

# Shortest needle Tier 1 will carry, by script. A short needle is an ordinary
# word somewhere among thirty languages - Romanian `cu`, Malay `cip` - and
# whole-token matching does not save it, because those are whole tokens too.
# The floor is script-relative because the information per character is: one
# Hangul syllable or one ideograph is a morpheme, four Latin letters are not.
MIN_LATIN_NEEDLE_LEN = 4
MIN_ALPHABET_NEEDLE_LEN = 3
MIN_IDEOGRAPH_NEEDLE_LEN = 2

# A run of tokens this short is what a word typed with its letters split
# apart leaves behind (`f u c k`, `f*ck`).
_FRAGMENT_LEN = 2


@lru_cache(maxsize=4096)
def _is_word_char(char: str) -> bool:
    """Marks count as word characters, unlike `\\w`.

    Cached per character: this runs once per character of every message, and
    `unicodedata.category` is not free.

    Indic vowel signs are category Mc/Mn but they are letters in every sense
    that matters here. `\\w` does not match them, so a separator-based split
    cut `रोड` into `र` and `ड` - which is what pushed the previous matcher
    into stripping marks instead, and that is what collapsed `रोड` onto the
    slur `रंडी`.
    """
    return char.isalnum() or char == "_" or unicodedata.category(char).startswith("M")


def _fold(text: str) -> str:
    """Casefold, drop invisible characters, normalize to NFC. Lossless in the
    sense that matters: no two distinct words are merged."""
    return unicodedata.normalize("NFC", _INVISIBLE.sub("", text).casefold())


def _collapse_repeats(text: str) -> str:
    """`fuuuuck` -> `fuck`. Three or more only: collapsing pairs would merge
    real words (`nigger` onto the country name `Niger`)."""
    return re.sub(r"(.)\1{2,}", r"\1", text)


def split_spans(text: str) -> List["Span"]:
    """Tokenize, keeping what SEPARATED each token from the one before it.

    The distinction is load-bearing. A word split by punctuation - `f*ck`,
    `k.u.r.v.a` - was never two words, so rejoining it recovers the original.
    A space is a real word boundary in every language that has them, and in
    Korean or Vietnamese the words on either side of one are routinely a
    syllable long: rejoining across a space turned `김 씨 발이 아파요` ("Mr
    Kim's foot hurts") into a slur. So the two gaps are treated differently
    in `_matches_split_word`, and that needs to be recorded here.
    """
    spans: List[Span] = []
    current: List[str] = []
    gap: List[str] = []
    for char in text:
        if _is_word_char(char):
            current.append(char)
            continue
        if current:
            spans.append(Span(_collapse_repeats("".join(current)), "".join(gap)))
            current = []
            gap = []
        gap.append(char)
    if current:
        spans.append(Span(_collapse_repeats("".join(current)), "".join(gap)))
    return spans


def split_tokens(text: str) -> List[str]:
    """The words of a message, with marks kept. Shared with `profanity.py`
    so the two tiers cannot drift into two different ideas of where a word
    ends."""
    return [span.text for span in split_spans(text)]


def needle(term: str) -> str:
    """The stored form of a wordlist term: folded, separators removed. A term
    written `k.u.r.v.a` or `f*ck` is one word with its letters split, so the
    separators are not part of it."""
    return _collapse_repeats("".join(split_tokens(_fold(term))))


def needle_floor(needle_text: str) -> int:
    """The shortest this needle is allowed to be, given the script it is
    written in."""
    codes = [ord(char) for char in needle_text if char.isalnum()]
    if not codes:
        return MIN_LATIN_NEEDLE_LEN
    if all(code < 0x250 for code in codes):
        return MIN_LATIN_NEEDLE_LEN
    if any(
        0x1100 <= code <= 0x11FF
        or 0x3040 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
        or 0x3130 <= code <= 0x318F
        for code in codes
    ):
        return MIN_IDEOGRAPH_NEEDLE_LEN
    return MIN_ALPHABET_NEEDLE_LEN


@lru_cache(maxsize=1)
def _universal() -> Dict[str, Set[str]]:
    """The universal set, split by how each needle may be matched.

    `token` needles match a whole token and `phrase` needles a run of
    consecutive whole words. There is no substring bucket: see below.
    """
    data = json.loads(_UNIVERSAL_PATH.read_text(encoding="utf-8"))
    buckets: Dict[str, Set[str]] = {"token": set(), "phrase": set()}
    for entry in data["terms"]:
        if not entry.get("tier1"):
            continue
        if entry["match"] == "substring":
            # Tier 1 does not substring-match. In a script with no word
            # spacing there is no boundary to respect, so a needle fires
            # inside ordinary text: 操你妈 is spread across 体操 / 你 / 妈妈
            # in "can your mother do this gymnastics routine". Establishing
            # the boundary needs segmentation Tier 1 cannot afford, so
            # Chinese and Japanese terms are Tier 2's. The classification
            # records that decision per term; this is the backstop.
            continue
        stored = needle(entry["term"])
        # Belt and braces: the floor is a property of the matcher, so it is
        # applied here as well as asserted over the data. A needle that slips
        # below it is not loaded, rather than quietly blocking a whole class
        # of ordinary short words until someone runs the tests.
        if len(stored) < needle_floor(stored):
            continue
        buckets[entry["match"]].add(stored)
    for bucket in buckets.values():
        bucket.discard("")
    return buckets


def universal_terms() -> List[TermRecord]:
    """Every classified term with its recorded evidence, for the gate and for
    Tier 2's observability annotations."""
    data = json.loads(_UNIVERSAL_PATH.read_text(encoding="utf-8"))
    return list(data["terms"])


def matches_tier1(text: str) -> bool:
    """True when the text contains a universal term.

    Takes no language and consults none.
    """
    if not text:
        return False
    terms = _universal()
    spans = split_spans(_fold(text))
    if not spans:
        return False

    if any(span.text in terms["token"] for span in spans):
        return True
    if matches_phrase(spans, terms["phrase"]):
        return True
    return _matches_split_word(spans, terms["token"])


def matches_phrase(
    spans: Sequence[Span], phrases: Set[str], within_sentence: bool = True
) -> bool:
    """A multi-word term matches a run of consecutive WHOLE words of ONE
    sentence.

    Substring-matching a phrase against the message with its spaces removed
    is what let the Vietnamese term `pe de` fire inside `Pedestrian`. Letting
    a run cross any separator is what let `Tôi đang tập viết chữ pê. Đê là
    chữ tiếp theo` ("I am practising the letter P. Đ is the next one") form
    one, across the full stop. So a run continues only over whitespace.

    `within_sentence` is what Tier 1 needs and Tier 2 does not: Tier 2 is the
    recall-oriented matcher, where a false positive costs an LLM call rather
    than a learner's sentence, and it has to keep catching evasions written
    with punctuation inside them (`đ.ị.t mẹ`).

    Indexed rather than sliced, and abandoned as soon as it cannot become a
    phrase: `tokens[start:]` copied the rest of the message on every
    iteration, which cost 412 ms on a 60 KB message, inline in the send path.
    """
    if not phrases:
        return False
    prefixes = _prefixes(frozenset(phrases))
    count = len(spans)
    for start in range(count):
        joined = ""
        for index in range(start, count):
            if index > start and within_sentence and not spans[index].after_space_only:
                break
            joined += spans[index].text
            if joined not in prefixes:
                break
            if joined in phrases:
                return True
    return False


@lru_cache(maxsize=4)
def _prefixes(phrases: FrozenSet[str]) -> Set[str]:
    """Every prefix of every phrase, so a scan abandons a run as soon as it
    cannot become one. Without it, each token started a scan that ran to the
    longest phrase's length - milliseconds per message, in the send path."""
    return {
        phrase[:length] for phrase in phrases for length in range(1, len(phrase) + 1)
    }


def _is_letter_of_an_alphabet(char: str) -> bool:
    """True for Latin, Greek and Cyrillic, where one character is one letter.

    False for Hangul, kana, ideographs and the Indic abugidas, where one
    character is a syllable or a whole morpheme - and therefore an ordinary
    word, not the fragment of one.
    """
    return ord(char) < 0x0590


def _matches_split_word(spans: Sequence[Span], needles: Set[str]) -> bool:
    """One word typed with its letters split apart (`f u c k`, `f*ck`).

    Two rules, because the two kinds of gap mean different things:

    - across PUNCTUATION, fragments of one or two characters rejoin, which is
      what recovers `f*ck` and `k.u.r.v.a`;
    - across a SPACE, only single characters of an ALPHABET rejoin. A space
      is a real word boundary, and in the scripts where one character is a
      whole syllable it separates ordinary words: `민수 씨 발 아파요?` is
      "Minsu, does your foot hurt?", and rejoining its syllables makes a
      slur. Spaced-out evasions in those scripts are Tier 2's.

    The rejoined run must equal a needle outright. Searching the whole
    separatorless message instead is how an ordinary sentence picks up a
    needle it never contained.
    """
    run: List[str] = []
    for span in spans:
        if _joinable(span, run):
            run.append(span.text)
            continue
        if len(run) >= 2 and "".join(run) in needles:
            return True
        run = [span.text] if len(span.text) <= _FRAGMENT_LEN else []
    if len(run) >= 2 and "".join(run) in needles:
        return True
    # One split point, with a long remainder: `f*cking` -> `f` + `cking`.
    # Punctuation gaps only, for the same reason as above.
    return any(
        not right.after_space
        and _alphabetic(left.text)
        and _alphabetic(right.text)
        and (len(left.text) <= _FRAGMENT_LEN or len(right.text) <= _FRAGMENT_LEN)
        and left.text + right.text in needles
        for left, right in zip(spans, spans[1:])
    )


def _joinable(span: Span, run: List[str]) -> bool:
    """Only letters of an alphabet ever rejoin.

    In Hangul, kana and the abugidas one character is a word, and punctuation
    between two of them is ordinary punctuation: `민수 씨,발 아파요?` is still
    "Minsu, does your foot hurt?" with a comma in it. So the alphabet rule
    applies to both kinds of gap, and the length allowance only to the
    punctuation one.
    """
    if not _alphabetic(span.text):
        return False
    if not span.after_space:
        return len(span.text) <= _FRAGMENT_LEN
    return len(span.text) == 1 and (not run or len(run[-1]) == 1)


def _alphabetic(token: str) -> bool:
    return bool(token) and all(_is_letter_of_an_alphabet(char) for char in token)
