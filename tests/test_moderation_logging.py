"""The moderation module must not write personal data to any log.

A moderation log record is not an ordinary diagnostic: it states that a
particular person tripped a content filter. Ordinary application logs are
not an access-controlled store and they outlive the decision by months, so
the rule the module holds to is that no Matrix ID and no message text ever
reaches a handler. Room ids, event ids and rule identifiers do — those are
what an operator needs to chase a false positive, and they resolve to a
person only through the database, under authorisation.

The tests capture the ROOT logger rather than the module's own. A leak that
arrives through a dependency's logger, or through an exception Synapse logs
on our behalf, is the same leak, and a module-scoped capture cannot see it.

**They also install Synapse's real request-logging machinery.** An earlier
revision of this file listed `LoggingContextFilter` as a channel "no module can
close", recorded it as a documented limit, and then scoped every test so the
channel was never exercised: no test installed a logcontext, so the attribute
carrying the Matrix ID was never set, and the canary assertions passed over a
leak that was real in production. That is a gate softened around a real
finding, which is the one thing these tests exist to prevent. The channel IS
closeable from inside a module, it is closed
(`log_safety.scrubbing_logger`), and `ModerationLogContextTestCase` drives the
real `LoggingContextFilter` through the real log-record factory to prove it.

What remains genuinely outside a module's reach, stated rather than excluded:
Synapse's own records, on Synapse's own loggers. `handle_new_client_event`
logs "Denying new event" with the Matrix ID before raising, and
`run_as_background_process` calls `logger.exception` on whatever a background
task lets escape. A filter on our loggers cannot touch either. The module's
answer is to let no exception escape a moderation frame at all
(`test_redaction_failure_logs_no_matrix_id`); the residue - what Synapse logs
of its own accord, entirely outside our call stack - is a homeserver property
and is recorded as a limit in moderation.instructions.md.
"""

import contextlib
import importlib
import logging
import pkgutil
import unittest
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, cast
from unittest.mock import AsyncMock, create_autospec, patch

from synapse.events import EventBase
from synapse.logging.context import (
    ContextRequest,
    LoggingContext,
    LoggingContextFilter,
)
from synapse.module_api import ModuleApi

import synapse_pangea_chat.moderation as moderation_package
from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.moderation import ChatModeration
from synapse_pangea_chat.moderation.choreo_client import moderate_text
from synapse_pangea_chat.moderation.log_safety import _IdentityScrubbingFilter

# Strings that must never survive a moderation code path. Distinctive enough
# that a substring search cannot match them by accident.
CANARY_SENDER = "@canary-mxid-zqxj:canary-server.invalid"
CANARY_LOCALPART = "canary-mxid-zqxj"
CANARY_TEXT = "canary-body-vkwp call me: 415-555-2671"
CANARY_TOKEN = "syt_canary-token-hprm"
CANARY_IP = "canary-ip-203-0-113-4"
CANARY_USER_AGENT = "canary-agent-mtrz"


def _standard_record_attrs() -> frozenset:
    """The attributes a bare `LogRecord` has, so everything else is an extra.

    Built by instantiating `logging.LogRecord` DIRECTLY, which is deliberate
    and is not the softening the module docstring describes: a direct
    instantiation does not go through `logging.getLogRecordFactory()`, so this
    baseline never carries the request attributes the factory adds - deriving
    it is therefore both safe and self-maintaining across Python versions,
    where a hardcoded list drifts. A hardcoded list that names an attribute the
    record does NOT have (`message` and `asctime`, which `Formatter.format`
    adds later) silently excludes that attribute from the search, which is a
    place to hide a leak.
    """
    return frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)


_STANDARD_RECORD_ATTRS = _standard_record_attrs()


def _exception_chain(error: BaseException) -> str:
    """Everything a structured sink could serialise off an exception."""
    seen: List[str] = []
    current: Optional[BaseException] = error
    while current is not None:
        seen.append(repr(current.args))
        seen.append(repr(getattr(current, "__dict__", {})))
        seen.append(repr(getattr(current, "doc", "")))
        current = current.__cause__ or current.__context__
    return "\n".join(seen)


class _CapturingHandler(logging.Handler):
    """Records the formatted message, the raw arguments and every extra.

    Formatting alone is not enough: a handler configured with a different
    formatter, or a structured-logging sink, would still serialise the
    arguments and the record's own attributes, so a leak that shows up only in
    `record.args` or only in `record.__dict__` is a real leak.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.seen: List[str] = []
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        try:
            self.seen.append(record.getMessage())
        except Exception:
            self.seen.append(str(record.msg))
        self.seen.append(repr(record.args))
        # An exception passed as a log ARGUMENT is not covered by `exc_info`,
        # and it carries a chain: `raise X from None` clears `__cause__` and
        # stops the traceback being rendered, but leaves `__context__` set, so
        # a `json.JSONDecodeError` with the whole response body on `.doc` was
        # reachable from a record whose own message named only a type. A
        # structured sink walks it; so does this.
        for argument in record.args or ():
            if isinstance(argument, BaseException):
                self.seen.append(_exception_chain(argument))
        if record.exc_info is not None:
            self.seen.append(logging.Formatter().formatException(record.exc_info))
            # The formatted traceback is not the whole exception. A structured
            # sink serialises `record.exc_info` itself, so the exception's own
            # args - where a library puts the text it failed on - are searched
            # directly rather than only in the rendering.
            exception = record.exc_info[1]
            if exception is not None:
                self.seen.append(_exception_chain(exception))
        # Anything attached with `extra=`, and everything Synapse's global
        # log-record factory attaches, lands in the record's __dict__ and never
        # appears in the formatted message - so a leak through that door would
        # be invisible to the lines above.
        self.seen.append(
            repr(
                {
                    key: value
                    for key, value in record.__dict__.items()
                    if key not in _STANDARD_RECORD_ATTRS
                }
            )
        )


class FakeEvent:
    def __init__(
        self,
        body: Optional[str] = None,
        msgtype: str = "m.text",
        sender: str = CANARY_SENDER,
        event_type: str = "m.room.message",
        content: Optional[Dict[str, Any]] = None,
    ):
        self.type = event_type
        self.sender = sender
        self.room_id = "!room:example.org"
        self.event_id = "$evt1"
        if content is not None:
            self.content = content
        elif body is None:
            self.content = {}
        else:
            self.content = {"msgtype": msgtype, "body": body}


def _event(*args: Any, **kwargs: Any) -> EventBase:
    # The module only reads the event surfaces this stub provides; the cast
    # states that intent rather than dragging a real Synapse event store in.
    return cast(EventBase, FakeEvent(*args, **kwargs))


def _module_api() -> ModuleApi:
    """A signature-enforcing `ModuleApi`, for the reason given in
    tests/test_moderation_unit.py: an unrestricted mock accepts every call, so
    a test built on one cannot fail when the call shape is wrong."""
    api = create_autospec(ModuleApi, instance=True)
    api._hs = SimpleNamespace(hostname="example.org")
    return cast(ModuleApi, api)


@contextlib.contextmanager
def _synapse_request_logcontext(request_id: str = "PUT-1") -> Iterator[None]:
    """Synapse's real request logging, installed the way Synapse installs it.

    `synapse.config.logger.one_time_logging_setup` does not add
    `LoggingContextFilter` to a logger or a handler - it replaces the process's
    log-record FACTORY, so every record created anywhere in the process is
    decorated at creation time with the in-flight request's `requester` and
    `authenticated_entity`. Reproducing that exactly is the point: a filter
    attached to a handler somewhere could be argued away, and this cannot.
    """
    previous_factory = logging.getLogRecordFactory()
    context_filter = LoggingContextFilter()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        context_filter.filter(record)
        return record

    logging.setLogRecordFactory(factory)
    context = LoggingContext(
        name=request_id,
        server_name="canary-server.invalid",
        request=ContextRequest(
            request_id=request_id,
            ip_address=CANARY_IP,
            site_tag="synapse",
            requester=CANARY_SENDER,
            authenticated_entity=CANARY_SENDER,
            method="PUT",
            # A Matrix URL carries client-chosen text in three places at
            # once: a user id in the path, the same id percent-encoded where
            # no pattern over the decoded form would find it, and a
            # transaction id that is whatever the client typed. That is why
            # the whole value goes rather than a scrubbed version of it.
            url=(
                "/_matrix/client/v3/rooms/!room:example.org/send/"
                f"m.room.message/{CANARY_TEXT}"
                f"?who=%40{CANARY_LOCALPART}%3Acanary-server.invalid"
            ),
            protocol="HTTP/1.1",
            user_agent=CANARY_USER_AGENT,
        ),
    )
    try:
        with context:
            yield
    finally:
        logging.setLogRecordFactory(previous_factory)


def _verdict() -> AsyncMock:
    """A signature-enforcing `moderate_text`. A bare `AsyncMock` accepts
    `access_t0ken=` as happily as `access_token=`, so a test built on one
    cannot see the caller getting the keyword wrong - which the real function
    rejects, inside the fail-open handler, on every message."""
    mock = create_autospec(moderate_text)
    mock.return_value = {
        "flagged": True,
        "categories": ["harassment"],
        "evaluated": True,
    }
    return cast(AsyncMock, mock)


def _config(**overrides: Any) -> PangeaChatConfig:
    defaults: Dict[str, Any] = {
        "cms_base_url": "http://cms.invalid",
        "cms_service_api_key": "k",
        "moderation_tier1_enabled": True,
        "moderation_tier2_enabled": False,
        "moderation_choreo_access_token": CANARY_TOKEN,
    }
    defaults.update(overrides)
    return PangeaChatConfig(**defaults)


class ModerationLoggingTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.handler = _CapturingHandler()
        root = logging.getLogger()
        self._previous_level = root.level
        root.addHandler(self.handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, self.handler)
        self.addCleanup(root.setLevel, self._previous_level)

    def assertNoCanaries(self) -> None:
        captured = "\n".join(self.handler.seen)
        for canary in (
            CANARY_SENDER,
            CANARY_LOCALPART,
            CANARY_TEXT,
            "canary-body-vkwp",
            CANARY_TOKEN,
        ):
            self.assertNotIn(
                canary,
                captured,
                f"{canary!r} reached a log handler; captured:\n{captured}",
            )

    def assertLoggedSomething(self) -> None:
        """A silent module would pass every canary assertion vacuously."""
        self.assertTrue(
            any(line.strip() for line in self.handler.seen),
            "nothing was logged at all, so the canary assertions prove nothing",
        )

    async def test_tier1_block_logs_no_sender_or_text(self) -> None:
        mod = ChatModeration(_module_api(), _config())
        await mod.check_event_for_spam(_event(CANARY_TEXT))
        self.assertLoggedSomething()
        self.assertNoCanaries()

    async def test_tier1_block_log_still_names_room_and_rule(self) -> None:
        """Debuggability is the other half of the rule: an operator chasing a
        false positive must be able to tell which rule fired, and where."""
        mod = ChatModeration(_module_api(), _config())
        await mod.check_event_for_spam(_event(CANARY_TEXT))
        captured = "\n".join(self.handler.seen)
        self.assertIn("!room:example.org", captured)
        self.assertIn("contact_details", captured)

    async def test_tier1_failure_logs_no_exception_message(self) -> None:
        """An exception raised while matching routinely quotes the text that
        produced it, so the traceback is a channel for the message body."""
        mod = ChatModeration(_module_api(), _config())
        event = _event(CANARY_TEXT)

        def _boom(text: str, regions: Any) -> None:
            raise ValueError(f"cannot parse {text!r}")

        with patch("synapse_pangea_chat.moderation.check_text", side_effect=_boom):
            await mod.check_event_for_spam(event)

        self.assertLoggedSomething()
        self.assertNoCanaries()

    async def test_tier2_dispatch_failure_logs_no_exception_message(self) -> None:
        mod = ChatModeration(
            _module_api(),
            _config(
                moderation_tier1_enabled=False,
                moderation_tier2_enabled=True,
                moderation_choreo_base_url="http://choreo.invalid",
            ),
        )
        event = _event(CANARY_TEXT)

        with patch.object(
            ChatModeration,
            "_room_has_activity_plan",
            side_effect=ValueError(f"cannot dispatch {CANARY_SENDER!r}"),
        ):
            await mod.on_new_event(event, {})
        self.assertLoggedSomething()
        self.assertNoCanaries()

    async def test_redaction_failure_logs_no_matrix_id(self) -> None:
        """The channel that is not one of our own log calls. `_check_and_redact`
        runs under `run_as_background_process`, which calls `logger.exception`
        on anything that reaches it, and Synapse raises "User <mxid> not in
        room <room>" when a sender who has left tries to redact - which is the
        ordinary case in a DM the offender leaves. The exception must not
        escape this module."""
        api = _module_api()
        cast(AsyncMock, api.create_and_send_event_into_room).side_effect = RuntimeError(
            f"User {CANARY_SENDER} not in room !room:example.org"
        )
        mod = ChatModeration(
            api,
            _config(
                moderation_tier1_enabled=False,
                moderation_tier2_enabled=True,
                moderation_choreo_base_url="http://choreo.invalid",
            ),
        )
        with patch(
            "synapse_pangea_chat.moderation.moderate_text",
            new=_verdict(),
        ):
            # No exception may propagate: the caller is a background process
            # whose handler would log it, Matrix ID and all.
            await mod._check_and_redact(_event(CANARY_TEXT), "flagged text")
        # The redaction must have been ATTEMPTED. Without this the test passes
        # when the send never happens at all - a different failure, caught by
        # the same fail-open handler, proving nothing about this one.
        cast(AsyncMock, api.create_and_send_event_into_room).assert_awaited_once()
        self.assertLoggedSomething()
        self.assertNoCanaries()

    def test_sender_digest_is_stable_and_discriminating(self) -> None:
        """The digest has to be worth logging: the same sender must map to the
        same token within a process (so repeats group), and two senders must
        not collide (so the grouping means something)."""
        mod = ChatModeration(_module_api(), _config())
        first = mod._sender_digest(CANARY_SENDER)
        self.assertEqual(first, mod._sender_digest(CANARY_SENDER))
        self.assertNotEqual(first, mod._sender_digest("@other:example.org"))
        self.assertNotIn(CANARY_LOCALPART, first)

    def test_sender_digest_is_not_reversible_by_enumeration(self) -> None:
        """An unsalted hash of a Matrix ID is not an anonymisation: the space
        of local users is small enough to enumerate. Two module instances must
        therefore disagree on the digest of the same sender."""
        first = ChatModeration(_module_api(), _config())
        second = ChatModeration(_module_api(), _config())
        self.assertNotEqual(
            first._sender_digest(CANARY_SENDER),
            second._sender_digest(CANARY_SENDER),
        )


class ModerationLogContextTestCase(unittest.IsolatedAsyncioTestCase):
    """The channel an earlier revision excluded rather than closed.

    Synapse's log-record factory decorates EVERY record created inside a
    request with the sender's Matrix ID, so the Tier-1 block line carried it to
    every handler despite the digest that replaced it in the format string. The
    default formatter prints neither attribute, which is exactly why this
    survived review; a structured sink serialises the whole record.
    """

    def setUp(self) -> None:
        self.handler = _CapturingHandler()
        root = logging.getLogger()
        self._previous_level = root.level
        root.addHandler(self.handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, self.handler)
        self.addCleanup(root.setLevel, self._previous_level)

    def _moderation_records(self) -> List[logging.LogRecord]:
        return [
            record
            for record in self.handler.records
            if record.name.startswith("synapse.modules.synapse_pangea_chat.moderation")
        ]

    def assertScrubbed(self) -> None:
        records = self._moderation_records()
        self.assertTrue(records, "the module logged nothing, so this proves nothing")
        captured = "\n".join(self.handler.seen)
        for canary in (
            CANARY_SENDER,
            CANARY_LOCALPART,
            CANARY_TEXT,
            CANARY_TOKEN,
            CANARY_IP,
            CANARY_USER_AGENT,
        ):
            self.assertNotIn(
                canary,
                captured,
                f"{canary!r} reached a log handler; captured:\n{captured}",
            )

    async def test_tier1_block_inside_a_request_carries_no_identity(self) -> None:
        with _synapse_request_logcontext():
            mod = ChatModeration(_module_api(), _config())
            await mod.check_event_for_spam(_event(CANARY_TEXT))
        self.assertScrubbed()

    async def test_tier1_failure_inside_a_request_carries_no_identity(self) -> None:
        with _synapse_request_logcontext():
            mod = ChatModeration(_module_api(), _config())
            with patch(
                "synapse_pangea_chat.moderation.check_text",
                side_effect=ValueError(f"cannot parse {CANARY_TEXT!r}"),
            ):
                await mod.check_event_for_spam(_event(CANARY_TEXT))
        self.assertScrubbed()

    async def test_a_rule_failure_inside_a_request_carries_no_identity(self) -> None:
        """The sub-module loggers are the reason the scrubber is attached per
        logger rather than once at the package root: `callHandlers` inherits an
        ancestor's HANDLERS, never its filters."""
        with _synapse_request_logcontext():
            mod = ChatModeration(_module_api(), _config())
            with patch(
                "synapse_pangea_chat.moderation.tier1_prefilter"
                ".contains_phone_number",
                side_effect=ValueError(f"cannot parse {CANARY_TEXT!r}"),
            ):
                await mod.check_event_for_spam(_event(CANARY_TEXT))
        self.assertScrubbed()
        self.assertTrue(
            any(
                record.name.endswith("tier1_prefilter")
                for record in self._moderation_records()
            ),
            "the sub-module logger never emitted, so its scrubbing is untested",
        )

    async def test_tier2_redaction_failure_inside_a_request(self) -> None:
        api = _module_api()
        cast(AsyncMock, api.create_and_send_event_into_room).side_effect = RuntimeError(
            f"User {CANARY_SENDER} not in room !room:example.org"
        )
        with _synapse_request_logcontext():
            mod = ChatModeration(
                api,
                _config(
                    moderation_tier1_enabled=False,
                    moderation_tier2_enabled=True,
                    moderation_choreo_base_url="http://choreo.invalid",
                ),
            )
            with patch(
                "synapse_pangea_chat.moderation.moderate_text",
                new=_verdict(),
            ):
                await mod._check_and_redact(_event(CANARY_TEXT), "flagged text")
        cast(AsyncMock, api.create_and_send_event_into_room).assert_awaited_once()
        self.assertScrubbed()

    async def test_a_client_supplied_request_id_is_not_kept(self) -> None:
        """`get_request_id` returns `f"{method}-{sequence}"` unless the
        deployment sets `request_id_header`, in which case the HEADER's value
        is used verbatim - so a client can put its own Matrix ID on every
        record of its own request. The shipped `precise` formatter prints this
        field, so it leaks with no structured logging at all."""
        with _synapse_request_logcontext(request_id=f"PUT-{CANARY_SENDER}"):
            mod = ChatModeration(_module_api(), _config())
            await mod.check_event_for_spam(_event(CANARY_TEXT))
        self.assertScrubbed()

    async def test_a_handler_on_a_child_logger_is_scrubbed_too(self) -> None:
        """Walking upwards covers the handlers a record PROPAGATES to. A
        handler attached directly to `moderation.tier1_prefilter` is not on
        that path, and a record emitted through that child logger reaches it
        first of all."""
        child = logging.getLogger(
            "synapse.modules.synapse_pangea_chat.moderation.tier1_prefilter"
        )
        child_handler = _CapturingHandler()
        child_handler.addFilter(LoggingContextFilter())
        child.addHandler(child_handler)
        self.addCleanup(child.removeHandler, child_handler)
        with _synapse_request_logcontext():
            mod = ChatModeration(_module_api(), _config())
            with patch(
                "synapse_pangea_chat.moderation.tier1_prefilter.phonenumbers"
                ".PhoneNumberMatcher",
                side_effect=ValueError(f"cannot parse {CANARY_TEXT!r}"),
            ):
                await mod.check_event_for_spam(_event(CANARY_TEXT))
        captured = "\n".join(child_handler.seen)
        self.assertTrue(child_handler.records, "the child logger never emitted")
        for canary in (CANARY_SENDER, CANARY_LOCALPART, CANARY_TEXT):
            self.assertNotIn(canary, captured)

    async def test_the_record_stays_traceable_after_scrubbing(self) -> None:
        """The other half of the rule. Stripping the whole record would also
        pass every canary assertion and would leave an operator unable to tie a
        moderation decision to the request that produced it, so what a record
        must KEEP is asserted too."""
        with _synapse_request_logcontext():
            mod = ChatModeration(_module_api(), _config())
            await mod.check_event_for_spam(_event(CANARY_TEXT))
        record = self._moderation_records()[0]
        self.assertEqual(record.request, "PUT-1")
        self.assertEqual(record.server_name, "canary-server.invalid")
        self.assertEqual(record.method, "PUT")
        captured = "\n".join(self.handler.seen)
        self.assertIn("!room:example.org", captured)
        self.assertIn("contact_details", captured)

    async def test_a_handler_filter_cannot_put_the_identity_back(self) -> None:
        """Synapse still documents attaching `LoggingContextFilter` to a
        HANDLER, and `Handler.handle` runs its own filters after the logger's -
        so such a handler re-derives the sender's Matrix ID from the logcontext
        and puts it back on a record we had already cleaned."""
        self.handler.addFilter(LoggingContextFilter())
        with _synapse_request_logcontext():
            mod = ChatModeration(_module_api(), _config())
            await mod.check_event_for_spam(_event(CANARY_TEXT))
        self.assertScrubbed()

    def test_the_handler_scrubber_leaves_other_loggers_alone(self) -> None:
        """Scoped to this package on purpose: stripping the requester from
        SYNAPSE's request log is not ours to do, and it is the identity an
        operator relies on everywhere else."""
        with _synapse_request_logcontext():
            ChatModeration(_module_api(), _config())
            logging.getLogger("synapse.http.server").warning("someone else")
        other = [
            record
            for record in self.handler.records
            if record.name == "synapse.http.server"
        ]
        self.assertEqual(len(other), 1)
        self.assertEqual(other[0].requester, CANARY_SENDER)

    def test_the_scrubber_does_not_delete_the_attributes(self) -> None:
        """A formatter is free to reference `%(requester)s`, and a deployment
        that does would get a KeyError out of the logging system if the fix
        removed the attribute instead of replacing its value. Redaction, not
        deletion."""
        with _synapse_request_logcontext():
            logging.getLogger("synapse.modules.synapse_pangea_chat.moderation").warning(
                "probe"
            )
        record = self._moderation_records()[0]
        for attribute in ("requester", "authenticated_entity", "ip_address"):
            self.assertTrue(hasattr(record, attribute), attribute)
            self.assertNotIn(CANARY_LOCALPART, getattr(record, attribute))

    def test_records_outside_a_request_are_untouched(self) -> None:
        """No logcontext means no identity to scrub, and the scrubber must not
        invent attributes that were never there."""
        logging.getLogger("synapse.modules.synapse_pangea_chat.moderation").warning(
            "probe"
        )
        record = self._moderation_records()[0]
        self.assertFalse(hasattr(record, "requester"))


class ModerationLoggerCoverageTestCase(unittest.TestCase):
    """Every logger in the package carries the scrubber.

    The fix is only a fix if it is impossible to add a file that misses it, so
    this walks the package rather than naming the three loggers that exist
    today. A new module with a bare `logging.getLogger` fails here.
    """

    def test_every_module_logger_is_scrubbed(self) -> None:
        checked = []
        modules = [moderation_package]
        for info in pkgutil.iter_modules(moderation_package.__path__):
            modules.append(
                importlib.import_module(f"{moderation_package.__name__}.{info.name}")
            )
        for module in modules:
            for name, value in vars(module).items():
                if not isinstance(value, logging.Logger):
                    continue
                checked.append(f"{module.__name__}.{name}")
                self.assertTrue(
                    any(isinstance(f, _IdentityScrubbingFilter) for f in value.filters),
                    f"{module.__name__}.{name} ({value.name}) has no identity "
                    "scrubber; use log_safety.scrubbing_logger",
                )
        self.assertTrue(checked, "no loggers were found, so nothing was checked")


if __name__ == "__main__":
    unittest.main()
