"""What a learner is told when Tier 1 refuses their message.

Tier 1 returned a bare `Codes.FORBIDDEN` and Synapse turned that into a
generic "This message has been rejected as probable spam", which every client
renders as an unexplained red failure. A learner mid-sentence in a second
language was given no reason, no rule, no indication that a machine decided
it, and nothing to do about it.

That is a compliance gap as well as a bad experience. The EU Digital Services
Act (Art. 17) requires a statement of reasons whenever content is removed or
restricted, and requires it to say **whether automated means were used**; the
Santa Clara Principles ask for the same thing in the same words. Both are
satisfied by one short sentence in language the person can read, and neither
is satisfied by a red icon.

**The statement names the RULE, never the match.** This is the constraint the
whole module is written around, applied to the one surface that faces the
person who wrote the text. Telling a sender *which term* tripped the filter
turns every refusal into a free query against the wordlist: send a candidate,
read back which word was named, and the list is enumerable a message at a
time. Naming the rule family - "contact details, like a phone number", "a word
we do not allow" - is the coarsest statement that is still a statement, and it
is all a sender who already knows what they typed actually needs. The messages
below are therefore CONSTANT per rule: two different blocked texts that trip
the same rule produce byte-identical bodies, so the response carries no
information about the text at all beyond which of the two rules fired.

The text is never echoed back either, for a different reason: the refusal
travels through error-handling paths that log and report, and a message body
quoted into one of them is the leak `log_safety` exists to prevent.

**The wire shape.** `CHECK_EVENT_FOR_SPAM_CALLBACK` is typed
`Awaitable[str | Codes | tuple[Codes, JsonDict] | bool]`, and
`spamchecker_callbacks.py` returns a two-tuple straight through to
`handlers/message.py`, which raises
`SynapseError(403, "<a fixed string>", code, dict)`. The fixed string is not
ours to choose - but `SynapseError.error_dict` builds the body with
`cs_error(msg, errcode, **additional_fields)`, and `cs_error` writes its
keyword arguments over the dict it has already built. So `error` in the
additional fields REPLACES Synapse's generic sentence, which is the only route
by which a reason reaches the client at all. That behaviour is depended upon,
so it is pinned by a test against the real Synapse functions rather than
assumed; an upgrade that reorders `cs_error` fails the suite instead of
silently restoring the generic message.
"""

from typing import Any, Dict, Mapping, Tuple

from synapse_pangea_chat.moderation.tier1_prefilter import (
    REASON_CONTACT_DETAILS,
    REASON_PROFANITY,
    RULE_REASONS,
)

#: The operator's key for overriding the messages below.
CONFIG_KEY = "tier1_refusal_messages"

#: The message used for a rule with no message of its own. A rule added later
#: gets a true, if unspecific, statement rather than silence - and the test
#: that every rule in `RULE_REASONS` has its OWN entry is what stops this
#: becoming the answer to everything.
DEFAULT_KEY = "default"

#: Machine-readable companions to the sentence, both namespaced so they cannot
#: collide with a Matrix-specified field. The rule identifier is what lets a
#: client show the sentence in the learner's own language - which matters more
#: here than in most products, since the people reading it are by definition
#: still learning the language it is written in. It discloses exactly what the
#: sentence already discloses: which of the rules fired, and nothing finer.
REASON_FIELD = "chat.pangea.moderation.rule"
#: DSA Art. 17's "whether automated means were used", as a field a client can
#: act on rather than a phrase it has to parse out of prose.
AUTOMATED_FIELD = "chat.pangea.moderation.automated"

#: Longest message an operator may configure. A bound rather than a taste
#: judgement: this string is returned on every refused send, and there is no
#: length at which a wall of text in a chat error is the kind thing to do.
MAX_MESSAGE_LENGTH = 1_000

# Short sentences, ordinary words, no idiom and no contraction-heavy phrasing:
# the reader is a language learner, possibly at A1, and possibly a minor. Each
# message does four things and stops - says the message did not send, says a
# machine decided it (DSA Art. 17), names the rule family, and points at the
# redress path that actually exists in a classroom. None of them names a term,
# quotes the message, or varies with its content.
DEFAULT_MESSAGES: Dict[str, str] = {
    REASON_CONTACT_DETAILS: (
        "Your message was not sent. An automatic filter found contact "
        "details, like a phone number. Please remove them and try again. If "
        "you think this is a mistake, tell your teacher."
    ),
    REASON_PROFANITY: (
        "Your message was not sent. An automatic filter found a word we do "
        "not allow in chat. Please change it and try again. If you think this "
        "is a mistake, tell your teacher."
    ),
    DEFAULT_KEY: (
        "Your message was not sent. An automatic filter does not allow this "
        "message. Please change it and try again. If you think this is a "
        "mistake, tell your teacher."
    ),
}

#: Every key an operator may set, derived from the rules themselves so that a
#: rule added to `tier1_prefilter._RULES` is configurable the same day it
#: exists rather than the day somebody remembers this file.
CONFIGURABLE_KEYS: Tuple[str, ...] = RULE_REASONS + (DEFAULT_KEY,)


def validate_messages(raw: Any) -> Dict[str, str]:
    """Return the configured messages merged over the defaults, or raise.

    Validated at config-parse time, and strictly, because every way of getting
    this key wrong is silent at runtime: a misspelled rule name is a message
    an operator wrote and no learner will ever see, and the default underneath
    it keeps working, so nothing anywhere says the override did not take.
    """
    if raw is None:
        return dict(DEFAULT_MESSAGES)
    if not isinstance(raw, Mapping):
        raise ValueError(f'Config "moderation.{CONFIG_KEY}" must be a mapping')
    messages = dict(DEFAULT_MESSAGES)
    for key, value in raw.items():
        if key not in CONFIGURABLE_KEYS:
            raise ValueError(
                f'Config "moderation.{CONFIG_KEY}" key {key!r} is not a Tier 1 '
                f"rule. Allowed keys: {', '.join(sorted(CONFIGURABLE_KEYS))}"
            )
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f'Config "moderation.{CONFIG_KEY}" entry {key!r} must be a '
                "non-empty string"
            )
        if len(value) > MAX_MESSAGE_LENGTH:
            raise ValueError(
                f'Config "moderation.{CONFIG_KEY}" entry {key!r} is longer '
                f"than {MAX_MESSAGE_LENGTH} characters"
            )
        messages[key] = value
    return messages


def refusal_body(rule: str, messages: Mapping[str, str]) -> Dict[str, Any]:
    """The `JsonDict` half of Tier 1's refusal, for the rule that fired.

    `messages` is keyed by rule identifier; a rule with no entry falls back to
    the default sentence rather than to Synapse's generic one, because a rule
    this file has not caught up with is still a rule the sender is entitled to
    a statement about.
    """
    return {
        # Overrides Synapse's fixed "rejected as probable spam" - see the
        # module docstring for why this key and not another.
        "error": messages.get(rule) or messages[DEFAULT_KEY],
        REASON_FIELD: rule,
        AUTOMATED_FIELD: True,
    }
