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
from typing import Dict, List, Sequence, Set, TypedDict

_UNIVERSAL_PATH = Path(__file__).with_name("tier1_universal.json")


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
    collisions: Dict[str, float]
    adjudication: Dict[str, object]


# Invisible characters carry no meaning; an evader puts them inside a word.
_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]")

# Shortest needle Tier 1 will carry in a space-delimited script. A two or
# three letter word is an ordinary word somewhere among thirty languages -
# Romanian `cu`, Malay `cip` - and exact matching does not save it.
MIN_TOKEN_NEEDLE_LEN = 4

# Scripts without word spacing cannot be tokenized, so their needles are
# substring-matched and the length floor is what keeps that safe.
MIN_SUBSTRING_NEEDLE_LEN = 2

# A run of tokens this short is what a word typed with its letters split
# apart leaves behind (`f u c k`, `f*ck`).
_FRAGMENT_LEN = 2


def _is_word_char(char: str) -> bool:
    """Marks count as word characters, unlike `\\w`.

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


def split_tokens(text: str) -> List[str]:
    """The words of a message, with marks kept. Shared with `profanity.py`
    so the two tiers cannot drift into two different ideas of where a word
    ends."""
    tokens: List[str] = []
    current: List[str] = []
    for char in text:
        if _is_word_char(char):
            current.append(char)
            continue
        if current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return [_collapse_repeats(token) for token in tokens]


def needle(term: str) -> str:
    """The stored form of a wordlist term: folded, separators removed. A term
    written `k.u.r.v.a` or `f*ck` is one word with its letters split, so the
    separators are not part of it."""
    return _collapse_repeats("".join(split_tokens(_fold(term))))


@lru_cache(maxsize=1)
def _universal() -> Dict[str, Set[str]]:
    """The universal set, split by how each needle may be matched.

    `token` needles match a whole token; `substring` needles come from
    scripts without word spacing and match anywhere; `phrase` needles match a
    run of consecutive whole tokens.
    """
    data = json.loads(_UNIVERSAL_PATH.read_text(encoding="utf-8"))
    buckets: Dict[str, Set[str]] = {"token": set(), "substring": set(), "phrase": set()}
    for entry in data["terms"]:
        if not entry.get("tier1"):
            continue
        buckets[entry["match"]].add(needle(entry["term"]))
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
    tokens = split_tokens(_fold(text))
    if not tokens:
        return False

    if any(token in terms["token"] for token in tokens):
        return True
    if matches_phrase(tokens, terms["phrase"]):
        return True
    if _matches_substring(tokens, terms["substring"]):
        return True
    return _matches_split_word(tokens, terms["token"])


def matches_phrase(tokens: Sequence[str], phrases: Set[str]) -> bool:
    """A multi-word term matches a run of consecutive WHOLE tokens.

    Substring-matching a phrase against the message with its spaces removed
    is what let the Vietnamese term `pe de` fire inside `Pedestrian`.
    """
    if not phrases:
        return False
    longest = max(len(phrase) for phrase in phrases)
    for start in range(len(tokens)):
        joined = ""
        for token in tokens[start:]:
            joined += token
            if len(joined) > longest:
                break
            if joined in phrases:
                return True
    return False


def _matches_substring(tokens: Sequence[str], substrings: Set[str]) -> bool:
    """Chinese and Japanese have no word spacing, so their terms can only be
    matched inside a token. The length floor and the per-term collision gate
    are what keep that from firing on ordinary compounds."""
    if not substrings:
        return False
    joined = "".join(tokens)
    return any(term in joined for term in substrings)


def _matches_split_word(tokens: Sequence[str], needles: Set[str]) -> bool:
    """One word typed with its letters split apart (`f u c k`, `f*ck`).

    Only runs of very short fragments are rejoined, and the rejoined run must
    equal a needle outright - searching the whole separatorless message
    instead is how an ordinary sentence picks up a needle it never contained.
    """
    run: List[str] = []
    for token in tokens:
        if len(token) <= _FRAGMENT_LEN:
            run.append(token)
            continue
        if len(run) >= 2 and "".join(run) in needles:
            return True
        run = []
    if len(run) >= 2 and "".join(run) in needles:
        return True
    return any(
        (len(left) <= _FRAGMENT_LEN or len(right) <= _FRAGMENT_LEN)
        and left + right in needles
        for left, right in zip(tokens, tokens[1:])
    )
