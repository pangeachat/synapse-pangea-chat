"""The four limits of the two-tier design, and the code that makes each one
either closed or visible.

One file rather than four more classes in `test_moderation_unit.py`, because
these four share a subject: what the tiers CANNOT do, and what an operator can
see about it.

- **L1** Tier 2 redacted on a bare `flagged`, so `shit` typed by a learner and
  a targeted threat produced byte-identical verdicts. Severity now gates the
  redaction, per category.
- **L2** A blocked learner was told nothing. Tier 1 now returns a statement of
  reasons with the refusal.
- **L3** Neither tier can read an encrypted room, and nothing said so.
- **L4** The gap between a message appearing and its redaction was an anecdote
  rather than a number.
"""

import unittest
from typing import Any, Dict, Optional, cast

from synapse.events import EventBase
from synapse.module_api import NOT_SPAM, ModuleApi

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.moderation import ChatModeration

from .moderation_doubles import HomeServerDouble, MetricReader
from .moderation_doubles import module_api as module_api_double

MESSAGE_EVENTS = "pangea_moderation_message_events_total"


class FakeEvent:
    """The surfaces both callbacks read, and nothing else.

    A copy of `test_moderation_unit.FakeEvent` extended with the one field
    these tests need it to vary - the event TYPE - rather than an import of it:
    importing a helper out of a 4,500-line test module couples the two files'
    collection order for no gain.
    """

    def __init__(
        self,
        body: Optional[str] = None,
        *,
        event_type: str = "m.room.message",
        sender: str = "@learner:example.org",
        content: Optional[Dict[str, Any]] = None,
        event_id: str = "$evt1",
    ) -> None:
        self.type = event_type
        self.sender = sender
        self.room_id = "!room:example.org"
        self.event_id = event_id
        if content is not None:
            self.content = content
        elif body is None:
            self.content = {}
        else:
            self.content = {"msgtype": "m.text", "body": body}


def _event(*args: Any, **kwargs: Any) -> EventBase:
    return cast(EventBase, FakeEvent(*args, **kwargs))


def _encrypted_event(event_id: str = "$enc1", **kwargs: Any) -> EventBase:
    """A real-shaped `m.room.encrypted` event: a megolm envelope.

    The ciphertext is what the homeserver gets and the whole of what it gets -
    there is no `body` to read, which is the limit these tests are about.
    """
    return _event(
        event_type="m.room.encrypted",
        event_id=event_id,
        content={
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "AwgAEnB2b3UgY2Fubm90IHJlYWQgdGhpcw",
            "device_id": "DEVICEID",
            "sender_key": "senderkey",
            "session_id": "sessionid",
        },
        **kwargs,
    )


def _config(**overrides: Any) -> PangeaChatConfig:
    defaults: Dict[str, Any] = {
        "cms_base_url": "http://cms.invalid",
        "cms_service_api_key": "k",
        "moderation_tier1_enabled": True,
        "moderation_tier2_enabled": False,
    }
    defaults.update(overrides)
    return PangeaChatConfig(**defaults)


def _tier2_config(**overrides: Any) -> PangeaChatConfig:
    defaults: Dict[str, Any] = {
        "moderation_tier1_enabled": False,
        "moderation_tier2_enabled": True,
        "moderation_choreo_base_url": "http://choreo.invalid",
        "moderation_choreo_access_token": "syt_test",
    }
    defaults.update(overrides)
    return _config(**defaults)


def _tier1_module(config: Optional[PangeaChatConfig] = None) -> ChatModeration:
    return ChatModeration(module_api_double(), config or _config())


def _tier2_module(
    test: unittest.TestCase,
    config: Optional[PangeaChatConfig] = None,
    api: Optional[ModuleApi] = None,
    homeserver: Optional[HomeServerDouble] = None,
) -> ChatModeration:
    """Tier 2, running, and stopped when the test ends.

    The cleanup is not tidiness: a dispatcher left running parks two worker
    coroutines on Deferreds nothing will fire, and Synapse reports each of them
    as a lost logging context into whichever later test triggers the
    collection.
    """
    resolved_api = api if api is not None else module_api_double(homeserver)
    module = ChatModeration(resolved_api, config or _tier2_config())
    test.addCleanup(_stop_tier2, module)
    return module


def _stop_tier2(module: ChatModeration) -> None:
    dispatcher = module._dispatcher
    if dispatcher is None:
        return
    dispatcher._stopping = True
    dispatcher._wake_all()


class TestEncryptedRoomsAreCountedRatherThanSilent(unittest.IsolatedAsyncioTestCase):
    """L3. Synapse holds a megolm envelope and no plaintext, so neither tier
    can moderate an E2EE room at all.

    That limit is structural and is not being closed here. What WAS wrong is
    that it was silent: an operator who switched moderation on had no series
    that distinguished "this room is clean" from "we never read a word of it",
    and no way to put a number on the share of traffic in the second state.

    So both tiers count every message-bearing event they are offered, split by
    whether they could read it. The ratio
    `encrypted / (encrypted + plaintext)` is the answer to "what fraction of
    traffic is moderation blind to", and there was previously no pair of series
    from which it could be computed.
    """

    def setUp(self) -> None:
        self.reader = MetricReader()
        for tier in ("tier1", "tier2"):
            for encryption in ("plaintext", "encrypted"):
                self.reader.snapshot(MESSAGE_EVENTS, tier=tier, encryption=encryption)

    def _delta(self, tier: str, encryption: str) -> float:
        return self.reader.delta(MESSAGE_EVENTS, tier=tier, encryption=encryption)

    async def test_tier1_counts_an_encrypted_event_and_lets_it_through(self) -> None:
        """The blocking tier must never reject a message it cannot read - the
        sender did nothing wrong by encrypting - so the verdict is unchanged
        and only the counter moves."""
        module = _tier1_module()
        self.assertEqual(
            await module.check_event_for_spam(_encrypted_event()), NOT_SPAM
        )
        self.assertEqual(self._delta("tier1", "encrypted"), 1.0)
        self.assertEqual(self._delta("tier1", "plaintext"), 0.0)

    async def test_tier1_counts_a_plaintext_message_as_readable(self) -> None:
        """The denominator. Without it the encrypted counter is a number with
        nothing to divide by, and "what fraction" stays unanswerable."""
        module = _tier1_module()
        self.assertEqual(
            await module.check_event_for_spam(_event("hola, ¿qué tal?")), NOT_SPAM
        )
        self.assertEqual(self._delta("tier1", "plaintext"), 1.0)
        self.assertEqual(self._delta("tier1", "encrypted"), 0.0)

    async def test_tier1_counts_a_plaintext_message_it_blocks(self) -> None:
        """Counted on the way in, whatever the verdict turns out to be: the
        series says what the tier COULD READ, not what it decided."""
        module = _tier1_module()
        await module.check_event_for_spam(_event("call me: 415-555-2671"))
        self.assertEqual(self._delta("tier1", "plaintext"), 1.0)

    async def test_a_state_event_is_counted_under_neither(self) -> None:
        """`check_event_for_spam` sees every event on the homeserver. Counting
        topic changes and membership as readable traffic would swamp the
        denominator and make the ratio mean nothing."""
        module = _tier1_module()
        event = _event(event_type="m.room.topic", content={"topic": "Lesson 4"})
        self.assertEqual(await module.check_event_for_spam(event), NOT_SPAM)
        self.assertEqual(self._delta("tier1", "plaintext"), 0.0)
        self.assertEqual(self._delta("tier1", "encrypted"), 0.0)

    async def test_tier2_counts_an_encrypted_event_and_queues_nothing(self) -> None:
        """No moderation call is attempted on ciphertext. Sending it to the
        endpoint would spend a request on a base64 envelope and publish an
        encrypted room's traffic pattern to a third party."""
        module = _tier2_module(self)
        assert module._dispatcher is not None
        await module.on_new_event(_encrypted_event(), {})
        self.assertEqual(module._dispatcher.queue_depth, 0)
        self.assertEqual(self._delta("tier2", "encrypted"), 1.0)

    async def test_tier2_counts_a_plaintext_message_as_readable(self) -> None:
        module = _tier2_module(self)
        assert module._dispatcher is not None
        await module.on_new_event(_event("hola"), {})
        self.assertEqual(module._dispatcher.queue_depth, 1)
        self.assertEqual(self._delta("tier2", "plaintext"), 1.0)
        self.assertEqual(self._delta("tier2", "encrypted"), 0.0)

    async def test_an_exempt_sender_is_counted_under_neither(self) -> None:
        """An exempt sender is unmoderated for a reason that has nothing to do
        with encryption, and folding the two together would report a bot's
        traffic as a coverage gap E2EE caused."""
        module = _tier1_module(
            _config(moderation_exempt_user_id_globs=["@bot*:example.org"])
        )
        await module.check_event_for_spam(_event("hola", sender="@bot:example.org"))
        await module.check_event_for_spam(_encrypted_event(sender="@bot:example.org"))
        self.assertEqual(self._delta("tier1", "plaintext"), 0.0)
        self.assertEqual(self._delta("tier1", "encrypted"), 0.0)


if __name__ == "__main__":
    unittest.main()
