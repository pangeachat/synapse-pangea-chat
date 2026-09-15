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

Two channels these tests deliberately do NOT cover, because no module can
close them:

- Synapse's global `LoggingContextFilter` sets `requester` and
  `authenticated_entity` on EVERY log record emitted inside a request's
  logging context, ours included. The default formatter prints neither, but
  a structured-logging sink serialises the whole record.
- Synapse logs some authorisation failures itself, with the Matrix ID, before
  raising - `handle_new_client_event`'s "Denying new event" among them.

Both are homeserver-wide properties of Synapse's request logging rather than
anything this module writes, and they are recorded as limits in
moderation.instructions.md. The rule the tests enforce is the one the module
can keep: nothing IT passes to a logger carries a Matrix ID or message text.
"""

import logging
import unittest
from typing import Any, Dict, List, Optional, cast
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.events import EventBase

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.moderation import ChatModeration

# Strings that must never survive a moderation code path. Distinctive enough
# that a substring search cannot match them by accident.
CANARY_SENDER = "@canary-mxid-zqxj:canary-server.invalid"
CANARY_LOCALPART = "canary-mxid-zqxj"
CANARY_TEXT = "canary-body-vkwp call me: 415-555-2671"
CANARY_TOKEN = "syt_canary-token-hprm"


class _CapturingHandler(logging.Handler):
    """Records both the formatted message and the raw arguments.

    Formatting alone is not enough: a handler configured with a different
    formatter, or a structured-logging sink, would still serialise the
    arguments, so a leak that only shows up in `record.args` is a real leak.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.seen: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.seen.append(record.getMessage())
        except Exception:
            self.seen.append(str(record.msg))
        self.seen.append(repr(record.args))
        if record.exc_info is not None:
            self.seen.append(logging.Formatter().formatException(record.exc_info))
        # Anything the module attached with `extra=` lands in the record's
        # __dict__ and never appears in the formatted message, so a leak
        # through that door would be invisible to the two lines above.
        # Synapse's own LoggingContextFilter also writes `requester` and
        # `authenticated_entity` here on every record raised inside a
        # request - see the module docstring; that is not ours to remove,
        # and it is not set in these tests, which have no logcontext.
        self.seen.append(
            repr(
                {
                    key: value
                    for key, value in record.__dict__.items()
                    if key
                    not in logging.LogRecord("", 0, "", 0, "", None, None).__dict__
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
        mod = ChatModeration(MagicMock(), _config())
        await mod.check_event_for_spam(_event(CANARY_TEXT))
        self.assertLoggedSomething()
        self.assertNoCanaries()

    async def test_tier1_block_log_still_names_room_and_rule(self) -> None:
        """Debuggability is the other half of the rule: an operator chasing a
        false positive must be able to tell which rule fired, and where."""
        mod = ChatModeration(MagicMock(), _config())
        await mod.check_event_for_spam(_event(CANARY_TEXT))
        captured = "\n".join(self.handler.seen)
        self.assertIn("!room:example.org", captured)
        self.assertIn("contact_details", captured)

    async def test_tier1_failure_logs_no_exception_message(self) -> None:
        """An exception raised while matching routinely quotes the text that
        produced it, so the traceback is a channel for the message body."""
        mod = ChatModeration(MagicMock(), _config())
        event = _event(CANARY_TEXT)

        def _boom(text: str, regions: Any) -> None:
            raise ValueError(f"cannot parse {text!r}")

        with patch("synapse_pangea_chat.moderation.check_text", side_effect=_boom):
            await mod.check_event_for_spam(event)

        self.assertLoggedSomething()
        self.assertNoCanaries()

    async def test_tier2_dispatch_failure_logs_no_exception_message(self) -> None:
        mod = ChatModeration(
            MagicMock(),
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
        api = MagicMock()
        api.create_and_send_event_into_room = AsyncMock(
            side_effect=RuntimeError(
                f"User {CANARY_SENDER} not in room !room:example.org"
            )
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
            new=AsyncMock(
                return_value={
                    "flagged": True,
                    "categories": ["harassment"],
                    "evaluated": True,
                }
            ),
        ):
            # No exception may propagate: the caller is a background process
            # whose handler would log it, Matrix ID and all.
            await mod._check_and_redact(_event(CANARY_TEXT), "flagged text")
        self.assertLoggedSomething()
        self.assertNoCanaries()

    def test_sender_digest_is_stable_and_discriminating(self) -> None:
        """The digest has to be worth logging: the same sender must map to the
        same token within a process (so repeats group), and two senders must
        not collide (so the grouping means something)."""
        mod = ChatModeration(MagicMock(), _config())
        first = mod._sender_digest(CANARY_SENDER)
        self.assertEqual(first, mod._sender_digest(CANARY_SENDER))
        self.assertNotEqual(first, mod._sender_digest("@other:example.org"))
        self.assertNotIn(CANARY_LOCALPART, first)

    def test_sender_digest_is_not_reversible_by_enumeration(self) -> None:
        """An unsalted hash of a Matrix ID is not an anonymisation: the space
        of local users is small enough to enumerate. Two module instances must
        therefore disagree on the digest of the same sender."""
        first = ChatModeration(MagicMock(), _config())
        second = ChatModeration(MagicMock(), _config())
        self.assertNotEqual(
            first._sender_digest(CANARY_SENDER),
            second._sender_digest(CANARY_SENDER),
        )


if __name__ == "__main__":
    unittest.main()
