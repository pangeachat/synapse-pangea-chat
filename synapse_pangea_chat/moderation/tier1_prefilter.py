"""Tier 1 deterministic pre-filter (trust-and-safety: server-side moderation).

Pure, model-free checks fast enough to run inline in the send path via
`check_event_for_spam` (pre-persist, can reject). Covers content where the
right outcome is to never let it appear, even briefly: contact details a
minor might share (phone numbers, street addresses) and wordlist profanity.

Package choices and their limits are recorded in
.github/instructions/moderation.instructions.md — notably the address regex
and profanity wordlist are English-centric by design; Tier 2 (LLM, redact
after) is the multilingual backstop.
"""

import logging
import re
from typing import Iterable, Optional

import phonenumbers
from better_profanity import Profanity

logger = logging.getLogger(
    "synapse.modules.synapse_pangea_chat.moderation.tier1_prefilter"
)

# Reason codes surfaced in logs (never user-visible text).
REASON_PHONE_NUMBER = "phone_number"
REASON_STREET_ADDRESS = "street_address"
REASON_PROFANITY = "profanity"

# Conservative street-address shape: a 1-5 digit house number, one to four
# capitalized-or-plain name words, then a street-suffix word. Deliberately
# narrow — a false block on ordinary chat is worse than a miss (Tier 2 and
# human reporting back this up), so no city/zip-only or bare-suffix matching.
_STREET_SUFFIXES = (
    "street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|"
    "place|pl|terrace|ter|way|square|sq|highway|hwy|parkway|pkwy|circle|cir"
)
_ADDRESS_RE = re.compile(
    r"\b\d{1,5}\s+(?:[A-Za-z][a-z'.-]*\s+){1,4}(?:" + _STREET_SUFFIXES + r")\.?\b",
    re.IGNORECASE,
)

# Module-level singleton: loading the wordlist is file I/O; do it once.
_profanity = Profanity()


def contains_phone_number(text: str, regions: Iterable[str]) -> bool:
    """True when libphonenumber finds a VALID number for any given region.

    Numbers written with an international prefix (+33 6...) match under any
    region, so the region list only needs to cover the national formats our
    users are likely to type bare (configured per deployment).
    """
    for region in regions:
        try:
            if any(phonenumbers.PhoneNumberMatcher(text, region)):
                return True
        except Exception:  # pragma: no cover - defensive: library quirk
            # silent-ok: fail-open per tier contract; logged for visibility,
            # and Tier 2 still sees the message.
            logger.warning("phone matcher failed for region %s", region, exc_info=True)
    return False


def contains_street_address(text: str) -> bool:
    return _ADDRESS_RE.search(text) is not None


def contains_profanity(text: str) -> bool:
    return _profanity.contains_profanity(text)


def check_text(text: str, phone_regions: Iterable[str]) -> Optional[str]:
    """Return a reason code when the text trips a Tier 1 rule, else None."""
    if contains_phone_number(text, phone_regions):
        return REASON_PHONE_NUMBER
    if contains_street_address(text):
        return REASON_STREET_ADDRESS
    if contains_profanity(text):
        return REASON_PROFANITY
    return None
