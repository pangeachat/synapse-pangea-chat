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
from typing import Any, Dict, Optional, Tuple, cast

from synapse.api.errors import Codes, SynapseError
from synapse.events import EventBase
from synapse.module_api import NOT_SPAM, ModuleApi

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.moderation import ChatModeration, refusal
from synapse_pangea_chat.moderation.tier1_prefilter import (
    REASON_CONTACT_DETAILS,
    REASON_PROFANITY,
    RULE_REASONS,
)

from .moderation_doubles import HomeServerDouble, MetricReader
from .moderation_doubles import module_api as module_api_double

MESSAGE_EVENTS = "pangea_moderation_message_events_total"

# Texts that trip each Tier 1 rule. Several per rule, because the sharpest
# assertion this file makes is that the refusal is the SAME for all of them.
CONTACT_DETAILS_TEXTS = (
    "call me: 415-555-2671",
    "my number is +33 6 12 34 56 78",
    "text 212-555-0182 after class",
)
PROFANITY_TEXTS = (
    "v 1 t t u",
    "n1gger",
)


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


def _refusal(result: Any) -> Tuple[Codes, Dict[str, Any]]:
    """The refusal a Tier 1 block returns, type-checked the way Synapse does.

    `spamchecker_callbacks.py:385-395` tests exactly these four things before
    it will pass a module's answer through - a two-tuple, a real `Codes` and a
    real `dict` - and returns `Codes.FORBIDDEN, {}` for anything else. A test
    that unpacked the tuple without re-asserting them would go green on a
    return value Synapse throws away.
    """
    assert isinstance(result, tuple), f"not a tuple: {result!r}"
    assert len(result) == 2, f"not a two-tuple: {result!r}"
    code, body = result
    assert isinstance(code, Codes), f"not a Codes: {code!r}"
    assert isinstance(body, dict), f"not a dict: {body!r}"
    return code, body


class TestABlockedLearnerIsToldWhy(unittest.IsolatedAsyncioTestCase):
    """L2. Tier 1 returned a bare `Codes.FORBIDDEN`, so the learner got a red
    "failed to send" and no reason at all.

    DSA Art. 17 requires a statement of reasons on a restriction, including
    whether automated means were used, and the Santa Clara Principles ask for
    the same. The channel exists: the callback is typed
    `tuple[Codes, JsonDict]` and the dict reaches the client's error body.

    The constraint pulling the other way is that a refusal addressed to the
    sender is also a query they control. These tests hold both ends: a real
    sentence in the body, and no more information in it than which rule fired.
    """

    async def test_a_blocked_message_comes_back_with_a_reason(self) -> None:
        module = _tier1_module()
        code, body = _refusal(
            await module.check_event_for_spam(_event(CONTACT_DETAILS_TEXTS[0]))
        )
        self.assertEqual(code, Codes.FORBIDDEN)
        self.assertEqual(
            body["error"], refusal.DEFAULT_MESSAGES[REASON_CONTACT_DETAILS]
        )

    async def test_the_reason_says_a_machine_decided_it(self) -> None:
        """DSA Art. 17's automated-means limb, in both the places a client can
        read it: the sentence a person sees and a field a client can act on."""
        module = _tier1_module()
        _code, body = _refusal(
            await module.check_event_for_spam(_event(PROFANITY_TEXTS[0]))
        )
        self.assertIs(body[refusal.AUTOMATED_FIELD], True)
        self.assertIn("automatic", body["error"].lower())

    async def test_the_reason_names_the_rule_a_client_can_translate(self) -> None:
        """The reader is by definition still learning the language the
        sentence is written in, so the rule travels as a stable identifier
        beside it. It discloses what the sentence already discloses."""
        module = _tier1_module()
        _code, body = _refusal(
            await module.check_event_for_spam(_event(CONTACT_DETAILS_TEXTS[0]))
        )
        self.assertEqual(body[refusal.REASON_FIELD], REASON_CONTACT_DETAILS)

    async def test_a_clean_message_is_refused_nothing(self) -> None:
        module = _tier1_module()
        self.assertEqual(
            await module.check_event_for_spam(_event("hola, ¿qué tal?")), NOT_SPAM
        )

    # --- The evasion oracle, closed --------------------------------------

    async def test_every_text_that_trips_one_rule_gets_the_same_refusal(
        self,
    ) -> None:
        """The whole of the oracle argument, as one assertion per rule.

        A refusal that varied with the text - naming the term, quoting the
        span, even differing in length - would be a free query against the
        wordlist: send a candidate, read the answer, repeat. Identical bodies
        mean the response carries ZERO bits about the message beyond which of
        the two rules fired, which is the coarsest statement that is still a
        statement of reasons.
        """
        module = _tier1_module()
        for texts in (CONTACT_DETAILS_TEXTS, PROFANITY_TEXTS):
            bodies = []
            for text in texts:
                with self.subTest(text=text):
                    _code, body = _refusal(
                        await module.check_event_for_spam(_event(text))
                    )
                    bodies.append(body)
            self.assertEqual(
                bodies,
                [bodies[0]] * len(bodies),
                "the refusal varies with the blocked text",
            )

    async def test_the_refusal_body_is_exactly_the_constant_for_its_rule(
        self,
    ) -> None:
        """Whole-body equality against the constant, not a search for leaks.

        A substring scan is the weaker test and, on English prose, a wrong
        one: `num` from "my number is" appears in "phone number" by
        coincidence, not by disclosure. What actually has to hold is that the
        body is ASSEMBLED from constants - every key and every value drawn
        from the message table, the rule identifier and the boolean - so there
        is no expression anywhere in it that the message could reach. An extra
        key, an interpolated span or a message rebuilt per call fails here.
        """
        module = _tier1_module()
        for text, rule in [
            (text, REASON_CONTACT_DETAILS) for text in CONTACT_DETAILS_TEXTS
        ] + [(text, REASON_PROFANITY) for text in PROFANITY_TEXTS]:
            with self.subTest(text=text):
                _code, body = _refusal(await module.check_event_for_spam(_event(text)))
                self.assertEqual(
                    body, refusal.refusal_body(rule, refusal.DEFAULT_MESSAGES)
                )

    async def test_the_refusal_carries_no_digit_from_the_message(self) -> None:
        """The crisp half of the echo question.

        A phone number is digits, and digits are the one thing that could not
        arrive in a fixed English sentence by coincidence - so a body with no
        digit anywhere in it cannot have echoed one. The other half, a term
        from the wordlist, is its own test below.
        """
        module = _tier1_module()
        for text in CONTACT_DETAILS_TEXTS + PROFANITY_TEXTS:
            with self.subTest(text=text):
                _code, body = _refusal(await module.check_event_for_spam(_event(text)))
                rendered = " ".join(str(value) for value in body.values())
                self.assertFalse(
                    [ch for ch in rendered if ch.isdigit()],
                    f"a digit reached the refusal body: {rendered!r}",
                )

    async def test_the_refusal_names_no_term_from_the_wordlist(self) -> None:
        """A sentence that happened to contain a listed term would hand one
        out on every refusal, whatever the message said."""
        from synapse_pangea_chat.moderation.tier1_prefilter import check_text

        for message in refusal.DEFAULT_MESSAGES.values():
            with self.subTest(message=message):
                self.assertIsNone(check_text(message, ["US"]))

    async def test_the_two_rules_are_told_apart_and_nothing_finer_is(
        self,
    ) -> None:
        """The rule family IS disclosed - that is the requirement - and it is
        the only thing that is."""
        module = _tier1_module()
        _code, contact = _refusal(
            await module.check_event_for_spam(_event(CONTACT_DETAILS_TEXTS[0]))
        )
        _code, profanity = _refusal(
            await module.check_event_for_spam(_event(PROFANITY_TEXTS[0]))
        )
        self.assertNotEqual(contact["error"], profanity["error"])
        self.assertEqual(contact[refusal.REASON_FIELD], REASON_CONTACT_DETAILS)
        self.assertEqual(profanity[refusal.REASON_FIELD], REASON_PROFANITY)

    # --- The wire contract, against real Synapse -------------------------

    def test_the_reason_replaces_synapse_s_generic_sentence_in_the_body(
        self,
    ) -> None:
        """The one behaviour this whole feature rests on, pinned.

        `handlers/message.py` raises `SynapseError(403, "<fixed string>",
        code, dict)` and the fixed string is not ours. `SynapseError.error_dict`
        builds the body with `cs_error(msg, errcode, **additional_fields)`, and
        `cs_error` writes its keyword arguments OVER the dict it has already
        built - so `error` in our dict replaces Synapse's sentence. Real
        Synapse functions, so a version that reorders `cs_error` fails here
        rather than silently restoring "rejected as probable spam".
        """
        body = refusal.refusal_body(REASON_CONTACT_DETAILS, refusal.DEFAULT_MESSAGES)
        error = SynapseError(
            403,
            "This message has been rejected as probable spam",
            Codes.FORBIDDEN,
            body,
        ).error_dict(None)
        self.assertEqual(error["errcode"], "M_FORBIDDEN")
        self.assertEqual(
            error["error"], refusal.DEFAULT_MESSAGES[REASON_CONTACT_DETAILS]
        )
        self.assertIs(error[refusal.AUTOMATED_FIELD], True)

    # --- Config ----------------------------------------------------------

    def test_every_rule_has_a_message_of_its_own(self) -> None:
        """Derived from the rule table, so a rule added to
        `tier1_prefilter._RULES` without a sentence fails here rather than
        quietly falling back to the unspecific one."""
        for reason in RULE_REASONS:
            with self.subTest(rule=reason):
                self.assertIn(reason, refusal.DEFAULT_MESSAGES)
        self.assertIn(refusal.DEFAULT_KEY, refusal.DEFAULT_MESSAGES)

    async def test_an_operator_can_change_the_wording(self) -> None:
        module = _tier1_module(
            _config(
                moderation_tier1_refusal_messages={
                    REASON_PROFANITY: "No pasa nada, pero cambia esa palabra."
                }
            )
        )
        _code, body = _refusal(
            await module.check_event_for_spam(_event(PROFANITY_TEXTS[0]))
        )
        self.assertEqual(body["error"], "No pasa nada, pero cambia esa palabra.")

    def test_a_misspelled_rule_name_is_refused_at_parse_time(self) -> None:
        """Silent at runtime otherwise: the default underneath keeps working,
        so nothing says the operator's wording never took effect."""
        with self.assertRaises(ValueError) as caught:
            refusal.validate_messages({"profanity_": "..."})
        self.assertIn("is not a Tier 1 rule", str(caught.exception))

    def test_an_empty_or_oversized_message_is_refused(self) -> None:
        for value in ("", "   ", 7, "x" * (refusal.MAX_MESSAGE_LENGTH + 1)):
            with self.subTest(value=repr(value)[:40]):
                with self.assertRaises(ValueError):
                    refusal.validate_messages({REASON_PROFANITY: value})

    def test_an_unset_override_leaves_every_default_in_place(self) -> None:
        self.assertEqual(refusal.validate_messages(None), refusal.DEFAULT_MESSAGES)
        self.assertEqual(refusal.validate_messages({}), refusal.DEFAULT_MESSAGES)


if __name__ == "__main__":
    unittest.main()
