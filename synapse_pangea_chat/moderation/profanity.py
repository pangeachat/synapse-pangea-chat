"""Multilingual profanity matching for the Tier 1 pre-filter.

The English `better-profanity` wordlist cannot see a Spanish, Russian, or
Japanese curse word, so Tier 1 was blind in 23 of our 24 full-support
languages. This replaces it with a curated per-language wordlist matched
through an evasion-resistant normalizer.

Model-free and deterministic, so Tier 1 stays sub-millisecond:

1. Normalize: casefold, strip combining diacritics (NFKD), fold a small set
   of leetspeak/homoglyph characters onto letters, drop separators an evader
   inserts between letters, and collapse long character repeats. The same
   normalizer runs over each wordlist term at load, so `f.u.c.k`, `fück`, and
   `fuuuck` all reduce to the same needle.
2. Match. Space-delimited scripts match a needle only as a whole token (so an
   innocent substring like the town "Scunthorpe" is not blocked), plus a
   plus a rejoin of consecutive single-letter fragments to catch
   letters-spaced-apart evasions. Scripts without word spacing (CJK) and
   multi-word phrases match by substring, which is unavoidable there and safe
   because those needles are long and specific.

Wordlist: `profanity_wordlists.json`, one array per language code, from a
native-speaker-reviewed corpus.
"""

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Set

# The lossless primitives live with the Tier 1 core, and both tiers use the
# same ones: two definitions of "where does a word end" is how the tiers drift
# apart and how a fix to one silently misses the other.
from synapse_pangea_chat.moderation.tier1_terms import matches_phrase, split_spans

_WORDLIST_PATH = Path(__file__).with_name("profanity_wordlists.json")

# Leetspeak / homoglyph folding, applied after diacritic stripping. Small and
# high-signal — an over-broad map turns benign text into false hits.
_SUBSTITUTIONS = {
    "0": "o",
    "1": "i",
    "3": "e",
    "4": "a",
    "5": "s",
    "7": "t",
    "8": "b",
    "@": "a",
    "$": "s",
    "!": "i",
    "|": "i",
    # Cyrillic / Greek look-alikes onto Latin (homoglyph attacks).
    "а": "a",
    "е": "e",
    "о": "o",
    "р": "p",
    "с": "c",
    "х": "x",
    "у": "y",
    "к": "k",
    "т": "t",
    "ѕ": "s",
    "і": "i",
    "ο": "o",
    "α": "a",
    "ε": "e",
}

# Invisible characters carry no meaning and are removed outright (not treated
# as separators), so `f<zero-width-space>uck` is one token again.
_INVISIBLE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]")

# A needle also matches a token that merely STARTS with it, so inflections and
# compounds are caught ("fuck" -> "fucking", "puta" -> "putas", "merda" ->
# "merdas"). Bounded, so a needle cannot swallow an unrelated long word.
_MAX_INFLECTION_SUFFIX = 4

# Needles shorter than this match only as a whole token. A 2-3 letter needle
# allowed to match by prefix would fire on ordinary words in every language
# (Portuguese "cu" would block Romanian "curva", Spanish "cuando", ...).
_MIN_PREFIX_NEEDLE_LEN = 4

# Benign tokens that would otherwise match by prefix or exact form. The
# Scunthorpe problem: blocking these is worse than missing the profanity they
# resemble, so they win. (Mid-word matches like "Scunthorpe" never reach here —
# boundary needles only match at a token start.)
_ALLOWLIST = {
    # English Scunthorpe-class collisions
    "cockpit",
    "cockpits",
    "cockburn",
    "cocktail",
    "cocktails",
    "cockroach",
    "cockroaches",
    "assessment",
    "assessments",
    "assess",
    "assessed",
    "asset",
    "assets",
    "assign",
    "assignment",
    "assignments",
    "assist",
    "assistant",
    "associate",
    "association",
    "assume",
    "assumption",
    "assure",
    "class",
    "classic",
    "classroom",
    "classes",
    "analysis",
    "analyst",
    "scunthorpe",
    "penistone",
    "shiitake",
    "shitake",
    "document",
    "documents",
    "niger",
    "nigeria",
    "nigerian",
    # Collisions found by running the multilingual corpus's own negative
    # controls through this matcher (see the moderation instructions doc).
    "salopette",
    "salopettes",
    "faszen",
    "lonteng",
    "kankeronderzoek",
    "curvatura",
    "picasso",
    "cabra",
    "homo",
    "sapiens",
    "মাগুরা",
}

# Scripts without word spacing: substring-matched.
_CJK_LANGS = {"zh", "ja", "yue"}


def _strip_diacritics(text: str) -> str:
    """Drop combining marks from LATIN, GREEK and CYRILLIC bases only, then
    recompose.

    Stripping every mark in every script is lossy in the scripts where marks
    are not decoration. Devanagari vowel signs are letters: dropping them
    turned Hindi `रोड` (road) into `रड`, which is also what `रंडी` (a slur)
    became, so an ordinary sentence about traffic matched. The same applies to
    Bengali, Arabic and Hangul. Where a mark IS decoration - Czech `č`,
    Spanish `ó`, French `è` - stripping it is what defeats the
    diacritic-stripping evasion, so that much is kept.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    kept: List[str] = []
    base_takes_marks = False
    for char in decomposed:
        if unicodedata.category(char).startswith("M"):
            if base_takes_marks:
                kept.append(char)
            continue
        base_takes_marks = not _decorated_script(char)
        kept.append(char)
    return unicodedata.normalize("NFC", "".join(kept))


def _decorated_script(char: str) -> bool:
    """True for the scripts where a combining mark is an accent on a letter
    rather than a letter in its own right.

    Latin, Greek and Cyrillic, and Arabic: the harakat are optional
    vocalization, normally left out, so `يا شَرْمُوطَة` has to reduce to the
    unvocalized form the wordlist carries. Devanagari, Bengali and Hangul are
    the other case - there the marks are letters, and dropping them merged
    `रोड` with `रंडी`."""
    return (
        ord(char) < 0x0590
        or 0x0600 <= ord(char) <= 0x06FF
        or 0x1E00 <= ord(char) < 0x2000
    )


def _fold(text: str) -> str:
    text = _strip_diacritics(_INVISIBLE.sub("", text).casefold())
    return "".join(_SUBSTITUTIONS.get(c, c) for c in text)


def _collapse_repeats(text: str) -> str:
    """Collapse a run of THREE OR MORE identical characters to one, so
    `fuuuck` reduces to `fuck`. Runs of exactly two are left alone: folding
    them would merge genuinely distinct words (`nigger` into the country name
    `Niger`, `puttana` into `putana`)."""
    return re.sub(r"(.)\1{2,}", r"\1", text)


def _needle(term: str) -> str:
    """The stored form of a term: folded, with its separators removed, so
    `f.u.c.k` and `fuck` reduce to the same needle.

    Separators are removed by tokenizing, not by a `\\w`-based substitution:
    `\\w` does not match Indic vowel signs, so the substitution deleted the
    marks that tell `रोड` and `रंडी` apart and left every Devanagari and
    Bengali needle truncated to its bare consonants."""
    return _collapse_repeats("".join(span.text for span in split_spans(_fold(term))))


@lru_cache(maxsize=1)
def _allowlist() -> Set[str]:
    """The allowlist normalized the same way tokens are, so entries can be
    written in their natural spelling."""
    return {_needle(w) for w in _ALLOWLIST}


@lru_cache(maxsize=1)
def _terms() -> Dict[str, Set[str]]:
    """Normalized needles unioned across languages (a message's language is
    unknown here too). `boundary` = one word in a space-delimited script,
    `phrase` = a run of consecutive words, `substring` = a script with no word
    spacing. Cached: the file is read once per process."""
    raw = json.loads(_WORDLIST_PATH.read_text(encoding="utf-8"))
    boundary: Set[str] = set()
    substring: Set[str] = set()
    phrase: Set[str] = set()
    for lang, words in raw.items():
        for w in words:
            if not w or not w.strip():
                continue
            needle = _needle(w)
            # Substring matching is only safe for needles that cannot occur
            # inside an unrelated word, and that is CJK terms written in their
            # own script and nothing else. A multi-word term is matched across
            # whole tokens instead: substring-matching it is what let the
            # Vietnamese term "pe de" fire inside "Pedestrian".
            if lang in _CJK_LANGS and _has_non_latin(w):
                substring.add(needle)
            elif _is_phrase(w):
                phrase.add(needle)
            else:
                boundary.add(needle)
    for bucket in (boundary, substring, phrase):
        bucket.discard("")
    return {"boundary": boundary, "substring": substring, "phrase": phrase}


def contains_profanity(text: str) -> bool:
    """Tier 2's matcher: true when the normalized message contains a wordlist
    term, by whole token, by a run of whole tokens, or - for scripts with no
    word spacing - anywhere inside a token.

    This is the RECALL-oriented tier. It folds diacritics and homoglyphs and
    matches inflections by prefix, and it is deliberately not what blocks a
    message: a false positive here costs an LLM call, while a false positive
    in Tier 1 costs a learner their sentence. Tier 1's core is
    `tier1_terms.matches_tier1`.
    """
    if not text:
        return False
    terms = _terms()
    folded = _fold(text)
    spans = split_spans(folded)
    tokens = [span.text for span in spans]
    if not tokens:
        return False

    # Scripts without word spacing: direct substring test.
    joined = "".join(tokens)
    for term in terms["substring"]:
        if term in joined:
            return True

    # Boundary: a token matches when it equals a needle, or starts with one
    # plus a short suffix (inflection/compound). Allowlisted tokens never match.
    if any(_token_matches(tok, terms["boundary"]) for tok in tokens):
        return True

    # A multi-word term, across consecutive whole tokens.
    # `within_sentence=False`: this tier is recall-oriented, and an evasion
    # written `đ.ị.t mẹ` is one term with punctuation inside it.
    if matches_phrase(spans, terms["phrase"], within_sentence=False):
        return True

    # Letters spaced apart (`f u c k`, `s.h.i.t`) leave a run of very short
    # tokens. Rejoin each such run and test it as one token — precise, unlike
    # substring-searching the whole message, which collides for short needles.
    for fragment in _short_token_runs(tokens):
        if _token_matches(fragment, terms["boundary"]):
            return True

    # A single stray fragment beside a longer one (`f*cking` -> `f` + `cking`)
    # is the same evasion with only one split point.
    for left, right in zip(tokens, tokens[1:]):
        if (len(left) <= 2 or len(right) <= 2) and _token_matches(
            left + right, terms["boundary"]
        ):
            return True

    return False


def _token_matches(token: str, needles: Set[str]) -> bool:
    """Does this token equal a needle, or a needle plus a short suffix?

    Asked of the TOKEN'S PREFIXES rather than by scanning the needle set, and
    the difference is a complexity class. The scan was O(tokens x needles) -
    540 comparisons per token, per message, measured at 1.86 ms on an
    89-character message and unbounded by input size. An inflection may add at
    most `_MAX_INFLECTION_SUFFIX` characters, so only four prefixes of the
    token can possibly be a needle, and each is one set lookup: O(1) per
    token, independent of how large the wordlist grows.
    """
    if not token or token in _allowlist():
        return False
    if token in needles:
        return True
    shortest = max(len(token) - _MAX_INFLECTION_SUFFIX, 1)
    for length in range(len(token) - 1, shortest - 1, -1):
        prefix = token[:length]
        if length >= _min_prefix_len(prefix) and prefix in needles:
            return True
    return False


def _min_prefix_len(needle: str) -> int:
    """Shortest needle allowed to match by prefix. Non-Latin scripts carry
    much more information per character (one Hangul syllable is a whole
    morpheme), so requiring four characters there would rule out real terms
    like 개새끼 while a two-character Latin needle would fire everywhere."""
    return 2 if _has_non_latin(needle) else _MIN_PREFIX_NEEDLE_LEN


def _has_non_latin(text: str) -> bool:
    return any(ord(c) > 0x2E80 for c in text)


def _is_phrase(term: str) -> bool:
    """True for a genuine multi-word term ("đụ má"), false for a single word
    typed with its letters spaced apart ("c u n t"). The distinction matters:
    a phrase is substring-matched, and treating a spacing evasion as one puts
    a short needle like `cunt` into the substring set, where it fires inside
    innocent words such as "Scunthorpe"."""
    parts = [p for p in term.split() if p]
    return len(parts) > 1 and all(len(p) >= 2 for p in parts)


def _short_token_runs(
    tokens: List[str], max_len: int = 2, min_run: int = 2
) -> List[str]:
    """Every rejoining of consecutive short tokens — the signature of a word
    typed with its letters separated (`f u c k`) or broken by punctuation
    (`c.u.n.t`).

    **Every WINDOW, not only the maximal run**, and that is the difference
    between catching the evasion and catching it only when it is alone on the
    line. Taking the maximal run let any ordinary short word beside the
    evasion destroy the match: `Spell it back to me: F,U,C,K.` rejoined as
    `tomefuck`, and `How do you spell it? c.u.n.t` as `itcunt`, because `to`,
    `me` and `it` are short tokens too. The evasion was there in both and this
    matcher reported neither.

    Bounded by the LONGEST NEEDLE rather than by a window count: a rejoining
    can only ever match a needle, so extending one past the longest needle in
    the wordlist cannot produce a match. That makes the scan linear in the
    message rather than quadratic, which matters because this runs on every
    Tier-2 message and a 60 KB message of short tokens is a legal one.
    """
    prefixes = _needle_prefixes()
    runs: List[str] = []
    count = len(tokens)
    for start in range(count):
        if len(tokens[start]) > max_len:
            continue
        joined = ""
        for index in range(start, count):
            token = tokens[index]
            if len(token) > max_len:
                break
            joined += token
            if joined not in prefixes:
                # A window that is not a prefix of any needle can never become
                # one, so the scan abandons it here. Without this every start
                # ran to the longest needle whatever the message said: a
                # 10,000-character message of short tokens cost ~88 ms of
                # reactor time, on a single-threaded reactor, per message.
                break
            if index - start + 1 >= min_run:
                runs.append(joined)
    return runs


@lru_cache(maxsize=1)
def _needle_prefixes() -> Set[str]:
    """Every prefix of every needle a rejoining could equal.

    The same device `tier1_terms._prefixes` uses on the phrase scan, and for
    the same reason: it turns "how long could this window get" into "can this
    window still become a match", which is what makes the scan proportional to
    the message rather than to the wordlist.
    """
    return {
        needle[:length]
        for bucket in _terms().values()
        for needle in bucket
        for length in range(1, len(needle) + 1)
    }
