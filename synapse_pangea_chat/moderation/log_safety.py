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
from typing import List, Optional, Tuple, TypeVar

_E = TypeVar("_E", bound=BaseException)

# 6 bytes is 12 hex characters: short enough to read in a log line, wide
# enough that two senders colliding within one process is not a practical
# concern (2^48 values against, at most, a few million users).
_DIGEST_BYTES = 6

# Every logger this module writes through sits under this name.
_PACKAGE_LOGGER = "synapse.modules.synapse_pangea_chat.moderation"


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
# The rest of what the filter sets - `server_name`, `site_tag`, `method`,
# `protocol` - names an endpoint and a request, not a person, and is what makes
# a record traceable, so it is left alone.
# `url` joins them, and the first attempt to keep it - scrubbing Matrix-ID
# shaped substrings out of the path and keeping the rest - was wrong. A Matrix
# request path carries client-chosen text: the transaction id of a message send
# is whatever the client put there, `%40alice%3Aexample.org` is a Matrix ID
# that no pattern over the decoded form will match, and a localpart may contain
# a `/`. `get_redacted_uri` removes access tokens and client secrets, not
# arbitrary request data. There is no pattern that separates "the endpoint" from
# "what the user typed" in a Matrix URL, so the whole value goes; `method` and
# the request id stay, and Synapse's own request lines carry the URL for an
# operator who needs it.
_IDENTITY_RECORD_ATTRS: Tuple[str, ...] = (
    "requester",
    "authenticated_entity",
    "ip_address",
    "user_agent",
    "url",
)

# `request` is the request id, and it is only safe in the shape Synapse
# generates: `get_request_id` returns `f"{method}-{sequence}"` UNLESS the
# deployment sets `request_id_header`, in which case the header's value is
# used verbatim - so a client that sends `X-Request-ID: @alice:example.org`
# puts its own Matrix ID on every record of its own request, and the shipped
# `precise` formatter PRINTS this field. The generated shape is kept, because
# it is what ties a moderation record to Synapse's own request lines; anything
# else is client-supplied text and goes.
_SYNAPSE_REQUEST_ID = re.compile(r"^[A-Z]+-\d+$")

REDACTED = "<redacted:pangea-moderation>"


class _IdentityScrubbingFilter(logging.Filter):
    """Removes the request identity Synapse attaches to records we emit.

    Attached to a LOGGER rather than to a handler, and that placement is the
    point: `Logger.handle` runs the logger's own filters before `callHandlers`
    walks the ancestor chain, so one filter covers every handler the record
    could reach, including the root handlers a deployment configures and any
    structured sink hanging off them.

    `only_prefix` exists for the second copy. A deployment is still supported
    in attaching `LoggingContextFilter` to a HANDLER - Synapse documents that
    configuration and keeps it working - and `Handler.handle` runs its own
    filters after the logger's, so such a handler re-derives the identity from
    the logcontext and puts it back on a record we had already cleaned. The
    answer is a second copy of this filter on those handlers, appended after
    theirs; the prefix keeps it to our own records, because stripping the
    requester from SYNAPSE's request log is not ours to do and would take away
    the identity an operator relies on everywhere else.

    It cannot cover records this module did not create - Synapse logging an
    authorisation failure on our behalf is Synapse's record, on Synapse's
    logger. That is why the module refuses to let an exception escape a
    moderation frame at all rather than relying on this.
    """

    def __init__(self, only_prefix: Optional[str] = None) -> None:
        super().__init__()
        self._only_prefix = only_prefix

    def filter(self, record: logging.LogRecord) -> bool:
        if self._only_prefix is not None and not (
            record.name == self._only_prefix
            or record.name.startswith(f"{self._only_prefix}.")
        ):
            # `startswith` on the bare prefix also matches a sibling logger
            # named `...moderation_something`, whose records are not ours to
            # touch. The dot is what makes it a namespace test.
            return True
        for attr in _IDENTITY_RECORD_ATTRS:
            if getattr(record, attr, None) is not None:
                setattr(record, attr, REDACTED)
        request = getattr(record, "request", None)
        if request is not None and not _SYNAPSE_REQUEST_ID.match(str(request)):
            # Written through `__dict__` because that is where a LogRecord
            # attribute lives; `setattr` with a constant name is the same
            # thing spelled less plainly.
            record.__dict__["request"] = REDACTED
        return True


def scrub_reachable_handlers(logger: logging.Logger) -> None:
    """Put a second scrubber on every handler `logger`'s records can reach.

    Called once when moderation starts. Handlers attached afterwards - by a
    logging-configuration reload, say - are not covered, and that residue is
    recorded in .github/instructions/moderation.instructions.md.
    """
    for target in _reachable_loggers(logger):
        for handler in target.handlers:
            _scrub_handler(handler)


def _scrub_handler(handler: logging.Handler) -> None:
    """Attach the scrubber to `handler`, and to whatever it forwards to.

    A `MemoryHandler` re-runs its TARGET's filters in `target.handle(record)`,
    and Synapse's shipped logging configuration uses exactly that buffer-to-
    target shape - so a target carrying `LoggingContextFilter` put the
    requester back on a record that had already passed a scrubbed buffer.
    """
    if not any(isinstance(f, _IdentityScrubbingFilter) for f in handler.filters):
        handler.addFilter(_IdentityScrubbingFilter(only_prefix=_PACKAGE_LOGGER))
    forwarded = getattr(handler, "target", None)
    if isinstance(forwarded, logging.Handler):
        _scrub_handler(forwarded)


def _reachable_loggers(logger: logging.Logger) -> List[logging.Logger]:
    """`logger`, its ancestors, and its descendants.

    The descendants are the half an earlier version missed. Walking upwards
    covers the handlers a record PROPAGATES to; a handler attached directly to
    `moderation.tier1_prefilter` is not on that path, and a record emitted
    through that child logger reaches it first of all.
    """
    reachable: List[logging.Logger] = []
    current: Optional[logging.Logger] = logger
    while current is not None:
        reachable.append(current)
        if not current.propagate:
            break
        current = current.parent
    prefix = f"{logger.name}."
    for name, existing in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(existing, logging.Logger) and name.startswith(prefix):
            reachable.append(existing)
    return reachable


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


def _severed(error: _E) -> _E:
    """Detach an exception from whatever was being handled when it was raised.

    The interpreter attaches the active exception at RAISE time, so this cannot
    be done in `__init__` (too early) and `raise ... from None` does not do it
    (it only suppresses the RENDERING, leaving `__context__` set). Shadowing
    the attribute with a property was worse: it conceals the chain from an
    ordinary read while `BaseException.__context__.__get__` still returns the
    original - a mask, not a fix, and a privacy claim that is not true.

    So the exception is caught one frame out, the real slots are cleared, and
    it is re-raised bare - which does not re-attach, because the exception is
    already the one being handled. Used at the module's own boundaries.
    """
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    return error


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
