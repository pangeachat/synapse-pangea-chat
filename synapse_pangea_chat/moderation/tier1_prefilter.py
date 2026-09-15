"""Tier 1 deterministic pre-filter (trust-and-safety: server-side moderation).

Pure, model-free checks fast enough to run inline in the send path via
`check_event_for_spam` (pre-persist, can reject): phone numbers a minor might
share, and wordlist profanity.

Street addresses were a third rule and are deliberately not here. A pattern
cannot tell a shared address from a discussed one, and in a language-learning
room the innocent readings dominate: landmarks are a stock topic and naming
where you live is an A1 lesson. Tier 1 rejects before persist, so each of
those was a learner silenced mid-sentence. Judging an address needs the
context only Tier 2 has.

Package choices and their limits are recorded in
.github/instructions/moderation.instructions.md. Profanity is matched across
all our learner languages (see `profanity.py`).
"""

from typing import Callable, Iterable, List, Optional, Tuple

import phonenumbers

from synapse_pangea_chat.moderation.log_safety import (
    _severed,
    error_site,
    scrubbing_logger,
)
from synapse_pangea_chat.moderation.profanity import (
    contains_profanity as _contains_profanity_multilingual,
)

logger = scrubbing_logger(
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
REASON_PROFANITY = "profanity"


def validate_phone_regions(regions: object) -> List[str]:
    """Return the configured regions, or raise `ValueError` describing why not.

    Validated at config-parse time, against libphonenumber's own list, because
    every way of getting this key wrong fails the same way: the matcher loops
    over the regions it was given and finds nothing, so the phone rule
    silently does not run. A shape check alone does not catch it - `[]` runs
    zero passes, and `"us"` or `"US "` are strings of the right type that
    libphonenumber does not recognise, so both return no matches for a number
    the operator believes is blocked. Neither says anything at any log level.

    An empty list is refused rather than read as "international numbers only":
    libphonenumber still needs a region to match against, so there is no way
    to express that here, and an empty list can only be a mistake.
    """
    if not isinstance(regions, list) or not all(
        isinstance(region, str) for region in regions
    ):
        raise ValueError(
            'Config "moderation.tier1_phone_regions" must be a list of strings'
        )
    if not regions:
        raise ValueError(
            'Config "moderation.tier1_phone_regions" must name at least one '
            "region; an empty list disables phone matching entirely, "
            "including international formats"
        )
    for region in regions:
        if region not in phonenumbers.SUPPORTED_REGIONS:
            raise ValueError(
                f'Config "moderation.tier1_phone_regions" entry {region!r} is '
                "not a region libphonenumber knows. Use the uppercase ISO "
                "3166-1 alpha-2 code, with no surrounding whitespace - "
                '"US", not "us" or "US ".'
            )
    return list(regions)


def contains_phone_number(text: str, regions: Iterable[str]) -> bool:
    """True when libphonenumber finds a VALID number for any given region.

    Numbers written with an international prefix (+33 6...) match under any
    region, so the region list only needs to cover the national formats our
    users are likely to type bare (configured per deployment).

    Exceptions are NOT caught here. A rule that cannot answer must say so; see
    `check_text` for why swallowing one was a security defect rather than
    defensive programming.
    """
    return any(any(phonenumbers.PhoneNumberMatcher(text, region)) for region in regions)


def contains_profanity(text: str) -> bool:
    return _contains_profanity_multilingual(text)


class Tier1RuleError(Exception):
    """A Tier 1 rule could not complete, so the tier has no verdict.

    Carries a rule identifier and nothing else: the text that broke the rule is
    exactly what must not travel with the exception (ADR-10). `check_text`
    severs the chain at its boundary, because `raise ... from None` only
    suppresses the rendering and leaves `__context__` holding the original -
    and a matcher that failed on a message body routinely quotes that body.
    """


# The rules, in the order they are asked, each paired with the reason it
# returns. A table rather than a chain of `if`s so that the failure policy
# below is applied to every rule by construction, and a rule added later gets
# it without anybody remembering to.
_RULES: Tuple[Tuple[str, Callable[[str, Iterable[str]], bool]], ...] = (
    (
        REASON_CONTACT_DETAILS,
        lambda text, regions: contains_phone_number(text, regions),
    ),
    (REASON_PROFANITY, lambda text, _regions: contains_profanity(text)),
)


def check_text(text: str, phone_regions: Iterable[str]) -> Optional[str]:
    """Return a reason code when the text trips a Tier 1 rule, else None.

    Raises `Tier1RuleError` when any rule fails to complete. That is the whole
    of the failure policy, and it is deliberately blunt: **a tier with a broken
    rule has no verdict at all.**

    The predecessor caught the phone matcher's exception inside
    `contains_phone_number` and returned `False`, which is not the same thing
    as "no phone number here" - it is "we do not know" wearing the answer's
    clothes. `check_text` then went on to the next rule and returned
    `Codes.FORBIDDEN` on its verdict, so a message was REJECTED on the strength
    of a Tier 1 run that had already failed, in a tier whose entire contract is
    that a failure lets the message through. Converting a partial failure into
    a clean negative is the class; the table above and this raise are the fix
    for all of it rather than for the rule that happened to expose it.

    The caller's handler logs the failure and returns NOT_SPAM, and Tier 2
    still sees the message.
    """
    try:
        return _check_rules(text, list(phone_regions))
    except Tier1RuleError as error:
        _severed(error)
        raise


def _check_rules(text: str, regions: List[str]) -> Optional[str]:
    for reason, rule in _RULES:
        try:
            hit = rule(text, regions)
        except Exception as exc:
            # Type and site, never the message or a traceback: a matcher that
            # fails on a message body routinely quotes that body back.
            logger.warning(
                "tier1 rule %s failed at %s (%s); the whole tier fails open",
                reason,
                error_site(exc),
                type(exc).__name__,
            )
            # `from None` suppresses the chain, so nothing downstream that
            # logs an exception can print the original library message.
            raise Tier1RuleError(reason) from None
        if hit:
            return reason
    return None
