"""Helpers that keep personal data out of moderation log records.

A moderation log line is not an ordinary diagnostic. It records that a
particular person tripped a content filter, which is a claim about them and
about what they wrote. Application logs are not an access-controlled store,
they are shipped to aggregators, and they outlive the decision by months, so
the module's rule is: no Matrix ID and no message text ever reaches a
handler. What does reach one is a room id, an event id, a rule identifier
and the digest below - enough to chase a false positive, and resolvable to a
person only through the database, under authorisation.

Three channels need closing, and only one of them is a log call we write:

- The arguments we pass. `sender_digest` is the substitute for a Matrix ID.
- The exceptions we let escape. `logger.exception` prints the exception's
  own message, and a library raising on a message body routinely quotes that
  body back ("cannot parse '<the message>'"). `error_site` keeps what makes
  such a failure debuggable - which line raised, and what type - without the
  message the traceback would have carried.
- The attributes Synapse attaches for us. `one_time_logging_setup` installs a
  log-record FACTORY that runs `LoggingContextFilter` over every record the
  process creates, so a record emitted inside a request context carries
  `requester` and `authenticated_entity` - the sender's Matrix ID - whether or
  not our format string mentions them. The default formatter prints neither,
  which is why this channel is easy to miss; a structured sink serialises the
  whole record and prints both. `scrubbing_logger` closes it.
"""

import hashlib
import logging
import os
import re
import secrets
from typing import Tuple

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


# The attributes `synapse.logging.context.LoggingContextFilter` copies off the
# in-flight request onto EVERY record, of which these name or locate the person
# the record is about. They are replaced rather than deleted: a deployment is
# free to configure a formatter that references `%(requester)s`, and deleting
# the attribute would turn a privacy fix into a KeyError in the logging system.
#
# The rest of what the filter sets - `request`, `server_name`, `site_tag`,
# `method`, `protocol` - names an endpoint and a request, not a person, and is
# what makes a record traceable, so it is left alone.
_IDENTITY_RECORD_ATTRS: Tuple[str, ...] = (
    "requester",
    "authenticated_entity",
    "ip_address",
    "user_agent",
)

# `url` is the awkward one, and it is neither dropped nor kept whole. The path
# is what tells an operator which endpoint produced a record, and most of them
# carry a room id rather than a Matrix ID - but a moderation record can be
# created under any request that persists an event, that set is not ours to
# enumerate, and some Matrix paths do embed a user id. So the Matrix IDs are
# taken out of the path and the rest of the path stays. A Matrix ID is
# `@localpart:server`; the character classes are the ones a Matrix ID and a URL
# path can actually contain, and the match is deliberately generous on the
# server part because over-redacting a URL costs nothing.
_MXID_IN_TEXT = re.compile(r"@[^\s:/?#]+:[^\s/?#]+")
_URL_RECORD_ATTRS: Tuple[str, ...] = ("url",)

REDACTED = "<redacted:pangea-moderation>"


class _IdentityScrubbingFilter(logging.Filter):
    """Removes the request identity Synapse attaches to records we emit.

    Attached to a LOGGER rather than to a handler, and that placement is the
    point: `Logger.handle` runs the logger's own filters before `callHandlers`
    walks the ancestor chain, so one filter covers every handler the record
    could reach, including the root handlers a deployment configures and any
    structured sink hanging off them.

    It cannot cover records this module did not create - Synapse logging an
    authorisation failure on our behalf is Synapse's record, on Synapse's
    logger. That is why `_check_and_redact` refuses to let an exception escape
    at all rather than relying on this.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for attr in _IDENTITY_RECORD_ATTRS:
            if getattr(record, attr, None) is not None:
                setattr(record, attr, REDACTED)
        for attr in _URL_RECORD_ATTRS:
            value = getattr(record, attr, None)
            if isinstance(value, str) and "@" in value:
                setattr(record, attr, _MXID_IN_TEXT.sub(REDACTED, value))
        return True


def scrubbing_logger(name: str) -> logging.Logger:
    """The module's `logging.getLogger`, with the identity scrubber attached.

    Every logger in the moderation package is obtained through this function.
    A logger's filters apply only to records logged THROUGH that logger -
    `callHandlers` inherits ancestors' handlers, never their filters - so
    attaching the scrubber to the package's top logger alone would leave every
    sub-module's records unscrubbed. Routing all of them through one factory is
    what makes that impossible to get wrong by adding a file.
    """
    logger = logging.getLogger(name)
    if not any(isinstance(f, _IdentityScrubbingFilter) for f in logger.filters):
        logger.addFilter(_IdentityScrubbingFilter())
    return logger


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
