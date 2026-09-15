"""Tier 1 deterministic pre-filter (trust-and-safety: server-side moderation).

Pure, model-free checks fast enough to run inline in the send path via
`check_event_for_spam` (pre-persist, can reject). Covers content where the
right outcome is to never let it appear, even briefly: contact details a
minor might share (phone numbers, street addresses) and wordlist profanity.

Package choices and their limits are recorded in
.github/instructions/moderation.instructions.md. Profanity is matched across
all our learner languages (see `profanity.py`); the address pattern remains
English-centric, with Tier 2 as its backstop.
"""

import logging
import re
from typing import Iterable, Optional

import phonenumbers

from synapse_pangea_chat.moderation.log_safety import error_site
from synapse_pangea_chat.moderation.profanity import (
    contains_profanity as _contains_profanity_multilingual,
)

logger = logging.getLogger(
    "synapse.modules.synapse_pangea_chat.moderation.tier1_prefilter"
)

# Rule identifiers, surfaced in logs and never in user-visible text.
#
# These name the RULE, not the personal data the rule looks for, and that is
# deliberate. A log line reading `rule=phone_number` next to a room id is not
# a neutral diagnostic: it asserts what a particular message contained, which
# is the kind of record this module keeps out of plaintext logs. The rule
# family is what an operator debugging a false positive actually needs, and
# there is one rule per family, so nothing is lost - the mapping from
# identifier to check is in moderation.instructions.md.
REASON_CONTACT_DETAILS = "contact_details"
REASON_LOCATION_DETAILS = "location_details"
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
        except Exception as exc:  # pragma: no cover - defensive: library quirk
            # silent-ok: fail-open per tier contract; logged for visibility,
            # and Tier 2 still sees the message.
            #
            # The exception's TYPE and the line that raised it are logged; its
            # message and traceback are not. A parsing library that fails on a
            # message body routinely quotes that body back in the error, so
            # `exc_info=True` here would put message text into the log by a
            # route no review of our own format strings would catch.
            logger.warning(
                "phone matcher failed for region %s at %s (%s)",
                region,
                error_site(exc),
                type(exc).__name__,
            )
    return False


def contains_street_address(text: str) -> bool:
    return _ADDRESS_RE.search(text) is not None


def contains_profanity(text: str) -> bool:
    return _contains_profanity_multilingual(text)


def check_text(text: str, phone_regions: Iterable[str]) -> Optional[str]:
    """Return a reason code when the text trips a Tier 1 rule, else None."""
    if contains_phone_number(text, phone_regions):
        return REASON_CONTACT_DETAILS
    if contains_street_address(text):
        return REASON_LOCATION_DETAILS
    if contains_profanity(text):
        return REASON_PROFANITY
    return None
