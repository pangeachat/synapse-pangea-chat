"""Helpers that keep personal data out of moderation log records.

A moderation log line is not an ordinary diagnostic. It records that a
particular person tripped a content filter, which is a claim about them and
about what they wrote. Application logs are not an access-controlled store,
they are shipped to aggregators, and they outlive the decision by months, so
the module's rule is: no Matrix ID and no message text ever reaches a
handler. What does reach one is a room id, an event id, a rule identifier
and the digest below - enough to chase a false positive, and resolvable to a
person only through the database, under authorisation.

Two channels need closing, and only one of them is a log call we write:

- The arguments we pass. `sender_digest` is the substitute for a Matrix ID.
- The exceptions we let escape. `logger.exception` prints the exception's
  own message, and a library raising on a message body routinely quotes that
  body back ("cannot parse '<the message>'"). `error_site` keeps what makes
  such a failure debuggable - which line raised, and what type - without the
  message the traceback would have carried.
"""

import hashlib
import os
import secrets

# 6 bytes is 12 hex characters: short enough to read in a log line, wide
# enough that two senders colliding within one process is not a practical
# concern (2^48 values against, at most, a few million users).
_DIGEST_BYTES = 6


def new_digest_key() -> bytes:
    """A fresh key for `sender_digest`, generated once per module instance.

    Keyed, not plain: the set of Matrix IDs on a homeserver is small and its
    shape is known, so an unsalted hash of one is reversible by enumeration
    and would be an anonymisation in name only. Generating the key per
    process rather than deriving it from config is deliberate - correlation
    across restarts is the property that turns a log into a behavioural
    record of a named person, and within one process is all an operator
    needs to see that the same sender tripped a rule repeatedly.
    """
    return secrets.token_bytes(16)


def sender_digest(sender: str, key: bytes) -> str:
    """A short, non-reversible stand-in for a Matrix ID, stable for a key."""
    return hashlib.blake2b(
        sender.encode("utf-8"), key=key, digest_size=_DIGEST_BYTES
    ).hexdigest()


def error_site(exc: BaseException) -> str:
    """Where an exception was raised, as `file:line`, and nothing else.

    Deliberately excludes the exception's message and the frames above it:
    both can carry the text that caused the failure. The caller logs the
    exception's type separately, which together with the site is what
    actually identifies a bug.
    """
    tb = exc.__traceback__
    if tb is None:
        return "unknown"
    while tb.tb_next is not None:
        tb = tb.tb_next
    return f"{os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}"
