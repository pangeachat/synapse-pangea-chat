"""Unit tests for server-side chat moderation (Tier 1 pre-filter + callback
filtering logic). No Synapse process — ModuleApi is a signature-enforcing
double.

On test doubles, because the choice is what decides whether these tests can
fail at all. An unrestricted `MagicMock` accepts every call, so a test written
against one asserts that our code called SOMETHING, not that it called the
thing correctly: reverting `_background_process_args` to the 1.124 shape left
`test_plain_room_dispatches` green while, under the real runner on 1.159, the
event was passed where the callable belongs and Tier 2 never ran. That is the
production bug this chunk exists to fix, and the test could not see it.

So: `create_autospec` for `ModuleApi`, and `_BackgroundProcessDouble` for
`run_as_background_process` — the latter binds against the INSTALLED Synapse's
signature and then does what the real runner does with the result, which is the
only way a test on one pin can be right about both.
"""

import inspect
import json
import traceback
import unittest
from types import MappingProxyType, SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple, cast
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

from synapse.api.errors import Codes
from synapse.events import EventBase
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.module_api import NOT_SPAM, ModuleApi
from twisted.internet import defer
from twisted.internet.task import Clock
from twisted.python.failure import Failure
from twisted.web.client import ResponseDone
from twisted.web.iweb import IBodyProducer

from synapse_pangea_chat import PangeaChat
from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.moderation import (
    UNKNOWN_CATEGORY,
    ChatModeration,
    _background_process_args,
    _displayed_text,
    _normalize_category,
    _summarize_categories,
    tier1_prefilter,
)
from synapse_pangea_chat.moderation.choreo_client import (
    MAX_RESPONSE_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    ModerationCheckError,
    moderate_text,
)
from synapse_pangea_chat.moderation.tier1_prefilter import (
    REASON_CONTACT_DETAILS,
    REASON_LOCATION_DETAILS,
    REASON_PROFANITY,
    Tier1RuleError,
    check_text,
)
from synapse_pangea_chat.room_preview import PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE


class FakeEvent:
    def __init__(
        self,
        body: Optional[str] = None,
        msgtype: str = "m.text",
        sender: str = "@learner:example.org",
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
    """A stand-in carrying only the surfaces the module reads.

    The cast states that intent; building a real `EventBase` would drag in an
    event store for no gain, and the callbacks under test read `type`,
    `sender`, `room_id`, `event_id` and `content` and nothing else.
    """
    return cast(EventBase, FakeEvent(*args, **kwargs))


def _module_api() -> ModuleApi:
    """A `ModuleApi` double that checks the signature of every call.

    `_hs` is attached by hand because it is set in `ModuleApi.__init__` rather
    than declared on the class, so autospec cannot know about it.
    """
    api = create_autospec(ModuleApi, instance=True)
    api._hs = SimpleNamespace(hostname="example.org")
    return cast(ModuleApi, api)


class _BackgroundProcessDouble:
    """`run_as_background_process`, reduced to the contract a caller must meet.

    It binds the call against the signature of the Synapse actually installed,
    so a caller passing the wrong shape fails here exactly as it would fail
    there; it then checks the two things the real runner checks by using them -
    that `server_name` is a string, and that `func` is an awaitable callable it
    can invoke - and invokes it. Passing the event where the coroutine function
    belongs raises `TypeError: 'FakeEvent' object is not callable` under the
    real runner, so it raises here too.
    """

    def __init__(self) -> None:
        self.signature = inspect.signature(run_as_background_process)
        self.calls: List[Tuple[Tuple[Any, ...], Dict[str, Any]]] = []
        self.started: List[Tuple[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # Recorded BEFORE anything can reject it. `started` only holds the
        # calls that bound and ran, so asserting on `started` alone would read
        # a rejected call as no call at all - and the production callbacks
        # swallow what this raises, which would turn "we dispatched something
        # broken" into a passing skip assertion.
        self.calls.append((args, kwargs))
        bound = self.signature.bind(*args, **kwargs)
        desc = bound.arguments["desc"]
        func = bound.arguments["func"]
        extra = bound.arguments.get("args", ())
        forwarded = bound.arguments.get("kwargs", {})
        if "server_name" in self.signature.parameters:
            server_name = bound.arguments["server_name"]
            if not isinstance(server_name, str):
                raise TypeError(
                    "run_as_background_process wants a str server_name, got "
                    f"{type(server_name).__name__}"
                )
        if not callable(func):
            raise TypeError(f"{type(func).__name__} object is not callable")
        # The real runner awaits `func(*args, **kwargs)`, so the double calls
        # it the same way: a caller forwarding a keyword the coroutine does not
        # take fails here exactly as it fails there. What it does NOT do is
        # demand a coroutine FUNCTION - the runner accepts anything callable
        # whose result can be awaited, a plain function returning a `Deferred`
        # included, and a double stricter than the thing it stands for fails
        # correct code.
        coroutine = func(*extra, **forwarded)
        if not inspect.isawaitable(coroutine) and not isinstance(
            coroutine, defer.Deferred
        ):
            raise TypeError(
                f"{type(func).__name__} returned "
                f"{type(coroutine).__name__}, which cannot be awaited"
            )
        self.started.append((desc, coroutine))
        return coroutine

    async def drain(self) -> None:
        """Run everything that was dispatched, as the real runner would."""
        started, self.started = self.started, []
        for _desc, coroutine in started:
            await coroutine

    def discard(self) -> None:
        for _desc, coroutine in self.started:
            coroutine.close()
        self.started = []


def _config(**overrides: Any) -> PangeaChatConfig:
    defaults: Dict[str, Any] = {
        "cms_base_url": "http://cms.invalid",
        "cms_service_api_key": "k",
        "moderation_tier1_enabled": True,
        "moderation_tier2_enabled": False,
    }
    defaults.update(overrides)
    return PangeaChatConfig(**defaults)


def _moderation(config: PangeaChatConfig) -> ChatModeration:
    return ChatModeration(_module_api(), config)


class TestBackgroundProcessDouble(unittest.IsolatedAsyncioTestCase):
    """The double is test infrastructure, so it gets tested too.

    F8 was a double permissive enough that the production bug passed through
    it. A replacement double that is never itself exercised is the same defect
    one level up: nothing would notice if it stopped enforcing the contract it
    exists to enforce.
    """

    async def test_it_forwards_keyword_arguments_like_the_real_runner(self) -> None:
        seen: Dict[str, Any] = {}

        async def job(value: int, *, flag: bool = False) -> None:
            seen["value"] = value
            seen["flag"] = flag

        double = _BackgroundProcessDouble()
        args = _background_process_args(
            SimpleNamespace(hostname="example.org"), "desc", job
        )
        double(*args, 1, flag=True)
        await double.drain()
        self.assertEqual(seen, {"value": 1, "flag": True})

    async def test_it_rejects_a_keyword_the_coroutine_does_not_take(self) -> None:
        async def job(value: int) -> None:
            return None

        double = _BackgroundProcessDouble()
        args = _background_process_args(
            SimpleNamespace(hostname="example.org"), "desc", job
        )
        with self.assertRaises(TypeError):
            double(*args, 1, unsupported=True)

    def test_it_rejects_the_wrong_call_shape(self) -> None:
        double = _BackgroundProcessDouble()
        if "server_name" in double.signature.parameters:
            with self.assertRaises(TypeError):
                # The 1.124 shape on 1.159: the event lands where the callable
                # belongs, which is the production bug this chunk fixes.
                double("desc", _event("hi"), "text")

    def test_it_records_a_call_it_then_rejects(self) -> None:
        """`started` holds only the calls that ran, so a skip assertion written
        against it reads a rejected call as no call at all."""
        double = _BackgroundProcessDouble()
        with self.assertRaises(TypeError):
            double("desc", _event("hi"), "text")
        self.assertEqual(len(double.calls), 1)
        self.assertEqual(double.started, [])


class TestTier1Prefilter(unittest.TestCase):
    def test_us_phone_number_blocks(self) -> None:
        self.assertEqual(
            check_text("call me at (415) 555-2671", ["US"]), REASON_CONTACT_DETAILS
        )

    def test_international_phone_blocks_regardless_of_region(self) -> None:
        self.assertEqual(
            check_text("mon numéro est +33 6 12 34 56 78", ["US"]),
            REASON_CONTACT_DETAILS,
        )

    def test_street_address_blocks(self) -> None:
        self.assertEqual(
            check_text("meet me at 42 Maple Street after class", ["US"]),
            REASON_LOCATION_DETAILS,
        )

    def test_profanity_blocks(self) -> None:
        self.assertEqual(
            check_text("you are a fucking idiot", ["US"]), REASON_PROFANITY
        )

    def test_clean_multilingual_text_passes(self) -> None:
        self.assertIsNone(check_text("¿Quieres pedir la paella?", ["US"]))

    def test_bare_year_is_not_a_phone_number(self) -> None:
        self.assertIsNone(check_text("I was born in 2008 and I like soccer", ["US"]))

    def test_ordinary_number_plus_noun_is_not_an_address(self) -> None:
        self.assertIsNone(check_text("I have 3 dogs and 2 cats at home", ["US"]))


class _ExplodingPattern:
    """A compiled-pattern stand-in that fails the way a real one can."""

    def search(self, text: str) -> Any:
        raise RuntimeError("library quirk")


class TestTier1FailsOpenAsAWhole(unittest.IsolatedAsyncioTestCase):
    """A rule that cannot answer must not be read as a rule that answered no.

    The phone matcher's exception used to be caught inside
    `contains_phone_number`, which returned `False`; `check_text` then ran the
    address rule and returned `Codes.FORBIDDEN`. A message was therefore
    REJECTED on the strength of a Tier 1 run that had already failed, in a tier
    whose whole contract is that a failure lets the message through.
    """

    # The failure is injected at the LIBRARY boundary each rule sits on, not at
    # the rule function. Patching `contains_phone_number` itself replaces the
    # very code that used to swallow the exception, so restoring that swallow
    # would leave every one of these tests green - the defect would be back and
    # invisible. `PhoneNumberMatcher`, `_ADDRESS_RE` and the multilingual
    # matcher are where a real library quirk actually raises.
    RULE_PATCHES = (
        ("phonenumbers", "meet me at 42 Maple Street"),
        ("_ADDRESS_RE", "you are a fucking idiot"),
        ("_contains_profanity_multilingual", "an ordinary sentence"),
    )

    @staticmethod
    def _broken(target: str) -> Any:
        if target == "phonenumbers":
            return patch.object(
                tier1_prefilter.phonenumbers,
                "PhoneNumberMatcher",
                side_effect=RuntimeError("library quirk"),
            )
        if target == "_ADDRESS_RE":
            # `re.Pattern.search` is read-only, so the pattern OBJECT is
            # replaced. Still the boundary the rule sits on, and still outside
            # `contains_street_address`, which is the point.
            return patch.object(tier1_prefilter, "_ADDRESS_RE", _ExplodingPattern())
        return patch.object(
            tier1_prefilter, target, side_effect=RuntimeError("library quirk")
        )

    def test_any_failing_rule_aborts_the_whole_tier(self) -> None:
        for target, text in self.RULE_PATCHES:
            with self.subTest(rule=target):
                with self._broken(target):
                    with self.assertRaises(Tier1RuleError):
                        check_text(text, ["US"])

    async def test_a_failed_rule_never_produces_a_block(self) -> None:
        """The end-to-end shape of the same defect: the callback must allow."""
        for target, text in self.RULE_PATCHES:
            with self.subTest(rule=target):
                mod = _moderation(_config())
                with self._broken(target):
                    self.assertEqual(
                        await mod.check_event_for_spam(_event(text)), NOT_SPAM
                    )

    def test_a_working_tier_still_returns_every_reason(self) -> None:
        """The other half: failing open must not become failing always."""
        self.assertEqual(
            check_text("call 415-555-2671", ["US"]), REASON_CONTACT_DETAILS
        )
        self.assertEqual(
            check_text("meet me at 42 Maple Street", ["US"]), REASON_LOCATION_DETAILS
        )
        self.assertEqual(
            check_text("you are a fucking idiot", ["US"]), REASON_PROFANITY
        )


class TestCheckEventForSpam(unittest.IsolatedAsyncioTestCase):
    async def test_clean_message_not_spam(self) -> None:
        mod = _moderation(_config())
        self.assertEqual(
            await mod.check_event_for_spam(_event("hola, ¿cómo estás?")),
            NOT_SPAM,
        )

    async def test_phone_number_forbidden(self) -> None:
        mod = _moderation(_config())
        self.assertEqual(
            await mod.check_event_for_spam(_event("call me: 415-555-2671")),
            Codes.FORBIDDEN,
        )

    async def test_exempt_sender_skipped(self) -> None:
        mod = _moderation(
            _config(moderation_exempt_user_id_globs=["@bot*:example.org"])
        )
        event = _event("call me: 415-555-2671", sender="@bot:example.org")
        self.assertEqual(await mod.check_event_for_spam(event), NOT_SPAM)

    async def test_exempt_glob_does_not_exempt_a_longer_impostor(self) -> None:
        """EX-1. An exempt sender skips BOTH tiers, so a pattern that matches
        more than it names is a bypass, not a cosmetic bug. The regex
        predecessor of this glob was applied with `re.match`, which anchors
        only the start, and exempted this sender."""
        mod = _moderation(
            _config(moderation_exempt_user_id_globs=["@bot*:example.org"])
        )
        for impostor in (
            "@botimposter:example.org.evil.com",
            # The star-free half of the same hole: under `re.match` an exact
            # pattern still behaved as a prefix.
            "@bot:example.org.evil.com",
        ):
            with self.subTest(sender=impostor):
                event = _event("call me: 415-555-2671", sender=impostor)
                self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_exempt_glob_does_not_exempt_a_longer_localpart(self) -> None:
        """The same hole without the suffix: an exact glob must not act as a
        prefix. `@bot:example.org` is not `@bot2:example.org`."""
        mod = _moderation(_config(moderation_exempt_user_id_globs=["@bot:example.org"]))
        event = _event("call me: 415-555-2671", sender="@bot2:example.org")
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_exempt_sender_skips_tier2_as_well(self) -> None:
        """Both tiers consult the same matcher, so both are asserted."""
        mod = _moderation(
            _config(
                moderation_tier1_enabled=False,
                moderation_tier2_enabled=True,
                moderation_choreo_base_url="http://choreo.invalid",
                moderation_choreo_access_token="syt_x",
                moderation_exempt_user_id_globs=["@bot*:example.org"],
            )
        )
        background = _BackgroundProcessDouble()
        with patch(
            "synapse_pangea_chat.moderation.run_as_background_process", background
        ):
            await mod.on_new_event(_event("something", sender="@bot:example.org"), {})
            self.assertEqual(background.calls, [])
            await mod.on_new_event(
                _event("something", sender="@botimposter:example.org.evil.com"),
                {},
            )
            self.assertEqual(len(background.started), 1)
            background.discard()

    async def test_non_message_event_skipped(self) -> None:
        mod = _moderation(_config())
        event = _event(event_type="m.room.topic", content={"topic": "415-555-2671"})
        self.assertEqual(await mod.check_event_for_spam(event), NOT_SPAM)

    async def test_image_caption_is_moderated(self) -> None:
        """A caption is text the reader sees, so it is checked like any
        message (red-team finding: captions bypassed both tiers)."""
        mod = _moderation(_config())
        event = _event(content={"msgtype": "m.image", "body": "call 415-555-2671"})
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_benign_image_filename_passes(self) -> None:
        mod = _moderation(_config())
        event = _event(content={"msgtype": "m.image", "body": "beach-photo.jpg"})
        self.assertEqual(await mod.check_event_for_spam(event), NOT_SPAM)

    async def test_formatted_body_is_moderated(self) -> None:
        """The HTML twin can carry the payload while `body` looks innocuous."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "ok",
                "format": "org.matrix.custom.html",
                "formatted_body": "<b>call 415-555-2671</b>",
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_fails_open_on_internal_error(self) -> None:
        mod = _moderation(_config())
        with patch(
            "synapse_pangea_chat.moderation.check_text",
            side_effect=RuntimeError("boom"),
        ):
            self.assertEqual(
                await mod.check_event_for_spam(_event("anything")), NOT_SPAM
            )


class TestTier2Dispatch(unittest.IsolatedAsyncioTestCase):
    def _tier2_config(self, **overrides: Any) -> PangeaChatConfig:
        return _config(
            moderation_tier1_enabled=False,
            moderation_tier2_enabled=True,
            moderation_choreo_base_url="http://choreo.invalid",
            moderation_choreo_access_token="syt_test",
            **overrides,
        )

    def _verdict(self, **overrides: Any) -> AsyncMock:
        """An autospec of the real `moderate_text`, not a bare `AsyncMock`.

        A bare mock accepts `access_t0ken=` as happily as `access_token=`, so
        the tests that use it cannot see a caller passing the wrong keyword -
        which the real function rejects, inside the fail-open handler, on every
        message.
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

    def _outage(self, error: Exception) -> AsyncMock:
        mock = create_autospec(moderate_text)
        mock.side_effect = error
        return cast(AsyncMock, mock)

    async def test_activity_room_skipped(self) -> None:
        mod = _moderation(self._tier2_config())
        state = {(PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE, ""): MagicMock()}
        background = _BackgroundProcessDouble()
        with patch(
            "synapse_pangea_chat.moderation.run_as_background_process", background
        ):
            await mod.on_new_event(_event("you suck"), state)
            self.assertEqual(background.calls, [])

    async def test_plain_room_dispatches(self) -> None:
        """Driven all the way through to the redaction, on purpose.

        Asserting only that a mock was called is what let the dispatch-shape
        regression pass. Running the dispatched coroutine is what proves the
        arguments landed where the runner puts them.
        """
        api = _module_api()
        mod = ChatModeration(api, self._tier2_config())
        background = _BackgroundProcessDouble()
        with (
            patch(
                "synapse_pangea_chat.moderation.run_as_background_process", background
            ),
            patch(
                "synapse_pangea_chat.moderation.moderate_text", self._verdict()
            ) as moderate,
        ):
            await mod.on_new_event(_event("you suck"), {})
            self.assertEqual(len(background.started), 1)
            self.assertEqual(background.started[0][0], "pangea_moderation_tier2")
            await background.drain()
        moderate.assert_awaited_once()
        assert moderate.await_args is not None
        self.assertEqual(moderate.await_args.args[0], "you suck")
        cast(AsyncMock, api.create_and_send_event_into_room).assert_awaited_once()

    async def test_flagged_result_redacts_as_sender(self) -> None:
        api = _module_api()
        mod = ChatModeration(api, self._tier2_config())
        event = _event("threatening text", sender="@offender:example.org")
        with patch(
            "synapse_pangea_chat.moderation.moderate_text",
            self._verdict(categories=["self-harm/intent"]),
        ):
            await mod._check_and_redact(event, "threatening text")
        send = cast(AsyncMock, api.create_and_send_event_into_room)
        send.assert_awaited_once()
        await_args = send.await_args
        assert await_args is not None
        sent = await_args.args[0]
        self.assertEqual(sent["type"], "m.room.redaction")
        self.assertEqual(sent["sender"], "@offender:example.org")
        self.assertEqual(sent["redacts"], event.event_id)
        self.assertEqual(sent["content"]["redacts"], event.event_id)
        self.assertIn("self_harm", sent["content"]["reason"])

    async def test_unflagged_result_does_not_redact(self) -> None:
        api = _module_api()
        mod = ChatModeration(api, self._tier2_config())
        with patch(
            "synapse_pangea_chat.moderation.moderate_text",
            self._verdict(flagged=False, categories=[]),
        ):
            await mod._check_and_redact(_event("hi"), "hi")
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()

    async def test_moderation_outage_fails_open(self) -> None:
        from synapse_pangea_chat.moderation.choreo_client import ModerationCheckError

        api = _module_api()
        mod = ChatModeration(api, self._tier2_config())
        with patch(
            "synapse_pangea_chat.moderation.moderate_text",
            self._outage(ModerationCheckError("down")),
        ):
            await mod._check_and_redact(_event("hi"), "hi")
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()

    async def test_a_service_supplied_category_never_reaches_the_room(self) -> None:
        """The category is a free-form string chosen by a service we do not
        run, and it lands in a redaction reason that is visible in the room and
        in a log line. A compromised, misconfigured or merely buggy endpoint
        returning a Matrix ID, or a copy of the message, must not have it
        republished by us."""
        for category in (
            "@alice:example.org",
            "the private message body, verbatim",
            "harassment; rm -rf /",
            "",
        ):
            with self.subTest(category=category):
                api = _module_api()
                mod = ChatModeration(api, self._tier2_config())
                with patch(
                    "synapse_pangea_chat.moderation.moderate_text",
                    self._verdict(categories=[category]),
                ):
                    await mod._check_and_redact(_event("x"), "x")
                send = cast(AsyncMock, api.create_and_send_event_into_room)
                assert send.await_args is not None
                reason = send.await_args.args[0]["content"]["reason"]
                self.assertTrue(reason.endswith(f": {UNKNOWN_CATEGORY}"), reason)
                if category:
                    self.assertNotIn(category, reason)

    async def test_a_known_category_survives_an_unknown_one_beside_it(self) -> None:
        api = _module_api()
        mod = ChatModeration(api, self._tier2_config())
        with patch(
            "synapse_pangea_chat.moderation.moderate_text",
            self._verdict(categories=["@alice:example.org", "sexual/minors"]),
        ):
            await mod._check_and_redact(_event("x"), "x")
        send = cast(AsyncMock, api.create_and_send_event_into_room)
        assert send.await_args is not None
        self.assertTrue(send.await_args.args[0]["content"]["reason"].endswith("sexual"))


class TestExtraction(unittest.IsolatedAsyncioTestCase):
    """Extraction must never see less text than a reader sees.

    Every case here is a route by which it did, or could. They are one class,
    not eleven bugs: the moment the extractor returns a subset of the displayed
    text, a sender can arrange for that subset to be empty and skip both tiers
    on every message they send.
    """

    async def test_junk_new_content_cannot_hide_the_displayed_body(self) -> None:
        """The total bypass. `m.new_content` with no `m.relates_to` is not a
        replacement - the Matrix spec makes `rel_type: m.replace` the thing
        that defines one - so every client displays the outer `body`. Trusting
        the key on presence alone discarded that body and returned nothing, for
        any sender, on every message."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "call 415-555-2671",
                "m.new_content": {},
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_a_non_edit_relation_does_not_unlock_new_content(self) -> None:
        """A thread relation is a relation too, and it displays its own body."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "call 415-555-2671",
                "m.new_content": {"msgtype": "m.text", "body": "harmless"},
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$other"},
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_new_content_on_a_non_edit_is_not_moderated(self) -> None:
        """The permissive half of the relation rule, and the reason the rule is
        a rule rather than "always read both". No client renders
        `m.new_content` without `rel_type: m.replace`, so text parked there on
        a non-edit is text nobody sees, and Tier 1 must not block on it.

        The outer body is deliberately benign in every case: a fixture whose
        outer body already trips a rule proves nothing about whether the inner
        surface was read, because the block arrives either way.
        """
        for relates_to in (
            None,
            {"rel_type": "m.thread", "event_id": "$other"},
            {"event_id": "$other"},
            {"rel_type": "m.annotation", "key": "x"},
        ):
            with self.subTest(relates_to=relates_to):
                content: Dict[str, Any] = {
                    "msgtype": "m.text",
                    "body": "an ordinary sentence",
                    "m.new_content": {
                        "msgtype": "m.text",
                        "body": "call 415-555-2671",
                    },
                }
                if relates_to is not None:
                    content["m.relates_to"] = relates_to
                mod = _moderation(_config())
                self.assertEqual(
                    await mod.check_event_for_spam(_event(content=content)), NOT_SPAM
                )

    async def test_edit_moderates_replacement_text(self) -> None:
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "* innocuous",
                "m.new_content": {"msgtype": "m.text", "body": "call 415-555-2671"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"},
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_edit_also_moderates_the_fallback_body(self) -> None:
        """ADR-8a(0). An edit carries two displayed surfaces: `m.new_content`,
        which modern clients render, and the outer `body`, which older ones
        render. Picking one leaves the other unmoderated in whichever client
        renders it."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "* call 415-555-2671",
                "m.new_content": {"msgtype": "m.text", "body": "harmless now"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"},
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_a_missing_msgtype_on_one_surface_skips_nothing(self) -> None:
        """The msgtype gate is the same bypass in miniature if it is read off
        one surface only."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "* call 415-555-2671",
                "m.new_content": {"body": "harmless now"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"},
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_replacement_text_is_read_from_any_mapping(self) -> None:
        """Event content is not guaranteed to be a plain dict - Synapse's Rust
        event type and `use_frozen_dicts: true` both hand modules other
        mappings - and an `isinstance(..., dict)` test on one of those would
        silently stop moderating the replacement text of every edit."""
        mod = _moderation(_config())
        # BOTH surfaces are non-dict mappings. A fixture whose outer content is
        # a plain dict leaves the outer `isinstance(..., Mapping)` test
        # untested, and that is the one a real homeserver exercises on every
        # event: Synapse hands modules a Rust `JsonObject`, never a dict.
        event = _event(
            content=MappingProxyType(
                {
                    "msgtype": "m.text",
                    "body": "* a harmless correction",
                    "m.new_content": MappingProxyType(
                        {"msgtype": "m.text", "body": "call me: 415-555-2671"}
                    ),
                    "m.relates_to": MappingProxyType(
                        {"rel_type": "m.replace", "event_id": "$orig"}
                    ),
                }
            )
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_a_non_dict_mapping_outer_body_is_moderated(self) -> None:
        """The plain-message half of the same property."""
        mod = _moderation(_config())
        event = _event(
            content=MappingProxyType(
                {"msgtype": "m.text", "body": "call me: 415-555-2671"}
            )
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_entity_escaped_html_is_decoded_before_matching(self) -> None:
        """`&#52;15-555-2671` is displayed as a phone number. Matching the raw
        entity string means matching something no reader ever sees."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "ok",
                "format": "org.matrix.custom.html",
                "formatted_body": "<p>call &#52;15-555-2671 now</p>",
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_angle_brackets_in_text_do_not_eat_the_message(self) -> None:
        """`<415-555-2671 >` is displayed in full by every renderer: HTML5 says
        a `<` not followed by an ASCII letter is a character, not a tag opener.
        The regex stripper read it as a tag and deleted the number with it, so
        a sender could hide any payload by wrapping it in angle brackets."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "ok",
                "format": "org.matrix.custom.html",
                "formatted_body": "compare <415-555-2671 > with the other one",
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_displayed_attribute_text_is_moderated(self) -> None:
        """An image's `alt` is rendered whenever the image does not load and is
        read aloud by every screen reader, so it is displayed text."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "ok",
                "format": "org.matrix.custom.html",
                "formatted_body": '<img src="mxc://x/y" alt="call 415-555-2671">',
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_an_unterminated_tag_is_not_displayed_and_not_matched(self) -> None:
        """The permissive direction, corrected. An earlier revision appended
        the raw tail of an unterminated tag on the theory that over-reading is
        always the safe error. It is not: HTML5 ends the tokenizer inside the
        tag state at end-of-input and emits nothing, so every renderer agrees
        this is invisible - and Tier 1 blocks before persist, so matching it
        silences a learner over markup nobody can see."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "ok",
                "format": "org.matrix.custom.html",
                "formatted_body": "see <x call 415-555-2671",
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), NOT_SPAM)

    async def test_an_attachment_filename_is_moderated(self) -> None:
        """`filename` is the attachment's original name and clients show it
        beside the caption whenever the two differ, so it is displayed text."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.file",
                "body": "notes",
                "filename": "call 415-555-2671.txt",
                "url": "mxc://example.org/x",
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_an_abruptly_closed_comment_does_not_hide_the_message(
        self,
    ) -> None:
        """`<!-->` closes the comment at once under HTML5, so what follows is
        on screen. Python's parser reads it as an OPEN comment and scans for a
        later terminator, swallowing everything between."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "",
                "format": "org.matrix.custom.html",
                "formatted_body": "<!-->call 415-555-2671<!-- -->",
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_a_cdata_section_does_not_hide_the_message(self) -> None:
        """HTML5 has no CDATA in an HTML body: `<![CDATA[` starts a bogus
        comment that ends at the first `>`, so the rest is displayed. Python's
        parser swallows the whole section."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "",
                "format": "org.matrix.custom.html",
                "formatted_body": "<![CDATA[>call 415-555-2671]]>",
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_spoiler_and_maths_attributes_are_moderated(self) -> None:
        """Element renders the spoiler's reason and the LaTeX source, so both
        are text a reader receives."""
        for formatted in (
            '<span data-mx-spoiler="call 415-555-2671">safe</span>',
            '<span data-mx-maths="4155552671"></span>',
        ):
            with self.subTest(formatted=formatted):
                mod = _moderation(_config())
                event = _event(
                    content={
                        "msgtype": "m.text",
                        "body": "",
                        "format": "org.matrix.custom.html",
                        "formatted_body": formatted,
                    }
                )
                self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_an_attribute_no_renderer_displays_is_not_matched(self) -> None:
        """`alt` on a `<b>` is shown by nothing. Treating the attribute NAME as
        displayed text, wherever it appeared, made this message - which reads
        "hello" - a Tier-1 block."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "hello",
                "format": "org.matrix.custom.html",
                "formatted_body": '<b alt="415-555-2671">hello</b>',
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), NOT_SPAM)

    async def test_attribute_text_does_not_interrupt_the_visible_text(self) -> None:
        """Inline elements concatenate, so this reads `415-555-2671`. Splicing
        the attribute in where the tag stood broke the number in half."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "",
                "format": "org.matrix.custom.html",
                "formatted_body": '41<b title="notes">5</b>-555-2671',
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_script_and_style_content_is_not_matched(self) -> None:
        """Nobody reads a stylesheet, so matching one is a false positive with
        no upside."""
        for formatted in (
            "hello <script>call 415-555-2671</script>",
            "hello <style>/* call 415-555-2671 */</style>",
        ):
            with self.subTest(formatted=formatted):
                mod = _moderation(_config())
                event = _event(
                    content={
                        "msgtype": "m.text",
                        "body": "hello",
                        "format": "org.matrix.custom.html",
                        "formatted_body": formatted,
                    }
                )
                self.assertEqual(await mod.check_event_for_spam(event), NOT_SPAM)

    def test_block_tags_separate_and_inline_tags_do_not(self) -> None:
        """The two halves of "what the reader sees", asserted on the renderer
        itself. Inline elements concatenate - `4<b>1</b>5` is one number on
        screen - while block elements break the line, so gluing them together
        would invent text as surely as splitting a number destroys it."""
        self.assertEqual(_displayed_text("4<b>1</b>5"), "415")
        self.assertEqual(
            _displayed_text("<p>one</p><p>two</p>").split(), ["one", "two"]
        )
        self.assertIn("\n", _displayed_text("<p>one</p><p>two</p>"))
        self.assertEqual(_displayed_text("a<br>b").split(), ["a", "b"])

    async def test_an_unknown_msgtype_is_still_moderated(self) -> None:
        """The msgtype allowlist was the same bypass in a third shape. `body`
        is "a textual representation of the message" by definition, and a
        client renders it for a msgtype it does not recognise - so an allowlist
        of `m.text`/`m.image`/... handed every sender a one-field skip of both
        tiers."""
        for msgtype in ("m.not-a-real-type", "com.example.custom", "m.location"):
            with self.subTest(msgtype=msgtype):
                mod = _moderation(_config())
                event = _event(
                    content={"msgtype": msgtype, "body": "call 415-555-2671"}
                )
                self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_a_missing_msgtype_is_still_moderated(self) -> None:
        mod = _moderation(_config())
        event = _event(content={"body": "call 415-555-2671"})
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_a_non_string_body_is_still_moderated(self) -> None:
        """Malformed is not absent. `EventValidator` requires `body` to be a
        string only for the msgtypes it knows about, and a client that renders
        a non-string body renders `str(body)`."""
        for body in (4155552671, ["call 415-555-2671"], {"x": "415-555-2671"}):
            with self.subTest(body=body):
                mod = _moderation(_config())
                event = _event(content={"msgtype": "m.text", "body": body})
                self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_a_non_mapping_new_content_is_ignored_not_fatal(self) -> None:
        """The outer body must still be moderated whatever shape the junk
        takes, and nothing may raise out of extraction."""
        for junk in ("a string", ["a", "list"], 42, None):
            with self.subTest(junk=junk):
                mod = _moderation(_config())
                event = _event(
                    content={
                        "msgtype": "m.text",
                        "body": "call 415-555-2671",
                        "m.new_content": junk,
                        "m.relates_to": {"rel_type": "m.replace"},
                    }
                )
                self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_a_non_mapping_relates_to_is_ignored_not_fatal(self) -> None:
        for junk in ("m.replace", ["m.replace"], 0):
            with self.subTest(junk=junk):
                mod = _moderation(_config())
                event = _event(
                    content={
                        "msgtype": "m.text",
                        "body": "call 415-555-2671",
                        "m.relates_to": junk,
                    }
                )
                self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_ordinary_html_still_passes(self) -> None:
        """The other direction: an extractor that over-reads is a
        false-positive engine, and Tier 1 must stay the permissive tier."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "look at my notes",
                "format": "org.matrix.custom.html",
                "formatted_body": (
                    "<p>look at my <em>notes</em> &amp; tell me</p>"
                    '<a href="https://example.org/x">here</a>'
                ),
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), NOT_SPAM)


class _FakeTransport:
    """The transport an `Agent` response actually delivers.

    Deliberately shaped like twisted's `TransportProxyProducer`: `loseConnection`
    and `stopProducing`, and **no `abortConnection`**. That absence is the point
    - `readBody`'s canceller aborts only `if` the transport has one, so a double
    that grows an `abortConnection` would hide the leak the real one has.
    """

    disconnecting = False

    def __init__(self, underlying: Optional[Any] = None) -> None:
        self.stopped = False
        self.lost = False
        self.aborted = False
        if underlying is not None:
            # twisted's proxy keeps the real transport on `_producer`, and the
            # real transport is where `abortConnection` lives.
            self._producer = underlying

    def stopProducing(self) -> None:
        self.stopped = True

    def loseConnection(self) -> None:
        self.lost = True


class _RealTransport:
    """The socket under the proxy: the one with `abortConnection`."""

    def __init__(self) -> None:
        self.aborted = False

    def abortConnection(self) -> None:
        self.aborted = True


class _FakeResponse:
    """Just enough of `IResponse` for a body read, and no more."""

    version = (b"HTTP", 1, 1)
    phrase = b"OK"

    def __init__(
        self,
        code: int = 200,
        body: Optional[bytes] = None,
        chunks: Optional[List[bytes]] = None,
        underlying: Optional[Any] = None,
    ) -> None:
        self.code = code
        self._chunks = chunks if chunks is not None else ([body] if body else None)
        self.protocol: Any = None
        self.transport = _FakeTransport(underlying=underlying)

    def deliverBody(self, protocol: Any) -> None:
        self.protocol = protocol
        protocol.makeConnection(self.transport)
        if self._chunks is None:
            # The stall: headers arrived, the body never does and the
            # connection is never closed.
            return
        for chunk in self._chunks:
            protocol.dataReceived(chunk)
        protocol.connectionLost(Failure(ResponseDone()))


def _consume(body_producer: Any) -> bytes:
    """The bytes the agent would put on the wire, and the interface check.

    `IBodyProducer.providedBy` rather than a duck-type test: that is the
    contract `Agent.request` actually requires, so passing the raw `BytesIO`
    instead of a `FileBodyProducer` fails here as it would fail there.

    The bytes are read from the producer's file rather than by driving
    `startProducing`, which cooperates through the global reactor and would
    leave a pending delayed call behind in a test with no reactor running.
    """
    if body_producer is None:
        raise TypeError("Agent.request was given no body producer")
    if not IBodyProducer.providedBy(body_producer):
        raise TypeError(
            "Agent.request wants an IBodyProducer, got "
            f"{type(body_producer).__name__}"
        )
    source = body_producer._inputFile
    position = source.tell()
    try:
        source.seek(0)
        return bytes(source.read())
    finally:
        source.seek(position)


class _FakeAgent:
    """An `Agent` double that checks the request it was handed.

    An agent that accepts any arguments and returns a canned response tests the
    response handling and nothing else: the method, the URI and the auth header
    could all be wrong and every test would pass.
    """

    def __init__(
        self, response: _FakeResponse, headers_after: Optional[float] = None
    ) -> None:
        self.response = response
        self.headers_after = headers_after
        self.requests: List[Tuple[bytes, bytes, Any, Any]] = []
        self.sent_bodies: List[bytes] = []
        self.clock: Optional[Clock] = None

    def request(
        self, method: bytes, uri: bytes, headers: Any = None, bodyProducer: Any = None
    ) -> Any:
        if not isinstance(method, bytes) or not isinstance(uri, bytes):
            raise TypeError("Agent.request takes bytes for method and uri")
        # The producer is CONSUMED, the way a real agent consumes it. A double
        # that only stores it cannot tell a `FileBodyProducer` from the
        # `BytesIO` somebody passed by mistake, and never sees the payload -
        # so the request JSON could be renamed and every client test would
        # still pass.
        self.sent_bodies.append(_consume(bodyProducer))
        self.requests.append((method, uri, headers, bodyProducer))
        if self.headers_after is None:
            return defer.succeed(self.response)
        assert self.clock is not None
        deferred: Any = defer.Deferred()
        self.clock.callLater(self.headers_after, deferred.callback, self.response)
        return deferred


class _FailingAgent:
    """An agent whose request fails the way a transport failure does."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def request(self, *args: Any, **kwargs: Any) -> Any:
        return defer.fail(self.error)


class TestChoreoClient(unittest.TestCase):
    """The transport, against a clock we control.

    A `Clock` rather than the real reactor is what makes the stall test a test:
    with the global reactor the assertion would be "wait 15 seconds and hope",
    and with no injectable clock at all there is no way to assert the absence
    of a scheduled timeout, which is the actual defect.
    """

    def _call(self, agent: Any, clock: Clock) -> Tuple[Any, List[Any]]:
        deferred = defer.ensureDeferred(
            moderate_text(
                "some text",
                base_url="http://choreo.invalid",
                access_token="syt_x",
                reactor=clock,
                agent=agent,
            )
        )
        results: List[Any] = []
        deferred.addBoth(results.append)
        return deferred, results

    def test_a_stalled_response_body_is_timed_out(self) -> None:
        """`agent.request`'s deferred fires when the RESPONSE HEADERS arrive,
        so its timeout is spent by then. A peer that sends headers and then
        holds the body open leaves `readBody` pending with no timeout scheduled
        anywhere - the check never completes, never fails, and never logs, and
        they accumulate one per message."""
        clock = Clock()
        agent = _FakeAgent(_FakeResponse())
        _deferred, results = self._call(agent, clock)
        self.assertEqual(results, [], "the body read should still be pending")
        self.assertTrue(clock.getDelayedCalls(), "no timeout was ever scheduled")

        clock.advance(REQUEST_TIMEOUT_SECONDS + 1)
        self.assertEqual(len(results), 1, "the stalled read never completed")
        self.assertIsInstance(results[0], Failure)
        results[0].trap(ModerationCheckError)

    def test_the_whole_exchange_shares_one_budget(self) -> None:
        """The body's timeout is what REMAINS of the request's, not a fresh one.

        The headers have to arrive late for this to mean anything: if they
        arrive at clock zero, a shared deadline and a fresh full-length timeout
        expire at the same instant and the test cannot tell them apart.
        """
        clock = Clock()
        agent = _FakeAgent(_FakeResponse(), headers_after=REQUEST_TIMEOUT_SECONDS - 2)
        agent.clock = clock
        _deferred, results = self._call(agent, clock)
        clock.advance(REQUEST_TIMEOUT_SECONDS - 2)
        self.assertEqual(results, [], "the body read should have started")
        clock.advance(1.9)
        self.assertEqual(results, [], "the shared deadline has not expired yet")
        clock.advance(0.2)
        self.assertEqual(
            len(results),
            1,
            "the body read outlived the request's deadline, so the budget was "
            "restarted rather than shared",
        )
        results[0].trap(ModerationCheckError)

    def test_a_timeout_tears_the_connection_down(self) -> None:
        """Cancelling a `readBody` fires the deferred and leaves the socket
        open: its canceller aborts only `if` the transport has an
        `abortConnection`, and the transport an `Agent` response delivers
        (`TransportProxyProducer`) does not have one. So the check "times out"
        while the peer keeps sending."""
        clock = Clock()
        response = _FakeResponse()
        agent = _FakeAgent(response)
        _deferred, results = self._call(agent, clock)
        clock.advance(REQUEST_TIMEOUT_SECONDS + 1)
        results[0].trap(ModerationCheckError)
        self.assertTrue(response.transport.stopped, "the peer was not stopped")
        self.assertTrue(response.transport.lost, "the connection was left open")

    def test_a_cancelled_check_tears_the_connection_down(self) -> None:
        """A Deferred with no canceller fires on cancel and leaves the
        connection open with the body still arriving - the same defect the
        timeout exists to fix, reached through a different door. Shutdown and
        the per-job deadline both cancel in-flight checks."""
        clock = Clock()
        response = _FakeResponse()
        agent = _FakeAgent(response)
        deferred, results = self._call(agent, clock)
        self.assertEqual(results, [])
        deferred.cancel()
        self.assertEqual(len(results), 1)
        results[0].trap(ModerationCheckError)
        self.assertTrue(response.transport.stopped, "the peer was not stopped")
        self.assertTrue(
            response.transport.lost or response.transport.aborted,
            "the connection was left open",
        )

    def test_a_timeout_aborts_rather_than_closing_gracefully(self) -> None:
        """`loseConnection` is a GRACEFUL close: it waits for buffered writes
        and registered producers, so a peer refusing to read leaves the socket
        open and the check's deadline does not actually end the exchange.
        `abortConnection` does not wait. The proxy an `Agent` response delivers
        does not have it and the transport underneath does, so it is reached
        through when it is there."""
        clock = Clock()
        underlying = _RealTransport()
        response = _FakeResponse(underlying=underlying)
        agent = _FakeAgent(response)
        _deferred, results = self._call(agent, clock)
        clock.advance(REQUEST_TIMEOUT_SECONDS + 1)
        results[0].trap(ModerationCheckError)
        self.assertTrue(underlying.aborted, "the connection was closed gracefully")
        self.assertFalse(
            response.transport.lost,
            "a graceful close was used even though an abort was available",
        )

    def test_an_oversized_body_is_refused_rather_than_buffered(self) -> None:
        """`readBody` has no size limit at all, so a peer that streams forever
        fills the process's memory, and a deadline does not help - it fires the
        deferred and leaves the body arriving."""
        clock = Clock()
        chunk = b"x" * 65536
        response = _FakeResponse(
            chunks=[chunk] * (MAX_RESPONSE_BYTES // len(chunk) + 2)
        )
        agent = _FakeAgent(response)
        _deferred, results = self._call(agent, clock)
        self.assertEqual(len(results), 1)
        results[0].trap(ModerationCheckError)
        self.assertTrue(response.transport.lost)

    def test_a_body_exactly_at_the_limit_is_still_read(self) -> None:
        """At the limit, not near it. A 1 KiB body passes whether the
        comparison is `>` or `>=`, so it does not test the boundary at all."""
        prefix = b'{"flagged": false, "categories": [], "note": "'
        suffix = b'"}'
        padding = b"y" * (MAX_RESPONSE_BYTES - len(prefix) - len(suffix))
        body = prefix + padding + suffix
        self.assertEqual(len(body), MAX_RESPONSE_BYTES)
        clock = Clock()
        agent = _FakeAgent(_FakeResponse(body=body))
        _deferred, results = self._call(agent, clock)
        self.assertEqual(results[0]["flagged"], False)

    def test_a_body_one_byte_over_the_limit_is_refused(self) -> None:
        clock = Clock()
        response = _FakeResponse(body=b"x" * (MAX_RESPONSE_BYTES + 1))
        agent = _FakeAgent(response)
        _deferred, results = self._call(agent, clock)
        results[0].trap(ModerationCheckError)

    def test_the_request_is_the_one_the_endpoint_expects(self) -> None:
        """The agent double checks what it was handed, so a wrong method, path
        or missing bearer token fails here rather than passing silently."""
        clock = Clock()
        agent = _FakeAgent(_FakeResponse(body=b'{"flagged": false}'))
        _deferred, results = self._call(agent, clock)
        self.assertEqual(len(agent.requests), 1)
        method, uri, headers, body_producer = agent.requests[0]
        self.assertEqual(method, b"POST")
        self.assertEqual(uri, b"http://choreo.invalid/choreo/moderate")
        self.assertEqual(headers.getRawHeaders(b"Authorization"), [b"Bearer syt_x"])
        self.assertEqual(headers.getRawHeaders(b"Content-Type"), [b"application/json"])
        self.assertIsNotNone(body_producer)
        self.assertEqual(json.loads(agent.sent_bodies[0]), {"text": "some text"})

    def test_a_prompt_response_is_returned(self) -> None:
        """The other direction: the timeout must not break the happy path."""
        clock = Clock()
        agent = _FakeAgent(
            _FakeResponse(body=b'{"flagged": true, "categories": ["hate"]}')
        )
        _deferred, results = self._call(agent, clock)
        self.assertEqual(results, [{"flagged": True, "categories": ["hate"]}])
        self.assertEqual(
            clock.getDelayedCalls(), [], "a completed call left a timer behind"
        )

    def test_a_malformed_verdict_is_refused_at_the_boundary(self) -> None:
        """Shape is checked where the data enters, not one frame later. The
        caller goes on to put these values in a log line and in a redaction
        reason that lands in a room."""
        for body in (
            b'{"flagged": "yes"}',
            b'{"flagged": true, "categories": "harassment"}',
            b'{"flagged": true, "categories": [{"name": "hate"}]}',
            b'{"flagged": true, "categories": [null]}',
            b"[1, 2, 3]",
            b"not json at all",
        ):
            with self.subTest(body=body):
                clock = Clock()
                agent = _FakeAgent(_FakeResponse(body=body))
                _deferred, results = self._call(agent, clock)
                self.assertEqual(len(results), 1)
                self.assertIsInstance(results[0], Failure, results[0])
                results[0].trap(ModerationCheckError)

    def test_an_error_status_is_refused(self) -> None:
        clock = Clock()
        agent = _FakeAgent(_FakeResponse(code=502, body=b"{}"))
        _deferred, results = self._call(agent, clock)
        self.assertIsInstance(results[0], Failure)
        results[0].trap(ModerationCheckError)

    PAYLOAD = "@alice:example.org a raw copy of the private message body"

    def test_the_failure_carries_no_exception_chain_at_all(self) -> None:
        """ADR-10, and `from None` is not enough for it.

        `raise X from None` clears `__cause__` and stops the traceback being
        RENDERED with the original - it leaves `__context__` set, and the
        original is still hanging off the exception for anything that walks it.
        A structured log sink walks it, and a `json.JSONDecodeError` carries
        the whole response body on `.doc`, so the payload left the module
        attached to an error whose own message says only the type.
        """
        clock = Clock()
        agent = _FakeAgent(_FakeResponse(body=self.PAYLOAD.encode()))
        _deferred, results = self._call(agent, clock)
        error = results[0].value
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(self.PAYLOAD, self._everything_reachable_from(error))

    def test_a_transport_failure_carries_no_exception_chain_either(self) -> None:
        """The other failure site, and it has to be asserted separately: a
        decode error and a transport error are raised from two different
        `except` blocks, so a test that only drives one leaves the other free
        to keep the original exception - and its payload - on `__context__`."""
        clock = Clock()
        agent = _FailingAgent(ValueError(self.PAYLOAD))
        _deferred, results = self._call(agent, clock)
        error = results[0].value
        self.assertIsInstance(error, ModerationCheckError)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(self.PAYLOAD, self._everything_reachable_from(error))

    def test_an_undecodable_body_does_not_escape_the_module(self) -> None:
        """`json.loads` raises `UnicodeDecodeError` - not a subclass of
        `JSONDecodeError` - on a body that is not valid UTF-8. Catching only
        the latter let it out of the module entirely, into
        `run_as_background_process`, whose `logger.exception` printed the
        undecodable bytes from its args."""
        clock = Clock()
        body = b'{"flagged":true,"categories":["@alice:example.org"],"n":"\xff"}'
        agent = _FakeAgent(_FakeResponse(body=body))
        _deferred, results = self._call(agent, clock)
        self.assertIsInstance(results[0], Failure)
        error = results[0].value
        self.assertIsInstance(error, ModerationCheckError)
        self.assertNotIn("@alice:example.org", self._everything_reachable_from(error))

    @staticmethod
    def _everything_reachable_from(error: BaseException) -> str:
        """Everything a structured log sink could serialise off an exception:
        the message, the args, the rendered traceback, and every exception
        hanging off the cause and context chain, with their own attributes."""
        seen: List[str] = []
        current: Optional[BaseException] = error
        while current is not None:
            seen.append(repr(current.args))
            seen.append(repr(getattr(current, "__dict__", {})))
            seen.append(repr(getattr(current, "doc", "")))
            seen.append(
                "".join(
                    traceback.format_exception(
                        type(current), current, current.__traceback__
                    )
                )
            )
            current = current.__cause__ or current.__context__
        return "\n".join(seen)


class TestCallbackRegistration(unittest.TestCase):
    """Which callback goes on which hook, asserted.

    Every other test calls the two methods directly, so swapping them at
    registration - the post-persist observer wired into the pre-persist
    blocking hook, or the other way round - changed nothing any of them could
    see, while in production it would either block on a coroutine that never
    returns a verdict or never block at all.
    """

    def test_each_tier_registers_on_its_own_hook(self) -> None:
        api = _module_api()
        mod = ChatModeration(
            api,
            _config(
                moderation_tier1_enabled=True,
                moderation_tier2_enabled=True,
                moderation_choreo_base_url="http://choreo.invalid",
                moderation_choreo_access_token="syt_x",
            ),
        )
        api.register_spam_checker_callbacks.assert_called_once_with(
            check_event_for_spam=mod.check_event_for_spam
        )
        api.register_third_party_rules_callbacks.assert_called_once_with(
            on_new_event=mod.on_new_event
        )

    def test_a_disabled_tier_registers_nothing(self) -> None:
        api = _module_api()
        ChatModeration(api, _config(moderation_tier1_enabled=True))
        api.register_spam_checker_callbacks.assert_called_once()
        api.register_third_party_rules_callbacks.assert_not_called()


class TestExemptGlobContainer(unittest.TestCase):
    def test_a_bare_string_is_refused_rather_than_iterated(self) -> None:
        """A string is iterable. `"@bot:*"` iterated character by character
        yields a standalone `"*"` glob, which exempts every sender on every
        homeserver - so the container's type is checked before its items."""
        with self.assertRaises(ValueError):
            _moderation(_config(moderation_exempt_user_id_globs="@bot:*"))

    def test_a_list_of_globs_is_accepted(self) -> None:
        mod = _moderation(_config(moderation_exempt_user_id_globs=["@bot:*"]))
        self.assertTrue(mod._is_exempt_sender("@bot:example.org"))
        self.assertFalse(mod._is_exempt_sender("@bots:example.org"))


class TestNormalizeCategory(unittest.TestCase):
    def test_openai_names_map_to_orchestrator_vocabulary(self) -> None:
        self.assertEqual(_normalize_category("self-harm/intent"), "self_harm")
        self.assertEqual(_normalize_category("harassment/threatening"), "harassment")
        self.assertEqual(_normalize_category("hate"), "hate")
        self.assertEqual(_normalize_category("sexual/minors"), "sexual")

    def test_anything_outside_the_documented_list_becomes_other(self) -> None:
        for category in (
            "@alice:example.org",
            "a whole message body",
            "self-harm/invented",
            "",
            None,
            42,
            ["nested"],
        ):
            with self.subTest(category=category):
                self.assertEqual(_normalize_category(category), UNKNOWN_CATEGORY)

    def test_an_empty_category_list_is_labelled_rather_than_indexed(self) -> None:
        self.assertEqual(_summarize_categories([]), "flagged")


class TestParseConfig(unittest.TestCase):
    BASE = {"cms_base_url": "http://cms.invalid", "cms_service_api_key": "k"}

    def test_defaults_dark(self) -> None:
        cfg = PangeaChat.parse_config(dict(self.BASE))
        self.assertFalse(cfg.moderation_tier1_enabled)
        self.assertFalse(cfg.moderation_tier2_enabled)

    def test_a_misspelled_key_is_refused_rather_than_ignored(self) -> None:
        """Every key in this block turns moderation ON. A typo that parses
        cleanly leaves both tiers dark, registers no callback and logs nothing
        at any level, so the operator's next signal is an incident."""
        for typo in (
            {"tier1_enable": True},
            {"tier_1_enabled": True},
            {"tier2_enabled": True, "choreo_url": "https://c.invalid"},
            {"exempt_user_id_glob": ["@bot:*"]},
        ):
            with self.subTest(typo=typo):
                with self.assertRaises(ValueError) as caught:
                    PangeaChat.parse_config({**self.BASE, "moderation": typo})
                self.assertIn("unknown keys", str(caught.exception))

    def test_the_unknown_key_error_names_the_key_and_the_alternatives(self) -> None:
        with self.assertRaises(ValueError) as caught:
            PangeaChat.parse_config({**self.BASE, "moderation": {"tier1_enable": True}})
        message = str(caught.exception)
        self.assertIn("tier1_enable", message)
        self.assertIn("tier1_enabled", message)

    def test_every_documented_key_is_accepted(self) -> None:
        """The other half of the unknown-key gate: an accepted-key list that
        has drifted away from the parser rejects valid configuration."""
        cfg = PangeaChat.parse_config(
            {
                **self.BASE,
                "moderation": {
                    "tier1_enabled": True,
                    "tier1_phone_regions": ["US"],
                    "tier2_enabled": True,
                    "choreo_base_url": "https://choreo.invalid",
                    "choreo_access_token": "syt_x",
                    "exempt_user_id_globs": ["@bot:*"],
                    "redaction_reason_prefix": "Removed",
                },
            }
        )
        self.assertTrue(cfg.moderation_tier2_enabled)

    def test_tier2_requires_url_and_token(self) -> None:
        with self.assertRaises(ValueError):
            PangeaChat.parse_config(
                {**self.BASE, "moderation": {"tier2_enabled": True}}
            )
        with self.assertRaises(ValueError):
            PangeaChat.parse_config(
                {
                    **self.BASE,
                    "moderation": {
                        "tier2_enabled": True,
                        "choreo_base_url": "http://c.invalid",
                    },
                }
            )

    def test_an_unusable_choreo_url_is_refused_at_startup(self) -> None:
        """A non-empty string is not a URL. Each of these starts cleanly and
        then fails on every message inside the fail-open handler, which is
        indistinguishable from moderation being switched off."""
        for url in (
            "ftp://choreo.invalid",
            "choreo.invalid",
            "/choreo",
            "https://",
            "https://choreo.invalid/?token=x",
            "https://choreo.invalid/#frag",
            "https://user:pw@choreo.invalid",
            "gopher://choreo.invalid",
            # `urlparse` reports an empty query and an empty fragment for
            # these, both falsy, so a component check alone lets them through -
            # and the appended path then becomes `?/choreo/moderate`, or is
            # swallowed into a fragment, and every request goes to `/`.
            "https://choreo.invalid?",
            "https://choreo.invalid#",
            "https://choreo.inva lid",
            "https://choreo.invalid/a b",
            "https://chöreo.invalid",
            # `\r` is whitespace a hand-written list of " \t\n" misses, and
            # twisted refuses to build a URI from it.
            "https://choreo.invalid\r/a",
            "https://choreo.invalid/\x00x",
            # A non-empty authority with no host at all.
            "https://:443",
            # Empty userinfo: `username` and `password` are both falsy, and
            # twisted reads `@choreo.invalid` as the hostname.
            "https://@choreo.invalid",
            # `urlparse` raises only when `.port` is READ, which nothing did.
            "https://choreo.invalid:bad",
            "https://choreo.invalid:99999",
            "https://choreo.invalid:0",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    PangeaChat.parse_config(
                        {
                            **self.BASE,
                            "moderation": {
                                "tier2_enabled": True,
                                "choreo_base_url": url,
                                "choreo_access_token": "syt_x",
                            },
                        }
                    )

    def test_the_stored_url_is_the_validated_one(self) -> None:
        """Validating a stripped copy and storing the original is how
        `"https://host "` passed a check it did not satisfy - and then failed
        inside twisted's URI parsing on every message."""
        cfg = PangeaChat.parse_config(
            {
                **self.BASE,
                "moderation": {
                    "tier2_enabled": True,
                    "choreo_base_url": "  https://choreo.example.org/api  ",
                    "choreo_access_token": "syt_x",
                },
            }
        )
        self.assertEqual(
            cfg.moderation_choreo_base_url, "https://choreo.example.org/api"
        )

    def test_a_usable_choreo_url_is_accepted(self) -> None:
        for url in (
            "https://choreo.example.org",
            "https://choreo.example.org/",
            "https://choreo.example.org/api",
            "http://127.0.0.1:8080",
            "https://choreo.example.org:65535",
        ):
            with self.subTest(url=url):
                cfg = PangeaChat.parse_config(
                    {
                        **self.BASE,
                        "moderation": {
                            "tier2_enabled": True,
                            "choreo_base_url": url,
                            "choreo_access_token": "syt_x",
                        },
                    }
                )
                self.assertEqual(cfg.moderation_choreo_base_url, url)

    def test_plaintext_choreo_url_parses_with_a_warning(self) -> None:
        """A local stack legitimately runs over http; the token crossing the
        network in the clear is still worth saying out loud."""
        with self.assertLogs("synapse.modules.synapse_pangea_chat", "WARNING") as logs:
            PangeaChat.parse_config(
                {
                    **self.BASE,
                    "moderation": {
                        "tier2_enabled": True,
                        "choreo_base_url": "http://127.0.0.1:8080",
                        "choreo_access_token": "syt_x",
                    },
                }
            )
        self.assertIn("not https", "\n".join(logs.output))

    def test_retired_regex_key_is_refused_with_a_migration_message(self) -> None:
        """EX-3/EX-6. The old key is never reinterpreted: a value valid under
        both grammars would mean different things, so the operator restates
        it. The error names the value and suggests a glob."""
        with self.assertRaises(ValueError) as caught:
            PangeaChat.parse_config(
                {
                    **self.BASE,
                    "moderation": {"exempt_user_id_patterns": [r"@bot.*:example\.org"]},
                }
            )
        message = str(caught.exception)
        self.assertIn("exempt_user_id_globs", message)
        self.assertIn(repr(r"@bot.*:example\.org"), message)
        self.assertIn("@bot*:example.org", message)

    def test_both_grammars_value_under_the_old_key_is_still_refused(self) -> None:
        """EX-6. `@bot?:example.org` parses under both grammars and means
        different things in each; nothing inspects it, the key refuses it."""
        with self.assertRaises(ValueError):
            PangeaChat.parse_config(
                {
                    **self.BASE,
                    "moderation": {"exempt_user_id_patterns": ["@bot?:example.org"]},
                }
            )

    def test_regex_shaped_glob_is_rejected(self) -> None:
        """EX-4. A value written for the old grammar, pasted under the new
        key, is refused rather than matched as a glob."""
        with self.assertRaises(ValueError) as caught:
            PangeaChat.parse_config(
                {
                    **self.BASE,
                    "moderation": {"exempt_user_id_globs": [r"@bot.*:example\.org"]},
                }
            )
        self.assertIn("glob grammar", str(caught.exception))

    def test_character_class_glob_is_rejected(self) -> None:
        """EX-4. `[` and `]` are outside the documented grammar, so they are
        refused rather than silently read as a character class."""
        for value in ("@bot[0-9]:example.org", "@bot:example.org|evil.com"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    PangeaChat.parse_config(
                        {
                            **self.BASE,
                            "moderation": {"exempt_user_id_globs": [value]},
                        }
                    )

    def test_empty_glob_is_rejected(self) -> None:
        for value in ("", "   "):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    PangeaChat.parse_config(
                        {
                            **self.BASE,
                            "moderation": {"exempt_user_id_globs": [value]},
                        }
                    )

    def test_retired_regex_key_is_refused_even_when_null(self) -> None:
        """An operator who wrote the key with no value still believes an
        exemption policy is configured; accepting it silently would leave
        them believing it."""
        empty: List[str] = []
        for value in (None, empty):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    PangeaChat.parse_config(
                        {
                            **self.BASE,
                            "moderation": {"exempt_user_id_patterns": value},
                        }
                    )

    def test_unusable_phone_regions_are_refused(self) -> None:
        """Every wrong value here fails the same way and says nothing: the
        matcher loops over the regions it was given, finds no numbers, and the
        phone rule silently does not run. A shape check does not catch any of
        these - they are all lists of strings."""
        for regions in ([], ["us"], ["US "], ["USA"], ["ZZ"], ["US", "xx"]):
            with self.subTest(regions=regions):
                with self.assertRaises(ValueError):
                    PangeaChat.parse_config(
                        {
                            **self.BASE,
                            "moderation": {
                                "tier1_enabled": True,
                                "tier1_phone_regions": regions,
                            },
                        }
                    )

    def test_valid_phone_regions_are_accepted(self) -> None:
        cfg = PangeaChat.parse_config(
            {
                **self.BASE,
                "moderation": {"tier1_phone_regions": ["US", "FR", "GB"]},
            }
        )
        self.assertEqual(cfg.moderation_tier1_phone_regions, ["US", "FR", "GB"])

    def test_match_everything_glob_parses_with_a_warning(self) -> None:
        """EX-5. Exempting everyone is the operator's call to make; the
        warning is what makes it a deliberate one."""
        with self.assertLogs("synapse.modules.synapse_pangea_chat", "WARNING") as logs:
            cfg = PangeaChat.parse_config(
                {**self.BASE, "moderation": {"exempt_user_id_globs": ["*"]}}
            )
        self.assertEqual(cfg.moderation_exempt_user_id_globs, ["*"])
        self.assertIn("every sender", "\n".join(logs.output))

    def test_full_config_parses(self) -> None:
        cfg = PangeaChat.parse_config(
            {
                **self.BASE,
                "moderation": {
                    "tier1_enabled": True,
                    "tier1_phone_regions": ["US", "FR"],
                    "tier2_enabled": True,
                    "choreo_base_url": "http://choreo.invalid",
                    "choreo_access_token": "syt_x",
                    "exempt_user_id_globs": ["@bot:*"],
                },
            }
        )
        self.assertTrue(cfg.moderation_tier1_enabled)
        self.assertEqual(cfg.moderation_tier1_phone_regions, ["US", "FR"])
        self.assertTrue(cfg.moderation_tier2_enabled)


if __name__ == "__main__":
    unittest.main()
