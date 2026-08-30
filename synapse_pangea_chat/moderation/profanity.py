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

# Anything that is not a letter or digit separates tokens. Unicode-aware, so
# Spanish "¡Joder" and CJK punctuation tokenize correctly — a hand-listed
# punctuation set silently missed those.
_SEPARATORS = re.compile(r"[^\w]+", re.UNICODE)

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
    """Drop combining marks, then RECOMPOSE. Without the recompose step NFKD
    leaves Hangul as individual jamo, which inflates every Korean token's
    length and breaks the inflection-suffix bound."""
    decomposed = unicodedata.normalize("NFKD", text)
    # Drop every mark category, not just non-spacing ones. Indic SPACING
    # vowel signs (category Mc) are marks that `unicodedata.combining()`
    # reports as 0 and that `\w` does not match, so leaving them in made them
    # act as separators and shredded Bengali and Devanagari words into single
    # consonants.
    stripped = "".join(
        c for c in decomposed if not unicodedata.category(c).startswith("M")
    )
    return unicodedata.normalize("NFC", stripped)


def _fold(text: str) -> str:
    text = _strip_diacritics(_INVISIBLE.sub("", text).casefold())
    return "".join(_SUBSTITUTIONS.get(c, c) for c in text)


def _collapse_repeats(text: str) -> str:
    """Collapse a run of THREE OR MORE identical characters to one, so
    `fuuuck` reduces to `fuck`. Runs of exactly two are left alone: folding
    them would merge genuinely distinct words (`nigger` into the country name
    `Niger`, `puttana` into `putana`)."""
    return re.sub(r"(.)\1{2,}", r"\1", text)


def _normalize_token(token: str) -> str:
    """Fold a single separatorless token and collapse long repeats."""
    return _collapse_repeats(_fold(token))


def _normalize_joined(text: str) -> str:
    """Fold the whole message and remove separators, so split needles rejoin."""
    return _collapse_repeats(_SEPARATORS.sub("", _fold(text)))


@lru_cache(maxsize=1)
def _allowlist() -> Set[str]:
    """The allowlist normalized the same way tokens are, so entries can be
    written in their natural spelling."""
    return {_normalize_token(_SEPARATORS.sub("", w)) for w in _ALLOWLIST}


@lru_cache(maxsize=1)
def _terms() -> Dict[str, Set[str]]:
    """Normalized needles unioned across languages (a message's language is
    unknown at Tier 1). `boundary` = space-delimited scripts, `substring` =
    CJK. Cached: the file is read once per process."""
    raw = json.loads(_WORDLIST_PATH.read_text(encoding="utf-8"))
    boundary: Set[str] = set()
    substring: Set[str] = set()
    for lang, words in raw.items():
        for w in words:
            if not w or not w.strip():
                continue
            needle = _normalize_token(_SEPARATORS.sub("", w))
            # A multi-word term ("đụ má") can never match a single token, and
            # is specific enough that a substring test is safe. CJK terms are
            # substring-matched for the same reason (no word spacing).
            # Substring matching is only safe for needles that cannot occur
            # inside an unrelated word: CJK terms written in their own script,
            # and multi-word phrases. A Latin-script term listed under a CJK
            # language (a romanization) must still be boundary-matched, or it
            # fires inside any word that contains it.
            if (lang in _CJK_LANGS and _has_non_latin(w)) or _is_phrase(w):
                substring.add(needle)
            else:
                boundary.add(needle)
    boundary.discard("")
    substring.discard("")
    return {"boundary": boundary, "substring": substring}


def contains_profanity(text: str) -> bool:
    """True when the normalized message contains a wordlist term — as a whole
    token (space-delimited scripts) or a substring (CJK, plus spaced-out
    evasions of longer terms)."""
    if not text:
        return False
    terms = _terms()
    joined = _normalize_joined(text)
    if not joined:
        return False

    # CJK: direct substring test on the separatorless message.
    for term in terms["substring"]:
        if term in joined:
            return True

    # Boundary: a token matches when it equals a needle, or starts with one
    # plus a short suffix (inflection/compound). Allowlisted tokens never match.
    folded = _fold(text)
    tokens = [_collapse_repeats(t) for t in _SEPARATORS.split(folded) if t]
    if any(_token_matches(tok, terms["boundary"]) for tok in tokens):
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
    if not token or token in _allowlist():
        return False
    if token in needles:
        return True
    return any(
        len(n) >= _min_prefix_len(n)
        and token.startswith(n)
        and 0 < len(token) - len(n) <= _MAX_INFLECTION_SUFFIX
        for n in needles
    )


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
    """Concatenations of consecutive tokens of at most `max_len` characters —
    the signature of a word typed with its letters separated (`f u c k`) or
    broken by punctuation (`f*ck`)."""
    runs: List[str] = []
    current: List[str] = []
    for token in tokens:
        if len(token) <= max_len:
            current.append(token)
            continue
        if len(current) >= min_run:
            runs.append("".join(current))
        current = []
    if len(current) >= min_run:
        runs.append("".join(current))
    return runs
