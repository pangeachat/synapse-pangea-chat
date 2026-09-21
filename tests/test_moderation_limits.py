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
from unittest.mock import AsyncMock, create_autospec, patch

from synapse.api.errors import Codes, SynapseError
from synapse.events import EventBase
from synapse.module_api import NOT_SPAM, ModuleApi

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.moderation import ChatModeration, refusal, severity
from synapse_pangea_chat.moderation.categories import (
    PROVIDER_CATEGORIES,
    UNKNOWN_CATEGORY,
)
from synapse_pangea_chat.moderation.choreo_client import moderate_text
from synapse_pangea_chat.moderation.dispatch import ModerationJob
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
    # A spelled-out run carrying a digit, and a compact leet token: the two
    # shapes Tier 1 blocks on. `v 1 t t u` stood here until `vittu` was demoted
    # for the French surname, so the fixture has to be a needle Tier 1 carries.
    "b 4 n g s a t",
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


MODERATE_TEXT = "synapse_pangea_chat.moderation.choreo_client.moderate_text"
CATEGORY_SCORE = "pangea_moderation_tier2_category_score"
SEVERITY_BASIS = "pangea_moderation_tier2_severity_basis_total"
REDACTION_SKIPPED = "pangea_moderation_tier2_redaction_skipped_total"
REDACTIONS = "pangea_moderation_tier2_redactions_total"
SUPPRESSED = "pangea_moderation_tier2_suppressed_total"


def _job(text: str = "x", **overrides: Any) -> ModerationJob:
    fields: Dict[str, Any] = {
        "event_id": "$evt1",
        "room_id": "!room:example.org",
        "sender": "@learner:example.org",
        "text": text,
        "enqueued_at": 0.0,
    }
    fields.update(overrides)
    return ModerationJob(**fields)


def _verdict(**overrides: Any) -> AsyncMock:
    """An autospec of the real `moderate_text`, not a bare `AsyncMock`.

    A bare mock accepts `access_t0ken=` as happily as `access_token=`, so a
    test written against one cannot see a caller passing the wrong keyword.
    """
    result: Dict[str, Any] = {
        "flagged": True,
        "categories": ["harassment"],
        "evaluated": True,
    }
    result.update(overrides)
    mock = create_autospec(moderate_text)
    mock.return_value = result
    return cast(AsyncMock, mock)


class TestSeverityGatesTheRedaction(unittest.IsolatedAsyncioTestCase):
    """L1. Tier 2 redacted on a bare `flagged`, so a learner typing `shit` and
    a targeted threat produced byte-identical verdicts and byte-identical
    outcomes.

    The evidence that separates them is `category_scores`, which the choreo
    handler now reports. These tests hold both halves of the compatibility
    contract at once: with the field, a per-category threshold decides; without
    it - which is staging's state today - the module behaves exactly as it did
    before any of this existed.
    """

    def setUp(self) -> None:
        self.reader = MetricReader()
        self.homeserver = HomeServerDouble()
        self.api = module_api_double(self.homeserver)

    def _module(self, **overrides: Any) -> ChatModeration:
        return _tier2_module(self, _tier2_config(**overrides), api=self.api)

    def _sent(self) -> Any:
        return cast(AsyncMock, self.api.create_and_send_event_into_room)

    async def _run(self, module: ChatModeration, verdict: AsyncMock) -> None:
        with patch(MODERATE_TEXT, verdict):
            await module._check_and_redact(_job("some message"))

    # --- With scores present ---------------------------------------------

    async def test_mild_swearing_is_left_standing(self) -> None:
        """The reported defect, end to end. `harassment` at 0.31 is what the
        provider returned for a learner typing `shit`; the default threshold
        for that category is 0.70, so the message stays."""
        self.reader.snapshot(REDACTION_SKIPPED, cause="below_threshold")
        module = self._module()
        await self._run(
            module,
            _verdict(categories=["harassment"], category_scores={"harassment": 0.31}),
        )
        self._sent().assert_not_awaited()
        self.assertEqual(
            self.reader.delta(REDACTION_SKIPPED, cause="below_threshold"), 1.0
        )

    async def test_targeted_abuse_is_still_redacted(self) -> None:
        """The same `flagged`, the same single category, three orders of
        magnitude apart. This is the distinction that did not exist."""
        module = self._module()
        await self._run(
            module,
            _verdict(categories=["harassment"], category_scores={"harassment": 0.993}),
        )
        self._sent().assert_awaited_once()

    async def test_a_score_exactly_at_the_threshold_redacts(self) -> None:
        """The comparison is `>=`, which is what makes a threshold of 0.0 mean
        "every flag of this category redacts"."""
        module = self._module()
        await self._run(
            module,
            _verdict(categories=["harassment"], category_scores={"harassment": 0.70}),
        )
        self._sent().assert_awaited_once()

    async def test_one_severe_category_redacts_a_verdict_of_several(self) -> None:
        """A message that is mildly rude AND a credible threat is a threat."""
        module = self._module()
        await self._run(
            module,
            _verdict(
                categories=["harassment", "harassment/threatening"],
                category_scores={"harassment": 0.2, "harassment/threatening": 0.45},
            ),
        )
        self._sent().assert_awaited_once()

    async def test_every_category_below_its_own_threshold_leaves_it_standing(
        self,
    ) -> None:
        module = self._module()
        await self._run(
            module,
            _verdict(
                categories=["harassment", "hate"],
                category_scores={"harassment": 0.2, "hate": 0.1},
            ),
        )
        self._sent().assert_not_awaited()

    async def test_the_child_safety_category_redacts_at_any_score(self) -> None:
        """`sexual/minors` is set to 0.0 and is not a trade-off this platform
        makes. A score low enough to clear every other category's bar still
        redacts here."""
        module = self._module()
        await self._run(
            module,
            _verdict(
                categories=["sexual/minors"], category_scores={"sexual/minors": 0.02}
            ),
        )
        self._sent().assert_awaited_once()

    async def test_a_threatening_subcategory_is_stricter_than_its_family(
        self,
    ) -> None:
        """The same score, two categories, two outcomes - which is the whole
        argument for per-category thresholds over one constant."""
        module = self._module()
        await self._run(
            module,
            _verdict(
                categories=["harassment/threatening"],
                category_scores={"harassment/threatening": 0.35},
            ),
        )
        self._sent().assert_awaited_once()
        self._sent().reset_mock()
        await self._run(
            module,
            _verdict(
                categories=["harassment"],
                category_scores={"harassment": 0.35},
                evaluated=True,
            ),
        )
        self._sent().assert_not_awaited()

    async def test_an_unrecognised_category_redacts_whatever_it_scored(self) -> None:
        """A name outside the documented vocabulary is not evidence of
        mildness, and a service must not get a softer disposition by inventing
        one. This is also the injection case: the value never reaches a label
        or a log line, only the `other` bucket."""
        module = self._module()
        await self._run(
            module,
            _verdict(
                categories=["@alice:example.org"],
                category_scores={"@alice:example.org": 0.0},
            ),
        )
        self._sent().assert_awaited_once()

    # --- Without scores: the un-upgraded choreo ---------------------------

    async def test_a_verdict_with_no_scores_redacts_exactly_as_before(self) -> None:
        """Staging's state today. The field is absent, there is no evidence
        the message is mild, and the decision is the one the module always
        took."""
        self.reader.snapshot(SEVERITY_BASIS, basis="no_scores")
        module = self._module()
        await self._run(module, _verdict(categories=["harassment"]))
        self._sent().assert_awaited_once()
        self.assertEqual(self.reader.delta(SEVERITY_BASIS, basis="no_scores"), 1.0)

    async def test_an_empty_score_map_redacts(self) -> None:
        module = self._module()
        await self._run(module, _verdict(categories=["harassment"], category_scores={}))
        self._sent().assert_awaited_once()

    async def test_a_null_score_map_redacts(self) -> None:
        module = self._module()
        await self._run(
            module, _verdict(categories=["harassment"], category_scores=None)
        )
        self._sent().assert_awaited_once()

    async def test_a_score_for_a_different_category_does_not_soften_this_one(
        self,
    ) -> None:
        """No family fallback. `harassment/threatening` is not `harassment`,
        and reading one's score for the other is the exact conflation the
        scores exist to end - so the tripped category is unscored and redacts.
        """
        module = self._module()
        await self._run(
            module,
            _verdict(
                categories=["harassment/threatening"],
                category_scores={"harassment": 0.01},
            ),
        )
        self._sent().assert_awaited_once()

    async def test_one_unscored_category_among_scored_ones_still_redacts(
        self,
    ) -> None:
        """Absence of evidence is not evidence of mildness, even beside real
        evidence of it."""
        module = self._module()
        await self._run(
            module,
            _verdict(
                categories=["harassment", "hate"],
                category_scores={"harassment": 0.01},
            ),
        )
        self._sent().assert_awaited_once()

    async def test_an_unusable_score_value_redacts(self) -> None:
        """Every shape a score can arrive in that we cannot read as a number
        in [0, 1]. Each of them is an unknown, and an unknown redacts - a
        service cannot talk this module out of a redaction with a malformed
        field."""
        values = ("0.1", None, True, float("nan"), float("inf"), -0.5, 1.5, [0.1])
        for index, value in enumerate(values):
            with self.subTest(score=repr(value)):
                self._sent().reset_mock()
                module = self._module()
                # A distinct event per case: the redaction claim is per event
                # id and idempotent, so reusing one would let the first case's
                # claim, not the score, decide every case after it.
                with patch(
                    MODERATE_TEXT,
                    _verdict(
                        categories=["harassment"],
                        category_scores={"harassment": value},
                    ),
                ):
                    await module._check_and_redact(
                        _job("some message", event_id=f"$unusable{index}")
                    )
                self._sent().assert_awaited_once()

    # --- Self-harm is untouched by any of it ------------------------------

    async def test_a_self_harm_disclosure_is_preserved_at_any_score(self) -> None:
        """The guarantee, re-asserted against the new branch. The preserve
        runs BEFORE severity is consulted, so a score that would clear every
        threshold in the table cannot reach the redaction path."""
        self.reader.snapshot(SUPPRESSED, category="self_harm")
        module = self._module()
        await self._run(
            module,
            _verdict(
                categories=["self-harm/intent"],
                category_scores={"self-harm/intent": 1.0},
            ),
        )
        self._sent().assert_not_awaited()
        self.assertEqual(self.reader.delta(SUPPRESSED, category="self_harm"), 1.0)

    async def test_a_severe_second_category_cannot_redact_a_disclosure(self) -> None:
        module = self._module()
        await self._run(
            module,
            _verdict(
                categories=["harassment", "self-harm/intent"],
                category_scores={"harassment": 1.0, "self-harm/intent": 0.01},
            ),
        )
        self._sent().assert_not_awaited()

    async def test_severity_is_never_consulted_for_a_preserved_verdict(self) -> None:
        """Stronger than the outcome: no score is even WEIGHED, so there is no
        threshold anybody could set that would participate in this decision."""
        self.reader.snapshot(SEVERITY_BASIS, basis="above_threshold")
        self.reader.snapshot(SEVERITY_BASIS, basis="below_threshold")
        self.reader.snapshot(SEVERITY_BASIS, basis="no_scores")
        module = self._module()
        await self._run(
            module,
            _verdict(categories=["self-harm"], category_scores={"self-harm": 0.999}),
        )
        for basis in ("above_threshold", "below_threshold", "no_scores"):
            self.assertEqual(self.reader.delta(SEVERITY_BASIS, basis=basis), 0.0)

    def test_a_threshold_cannot_be_configured_for_a_self_harm_category(self) -> None:
        """Refused rather than accepted-and-inert: a setting that appears to
        control the disposition of a disclosure would be a lie about what the
        module does."""
        for name in severity.PRESERVED_WIRE_CATEGORIES:
            with self.subTest(category=name):
                with self.assertRaises(ValueError) as caught:
                    severity.validate_thresholds({name: 0.9})
                self.assertIn("never redacted", str(caught.exception))

    def test_every_self_harm_subcategory_is_covered(self) -> None:
        """Derived from the vocabulary, so a sub-category the provider adds
        under `self-harm/` is refused the day the vocabulary learns it."""
        self.assertEqual(
            set(severity.PRESERVED_WIRE_CATEGORIES),
            {"self-harm", "self-harm/instructions", "self-harm/intent"},
        )

    # --- Observability ----------------------------------------------------

    async def test_the_score_that_drove_a_redaction_is_recorded(self) -> None:
        """An operator tunes the thresholds from this, so it has to carry the
        number and not just the outcome."""
        before = self.reader.value(
            CATEGORY_SCORE + "_sum", category="harassment", outcome="at_or_above"
        )
        module = self._module()
        await self._run(
            module,
            _verdict(categories=["harassment"], category_scores={"harassment": 0.93}),
        )
        after = self.reader.value(
            CATEGORY_SCORE + "_sum", category="harassment", outcome="at_or_above"
        )
        self.assertAlmostEqual(after - before, 0.93, places=6)

    async def test_the_score_that_spared_a_message_is_recorded_too(self) -> None:
        """Half a distribution cannot be tuned from. The messages left
        standing are precisely the ones an operator lowering a threshold needs
        to look at."""
        before = self.reader.value(
            CATEGORY_SCORE + "_sum", category="harassment", outcome="below"
        )
        module = self._module()
        await self._run(
            module,
            _verdict(categories=["harassment"], category_scores={"harassment": 0.31}),
        )
        after = self.reader.value(
            CATEGORY_SCORE + "_sum", category="harassment", outcome="below"
        )
        self.assertAlmostEqual(after - before, 0.31, places=6)

    async def test_every_weighed_category_is_observed_not_just_the_decider(
        self,
    ) -> None:
        counts = {
            category: self.reader.value(
                CATEGORY_SCORE + "_count", category=category, outcome="at_or_above"
            )
            for category in ("harassment", "hate")
        }
        module = self._module()
        await self._run(
            module,
            _verdict(
                categories=["harassment", "hate"],
                category_scores={"harassment": 0.99, "hate": 0.2},
            ),
        )
        for category in ("harassment", "hate"):
            after = self.reader.value(
                CATEGORY_SCORE + "_count", category=category, outcome="at_or_above"
            )
            self.assertEqual(after - counts[category], 1.0, category)

    async def test_the_basis_says_whether_thresholding_is_in_effect(self) -> None:
        """The rollout signal: a deployment sitting at 100% `no_scores` has
        thresholds configured and none of them doing anything."""
        for scores, expected in (
            ({"harassment": 0.99}, "above_threshold"),
            ({"harassment": 0.01}, "below_threshold"),
            (None, "no_scores"),
        ):
            with self.subTest(basis=expected):
                self.reader.snapshot(SEVERITY_BASIS, basis=expected)
                module = self._module()
                await self._run(
                    module,
                    _verdict(categories=["harassment"], category_scores=scores),
                )
                self.assertEqual(self.reader.delta(SEVERITY_BASIS, basis=expected), 1.0)

    async def test_a_repeated_category_is_weighed_once(self) -> None:
        """A response is data from a service we do not run, and the transport
        caps the body at 1 MiB rather than at a category count - so
        `["harassment"] * 75000` is a well-formed verdict. Weighing each
        occurrence meant one histogram observation per entry, inline on the
        reactor thread, at a repetition the sender chooses.

        Deduplicating is decision-preserving: the rule is `any` over the
        categories and `any` over a list equals `any` over its set. What it
        removes is the amplification.
        """
        before = self.reader.value(
            CATEGORY_SCORE + "_count", category="harassment", outcome="at_or_above"
        )
        module = self._module()
        await self._run(
            module,
            _verdict(
                categories=["harassment"] * 500,
                category_scores={"harassment": 0.99},
            ),
        )
        after = self.reader.value(
            CATEGORY_SCORE + "_count", category="harassment", outcome="at_or_above"
        )
        self.assertEqual(after - before, 1.0)
        self._sent().assert_awaited_once()

    def test_deduplication_does_not_change_any_decision(self) -> None:
        """The property the dedupe rests on, asserted directly rather than
        left to the argument above."""
        thresholds = severity.DEFAULT_CATEGORY_THRESHOLDS
        for categories, scores in (
            (["harassment"], {"harassment": 0.99}),
            (["harassment"], {"harassment": 0.01}),
            (["harassment", "hate"], {"harassment": 0.01, "hate": 0.99}),
            (["harassment", "hate"], {"harassment": 0.01, "hate": 0.01}),
            (["harassment", "hate"], {"hate": 0.01}),
            (["harassment"], None),
        ):
            with self.subTest(categories=categories):
                once = severity.decide(categories, scores, thresholds)
                thrice = severity.decide(categories * 3, scores, thresholds)
                self.assertEqual(once.redact, thrice.redact)
                self.assertEqual(once.basis, thrice.basis)
                self.assertEqual(once.weighed, thrice.weighed)
                self.assertEqual(once.driver, thrice.driver)

    async def test_a_category_label_never_carries_a_value_the_service_chose(
        self,
    ) -> None:
        """Cardinality, and the injection route with it. The label is the
        normalised name; an invented category lands in `other` or nowhere."""
        module = self._module()
        await self._run(
            module,
            _verdict(
                categories=["!room:example.org"],
                category_scores={"!room:example.org": 0.5},
            ),
        )
        self.assertEqual(
            self.reader.value(
                CATEGORY_SCORE + "_count",
                category="!room:example.org",
                outcome="at_or_above",
            ),
            0.0,
        )

    # --- Config -----------------------------------------------------------

    async def test_an_operator_can_move_a_threshold(self) -> None:
        module = self._module(moderation_tier2_category_thresholds={"harassment": 0.25})
        await self._run(
            module,
            _verdict(categories=["harassment"], category_scores={"harassment": 0.31}),
        )
        self._sent().assert_awaited_once()

    def test_a_partial_override_keeps_every_other_default(self) -> None:
        thresholds = severity.validate_thresholds({"harassment": 0.25})
        self.assertEqual(thresholds["harassment"], 0.25)
        self.assertEqual(
            {key: value for key, value in thresholds.items() if key != "harassment"},
            {
                key: value
                for key, value in severity.DEFAULT_CATEGORY_THRESHOLDS.items()
                if key != "harassment"
            },
        )

    def test_a_misspelled_category_is_refused_at_parse_time(self) -> None:
        """Silent otherwise: the default underneath keeps applying and nothing
        says the operator's change did nothing."""
        with self.assertRaises(ValueError) as caught:
            severity.validate_thresholds({"harrassment": 0.5})
        self.assertIn("not a category this provider reports", str(caught.exception))

    def test_an_out_of_range_or_non_numeric_threshold_is_refused(self) -> None:
        for value in (-0.1, 1.1, "0.5", True, None, float("nan")):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    severity.validate_thresholds({"harassment": value})

    def test_the_default_table_covers_every_category_that_can_be_redacted(
        self,
    ) -> None:
        """Derived from the vocabulary, so a category the provider adds cannot
        arrive with no threshold and fall through to a lookup that is not
        there."""
        redactable = {
            name
            for name in PROVIDER_CATEGORIES
            if name not in severity.PRESERVED_WIRE_CATEGORIES
        }
        self.assertEqual(
            redactable | {UNKNOWN_CATEGORY},
            set(severity.DEFAULT_CATEGORY_THRESHOLDS),
        )

    def test_child_safety_is_the_strictest_setting_in_the_table(self) -> None:
        """A property of the defaults rather than a restatement of one value:
        an edit that loosened `sexual/minors` past any other category fails
        here."""
        minors = severity.DEFAULT_CATEGORY_THRESHOLDS["sexual/minors"]
        for name, threshold in severity.DEFAULT_CATEGORY_THRESHOLDS.items():
            if name == UNKNOWN_CATEGORY:
                continue
            self.assertLessEqual(minors, threshold, name)


class TestTheVisibleWindowIsMeasured(unittest.IsolatedAsyncioTestCase):
    """L4. Tier 2 redacts after persist, so between a flagged message
    appearing and its redaction there is a window in which every member of the
    room can read it.

    That window is inherent to the design - closing it means blocking in the
    send path, which is the thing the queue and the worker pool exist to avoid
    - so it is not being eliminated. What was wrong is that its size was an
    anecdote: "about two seconds", measured once, on one machine, against a
    warm provider. It is now a histogram an operator can read a distribution
    off.
    """

    def setUp(self) -> None:
        self.reader = MetricReader()
        self.homeserver = HomeServerDouble()
        self.api = module_api_double(self.homeserver)

    async def test_the_window_is_measured_from_persist_to_redaction_sent(
        self,
    ) -> None:
        """End to end, not the provider call alone: the clock advances during
        the queue wait AND during the check, and the observation covers both
        plus the send."""
        clock = self.homeserver.clock
        module = _tier2_module(self, _tier2_config(), api=self.api)
        enqueued_at = clock.time()

        async def slow_check(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
            clock.now += 1.75
            return {"flagged": True, "categories": ["harassment"], "evaluated": True}

        before = self.reader.value(
            "pangea_moderation_tier2_redaction_window_seconds_sum"
        )
        count_before = self.reader.value(
            "pangea_moderation_tier2_redaction_window_seconds_count"
        )
        with patch(MODERATE_TEXT, side_effect=slow_check):
            # 0.5s of queue wait before a worker even picks it up.
            clock.now += 0.5
            await module._check_and_redact(_job("you suck", enqueued_at=enqueued_at))
        after = self.reader.value(
            "pangea_moderation_tier2_redaction_window_seconds_sum"
        )
        count_after = self.reader.value(
            "pangea_moderation_tier2_redaction_window_seconds_count"
        )
        self.assertEqual(count_after - count_before, 1.0)
        self.assertAlmostEqual(after - before, 2.25, places=6)

    async def test_a_message_that_is_not_redacted_is_not_in_the_window(
        self,
    ) -> None:
        """The series measures the visible window of a message that WAS taken
        down. Folding in the ones that were left standing - a clean verdict, a
        preserve, a score below threshold - would make the distribution
        describe something nobody is asking about."""
        count_before = self.reader.value(
            "pangea_moderation_tier2_redaction_window_seconds_count"
        )
        module = _tier2_module(self, _tier2_config(), api=self.api)
        with patch(
            MODERATE_TEXT,
            _verdict(
                categories=["self-harm/intent"],
                category_scores={"self-harm/intent": 0.9},
            ),
        ):
            await module._check_and_redact(_job("i want to hurt myself"))
        with patch(MODERATE_TEXT, _verdict(flagged=False, categories=[])):
            await module._check_and_redact(_job("hola", event_id="$evt2"))
        self.assertEqual(
            self.reader.value("pangea_moderation_tier2_redaction_window_seconds_count"),
            count_before,
        )
