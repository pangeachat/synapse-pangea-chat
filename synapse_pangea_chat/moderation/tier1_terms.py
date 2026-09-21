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
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import (
    Any,
    Deque,
    Dict,
    FrozenSet,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    TypedDict,
)

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
        """Nothing but whitespace, and no LINE BREAK, so the two tokens are
        consecutive words of one line.

        A newline is a stronger boundary than a space, not a weaker one, and
        the rendered text of a table row, a list or a `<br>` is full of them:
        `<ol><li>v</li><li>1</li><li>t</li><li>t</li><li>u</li></ol>` is a
        numbered list a learner can write, and rejoining down it produced
        `M_FORBIDDEN`. A word written with spaces inside it stays on one line.
        """
        return (
            self.gap != ""
            and self.gap.isspace()
            and "\n" not in self.gap
            and "\r" not in self.gap
        )


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
    #: it. `basis` is one of `not_a_word_or_an_identifier`,
    #: `curator_attested`, `model_review` or `native_review`.
    review: Dict[str, str]
    collisions: Dict[str, float]
    adjudication: Dict[str, object]
    #: The three-family model vote, on every term that went through it -
    #: promoted or not, so nobody re-reviews a term blind. Data for the gate;
    #: the matcher never reads it.
    model_review: Dict[str, Any]
    #: The ordinary word, and its language, that made the vocabulary sweep
    #: demote a term the vote had promoted.
    sweep_collision: Dict[str, str]
    #: A benign reading named after the vote had promoted the term, which
    #: demotes it the same way one named in the vote does: the language, the
    #: meaning, who named it, and the sentence that reproduced.
    later_benign_reading: Dict[str, str]


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


#: A compact whole token: `n1gger` matches the token `n1gger`.
BUCKET_TOKEN = "token"
#: A run of consecutive whole words of one sentence: `bhen ch0d`.
BUCKET_PHRASE = "phrase"
#: A word typed with its letters spaced apart and NOTHING else: `p 1 c a`
#: matches `p 1 c a` and never the token `p1ca`.
BUCKET_SPLIT = "split"
#: Every needle a spelled-out run may equal - the union of the two above.
#: Precomputed, because the rejoining scan caches its longest needle on the
#: set it is given and building that union per message would defeat it.
BUCKET_REJOIN = "rejoin"


def match_bucket(entry: TermRecord) -> Optional[str]:
    """Which bucket this term's needle belongs in, or None for a term Tier 1
    does not carry at all.

    **A term written with its letters separated is a SPELLED-OUT evasion, and
    may only ever match a spelled-out run.** `needle()` strips every
    separator - it has to, because `f*ck` and `k.u.r.v.a` are one word with
    its letters split - and that is exactly what defeated the intent of a
    term authored as `p 1 c a`. Stored as `p1ca` in the compact bucket, a
    needle written to catch a run of spaced letters became a blocklist entry
    for any whole token, and `Room P1CA is down the hall`, `Model P1CA-200
    ships Friday` and a gamertag were all rejected before persist.

    The irony is worth recording so it is not repeated: `pica` was demoted
    for ambiguity - a typographic unit in Slovak, an ordinary word in
    Catalan, Portuguese and Spanish - and its leetspeak form carried the
    identical collision straight back in.

    `substring` is None rather than a bucket: in a script with no word
    spacing there is no boundary to respect, so a needle fires inside
    ordinary text - 操你妈 is spread across 体操 / 你 / 妈妈 in "can your
    mother do this gymnastics routine". Establishing the boundary needs
    segmentation Tier 1 cannot afford, so Chinese and Japanese terms are
    Tier 2's. The classification records that per term; this is the backstop.
    """
    if entry["match"] == "substring":
        return None
    if entry["match"] == "phrase":
        return BUCKET_PHRASE
    if any(char.isspace() for char in entry["term"]):
        return BUCKET_SPLIT
    return BUCKET_TOKEN


@lru_cache(maxsize=1)
def _universal() -> Dict[str, Set[str]]:
    """The universal set, split by how each needle may be matched.

    See `match_bucket` for what separates the three, and why a needle in
    `split` must never reach `token`.
    """
    data = json.loads(_UNIVERSAL_PATH.read_text(encoding="utf-8"))
    buckets: Dict[str, Set[str]] = {
        BUCKET_TOKEN: set(),
        BUCKET_PHRASE: set(),
        BUCKET_SPLIT: set(),
    }
    for entry in data["terms"]:
        if not entry.get("tier1"):
            continue
        bucket = match_bucket(entry)
        if bucket is None:
            continue
        stored = needle(entry["term"])
        # Belt and braces: the floor is a property of the matcher, so it is
        # applied here as well as asserted over the data. A needle that slips
        # below it is not loaded, rather than quietly blocking a whole class
        # of ordinary short words until someone runs the tests.
        if len(stored) < needle_floor(stored):
            continue
        buckets[bucket].add(stored)
    for bucket_terms in buckets.values():
        bucket_terms.discard("")
    buckets[BUCKET_REJOIN] = buckets[BUCKET_TOKEN] | buckets[BUCKET_SPLIT]
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

    # Only the compact bucket may match a whole token. A needle authored as a
    # spaced spelling is in `split`, and is reachable only through the
    # rejoining scan below.
    if any(span.text in terms[BUCKET_TOKEN] for span in spans):
        return True
    if matches_phrase(spans, terms[BUCKET_PHRASE]):
        return True
    return _matches_split_word(spans, terms[BUCKET_REJOIN])


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
    """One word typed with its letters spaced apart (`p 1 c a`, `n 1 g g e r`).

    Two conditions, and a rejoining needs both.

    **Across WHITESPACE ALONE.** A space is a word boundary in every language
    that has them, so a word written with spaces inside it was never a run of
    separate words - while punctuation between two pieces is how identifiers
    are written. Rejoining across punctuation blocked `Download the cert from
    p3.der and install it.` (the standard DER certificate extension), `The API
    path is /api/v1/ado for now.` and `Our domain is p1.ca and it works.` -
    198 blocking forms in all, across `.`, `-` and `/`. Tier 1 rejects before
    persist, so each of those is a learner silenced mid-sentence.

    **And the run must carry a DIGIT.** It is the evidence standard the
    promotion policy applies to a term (`not_a_word_or_an_identifier`),
    applied to the rejoining: no orthography of the thirty supported languages
    puts a digit inside a word, and no identifier is written as spaced single
    letters either, so a SPACED-OUT run carrying one can only be a
    deliberately obfuscated spelling. Without it, `The letters are C U N T.`
    is a spelling lesson - a first-week classroom exercise here - and Tier 1
    cannot tell it from the evasion it is structurally identical to.

    Both halves of that are load-bearing, and only the RUN has both. A
    COMPACT token carrying a digit is exactly the shape of an identifier -
    `P1CA`, `P3DER` - which is why the digit alone never licensed a compact
    needle and why five terms left Tier 1 when it was checked.

    Everything the two conditions exclude is Tier 2's, which reads the message
    in context: `c u n t`, `f.u.c.k`, `cu.nt`, `Press C,U,N,T to continue.`

    Bounded as well as narrowed: a run is capped at the longest needle, so
    this is linear in the message. Before the cap, a 60 KB message of spaced
    single characters took **4.6 seconds** inline in the send path.
    """
    limit = _longest_needle(frozenset(needles))
    if not limit:
        return False
    run: Deque[Span] = deque()
    run_length = 0
    run_digits = 0
    for span in spans:
        if _joinable(span, run[-1].text if run else ""):
            run.append(span)
            run_length += len(span.text)
            run_digits += _digit_count(span.text)
        else:
            run.clear()
            run_length = 0
            run_digits = 0
            # A single character can still START a run: the first fragment
            # has no preceding whitespace to qualify it, and dropping it
            # meant `p 1 c a` at the head of a message rejoined nothing.
            if _alphanumeric(span.text) and len(span.text) == 1:
                run.append(span)
                run_length = 1
                run_digits = _digit_count(span.text)
        while run_length > limit and run:
            dropped = run.popleft()
            run_length -= len(dropped.text)
            run_digits -= _digit_count(dropped.text)
        if run_digits and len(run) >= 2 and _run_hits(run, needles):
            return True
    return False


def _run_hits(run: Sequence[Span], needles: Set[str]) -> bool:
    """Does any SUFFIX of this run equal a needle?

    Every suffix, not only the whole run: the run grows from wherever the last
    unjoinable token was, so one short word in front of the evasion defeated a
    whole-run test - `p 1 c a` blocked and `Say a p 1 c a now` did not. The run
    is capped at the longest needle, so this is a bounded number of joins per
    token and the scan stays linear in the message.
    """
    joined = ""
    for span in reversed(run):
        joined = span.text + joined
        if len(joined) >= 2 and joined in needles:
            return True
    return False


def _digit_count(token: str) -> int:
    return sum(1 for char in token if char.isdigit())


@lru_cache(maxsize=4)
def _longest_needle(needles: FrozenSet[str]) -> int:
    """The longest thing a rejoining could equal. A run past it cannot match,
    so this is what keeps the scan linear."""
    return max((len(needle_text) for needle_text in needles), default=0)


def _joinable(span: Span, previous: str) -> bool:
    """Single characters of an alphabet, separated by whitespace and nothing
    else.

    In Hangul, kana and the abugidas one character is a word, so rejoining
    two of them makes a slur out of `민수 씨 발 아파요?` ("Minsu, does your
    foot hurt?"). And a gap with anything but whitespace in it is how an
    identifier is written, not how a word is broken: see
    `_matches_split_word`.

    `previous` is the run's last fragment, not the run: reading it as a list
    meant rebuilding that list on every token, which is what made a long
    message quadratic.
    """
    return (
        _alphanumeric(span.text)
        and span.after_space_only
        and len(span.text) == 1
        and (not previous or len(previous) == 1)
    )


def _alphanumeric(token: str) -> bool:
    """A character of an alphabet, or a digit.

    Digits count, and they have to: the only rejoining Tier 1 acts on is one
    that contains a digit, so a matcher that refused to carry digits through a
    run could never produce one.
    """
    return bool(token) and all(_is_letter_of_an_alphabet(char) for char in token)
