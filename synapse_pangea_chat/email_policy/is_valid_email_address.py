from __future__ import annotations

import re

# Mail servers in practice refuse addresses longer than this. Synapse's own
# ceiling is 500, which is more than any real address needs.
MAX_ADDRESS_LENGTH = 254

# Letters and digits are matched Unicode-aware so that an internationalised
# domain typed in its native script is accepted alongside its punycode form.
_LABEL = re.compile(r"[^\W_](?:[^\W_]|-)*", re.UNICODE)
_TOP_LEVEL_LABEL = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def is_valid_email_address(address: str) -> bool:
    """Whether an email address is acceptable to Pangea at signup.

    Deliberately looser than the email address standard and deliberately
    stricter than Synapse's built-in check, which accepts anything holding a
    single "@". The goal is to catch obvious junk before it costs a hard
    bounce, not to be a complete implementation of the standard: rejecting a
    real learner's unusual address is a worse outcome than letting one junk
    address through.

    Expects the canonicalised address returned by Synapse's ``validate_email``.

    Design: .github/instructions/email-address-policy.instructions.md
    """
    if len(address) > MAX_ADDRESS_LENGTH:
        return False

    local_part, separator, domain = address.rpartition("@")
    if not separator or not local_part:
        return False
    if any(character.isspace() for character in address) or "@" in local_part:
        return False

    labels = domain.split(".")
    # At least two labels, so that "a@b" is refused.
    if len(labels) < 2:
        return False
    if not all(_is_label(label) for label in labels):
        return False
    # Two or more letters, so that "a@b.c" and "a@b.11" are refused.
    return _TOP_LEVEL_LABEL.fullmatch(labels[-1]) is not None


def _is_label(label: str) -> bool:
    """A domain label: starts and ends alphanumeric, hyphens allowed inside."""
    return _LABEL.fullmatch(label) is not None and not label.endswith("-")
