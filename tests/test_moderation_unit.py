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

import asyncio
import inspect
import json
import logging
import time
import traceback
import unittest
from types import MappingProxyType, SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple, cast
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

from synapse.api.errors import Codes
from synapse.events import EventBase
from synapse.logging.context import LoggingContext, PreserveLoggingContext
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.module_api import NOT_SPAM, ModuleApi
from twisted.internet import defer
from twisted.internet.error import ConnectionAborted
from twisted.internet.task import Clock
from twisted.python.failure import Failure
from twisted.web._newclient import ResponseNeverReceived
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
from synapse_pangea_chat.moderation.dispatch import ModerationJob
from synapse_pangea_chat.moderation.disposition import DISPOSITION_TABLE, STATEMENTS
from synapse_pangea_chat.moderation.tier1_prefilter import (
    REASON_CONTACT_DETAILS,
    REASON_PROFANITY,
    Tier1RuleError,
    check_text,
)
from synapse_pangea_chat.room_preview import PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE

from .moderation_doubles import (
    DbPoolDouble,
    EventStoreDouble,
    HomeServerDouble,
    MetricReader,
)
from .moderation_doubles import module_api as module_api_double


class FakeEvent:
    def __init__(
        self,
        body: Optional[str] = None,
        msgtype: str = "m.text",
        sender: str = "@learner:example.org",
        event_type: str = "m.room.message",
        content: Optional[Dict[str, Any]] = None,
        event_id: str = "$evt1",
    ):
        self.type = event_type
        self.sender = sender
        self.room_id = "!room:example.org"
        self.event_id = event_id
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


def _module_api(
    homeserver: Optional[HomeServerDouble] = None,
    *,
    run_background_tasks: bool = True,
) -> ModuleApi:
    """A `ModuleApi` double that checks the signature of every call.

    See `tests/moderation_doubles.py`; the homeserver surface it carries is
    shared with the other moderation test modules rather than copied, because
    the part that matters - a clock whose `call_later` does not run inline -
    is exactly what a second copy would get subtly wrong.
    """
    return module_api_double(homeserver, run_background_tasks=run_background_tasks)


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
        for _desc, started in self.started:
            # A `Deferred` is an accepted return, and it has no `close`.
            close = getattr(started, "close", None)
            if close is not None:
                close()
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


def _tier2_config(**overrides: Any) -> PangeaChatConfig:
    return _config(
        moderation_tier1_enabled=False,
        moderation_tier2_enabled=True,
        moderation_choreo_base_url="http://choreo.invalid",
        moderation_choreo_access_token="syt_test",
        **overrides,
    )


def _tier2_module(
    test: unittest.TestCase, api: ModuleApi, config: PangeaChatConfig
) -> ChatModeration:
    """A `ChatModeration` with Tier 2 running, stopped when the test ends.

    The cleanup is not tidiness. A dispatcher left running holds two worker
    coroutines parked on Deferreds that nothing will ever fire, and Synapse
    reports every one of them as "Expected logging context ... was lost" when
    they are collected - noise that would land in whichever test happened to
    trigger the collection.
    """
    module = ChatModeration(api, config)
    test.addCleanup(_stop_tier2, module)
    return module


def _stop_tier2(module: ChatModeration) -> None:
    dispatcher = module._dispatcher
    if dispatcher is None:
        return
    dispatcher._stopping = True
    dispatcher._wake_all()


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
        """Asserted on whichever Synapse is installed, never skipped.

        On 1.159 the 1.124 shape puts the event where the callable belongs -
        the production bug this chunk fixes. On 1.124 the 1.159 shape does the
        same thing in the other direction. A test that asserts only under one
        `if` proves nothing on the other pin.
        """
        double = _BackgroundProcessDouble()
        if "server_name" in double.signature.parameters:
            wrong_shape: Tuple[Any, ...] = ("desc", _event("hi"), "text")
        else:
            wrong_shape = ("desc", "example.org", _event("hi"), "text")
        with self.assertRaises(TypeError):
            double(*wrong_shape)

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

    def test_profanity_blocks(self) -> None:
        self.assertEqual(check_text("you are a motherfucker", ["US"]), REASON_PROFANITY)

    def test_clean_multilingual_text_passes(self) -> None:
        self.assertIsNone(check_text("¿Quieres pedir la paella?", ["US"]))

    def test_bare_year_is_not_a_phone_number(self) -> None:
        self.assertIsNone(check_text("I was born in 2008 and I like soccer", ["US"]))

    def test_an_address_is_not_a_tier1_block(self) -> None:
        """Addresses are Tier 2's to judge.

        A pattern cannot tell a shared address from a discussed one, and Tier 1
        rejects before persist, so each of these was an innocent learner
        silenced: landmarks are a stock topic in a language-learning room and
        naming where you live is an A1 lesson.
        """
        for text in (
            "10 Downing Street is where the Prime Minister lives",
            "The White House is at 1600 Pennsylvania Avenue",
            "I live at 42 Maple Street",
            "meet me at 42 Maple Street after class",
        ):
            with self.subTest(text=text):
                self.assertIsNone(check_text(text, ["US"]))


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
    # invisible. `PhoneNumberMatcher` and the universal-term matcher are
    # where a real library quirk actually raises.
    RULE_PATCHES = (
        ("phonenumbers", "call 415-555-2671"),
        ("_matches_universal_term", "an ordinary sentence"),
    )

    @staticmethod
    def _broken(target: str) -> Any:
        if target == "phonenumbers":
            return patch.object(
                tier1_prefilter.phonenumbers,
                "PhoneNumberMatcher",
                side_effect=RuntimeError("library quirk"),
            )
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
        self.assertEqual(check_text("you are a motherfucker", ["US"]), REASON_PROFANITY)


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
        mod = _tier2_module(
            self,
            _module_api(),
            _config(
                moderation_tier1_enabled=False,
                moderation_tier2_enabled=True,
                moderation_choreo_base_url="http://choreo.invalid",
                moderation_choreo_access_token="syt_x",
                moderation_exempt_user_id_globs=["@bot*:example.org"],
            ),
        )
        assert mod._dispatcher is not None
        await mod.on_new_event(_event("something", sender="@bot:example.org"), {})
        self.assertEqual(mod._dispatcher.queue_depth, 0)
        await mod.on_new_event(
            _event(
                "something",
                sender="@botimposter:example.org.evil.com",
                event_id="$other",
            ),
            {},
        )
        self.assertEqual(mod._dispatcher.queue_depth, 1)

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


# The moderation call now happens inside `ChoreoChecker`, which lives in
# `choreo_client`, so that is where a test patches it. Patching a name that
# `moderation/__init__.py` no longer imports would silently patch nothing.


class TestTier2Dispatch(unittest.IsolatedAsyncioTestCase):
    def _job(self, text: str = "x", **overrides: Any) -> ModerationJob:
        fields: Dict[str, Any] = {
            "event_id": "$evt1",
            "room_id": "!room:example.org",
            "sender": "@learner:example.org",
            "text": text,
            "enqueued_at": 0.0,
        }
        fields.update(overrides)
        return ModerationJob(**fields)

    def _tier2_config(self, **overrides: Any) -> PangeaChatConfig:
        return _tier2_config(**overrides)

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
        """Asserted on the QUEUE, which is the route production takes.

        It used to be asserted on `run_as_background_process`, which Tier 2
        no longer dispatches through at all - so replacing the activity-room
        check with an unconditional `False` left the test green while every
        activity room was double-moderated.
        """
        homeserver = HomeServerDouble()
        api = _module_api(homeserver)
        mod = _tier2_module(self, api, self._tier2_config())
        assert mod._dispatcher is not None
        state = {(PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE, ""): MagicMock()}
        with patch(MODERATE_TEXT, self._verdict()) as moderate:
            await mod.on_new_event(_event("you suck"), state)
            self.assertEqual(mod._dispatcher.queue_depth, 0)
            homeserver.clock.drain()
        moderate.assert_not_awaited()
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()

    async def test_plain_room_dispatches(self) -> None:
        """Driven all the way through to the redaction, on purpose.

        Asserting only that something was queued is what let the old
        dispatch-shape regression pass. Running the queued job - by turning
        the clock, exactly as the reactor would - is what proves the job made
        the call and the call reached the redaction.
        """
        homeserver = HomeServerDouble()
        api = _module_api(homeserver)
        mod = _tier2_module(self, api, self._tier2_config())
        with patch(MODERATE_TEXT, self._verdict()) as moderate:
            await mod.on_new_event(_event("you suck"), {})
            # Nothing has run yet: `on_new_event` returns to the notifier
            # without doing any of the work.
            moderate.assert_not_awaited()
            homeserver.clock.drain()
        moderate.assert_awaited_once()
        assert moderate.await_args is not None
        self.assertEqual(moderate.await_args.args[0], "you suck")
        cast(AsyncMock, api.create_and_send_event_into_room).assert_awaited_once()

    async def test_a_worker_instance_neither_queues_nor_checks(self) -> None:
        """`on_new_event` fires on EVERY process subscribed to the events
        stream, so without the guard an N-worker deployment makes N moderation
        calls and N redaction attempts for one message."""
        homeserver = HomeServerDouble()
        api = _module_api(homeserver, run_background_tasks=False)
        mod = _tier2_module(self, api, self._tier2_config())
        with patch(MODERATE_TEXT, self._verdict()) as moderate:
            await mod.on_new_event(_event("you suck"), {})
            homeserver.clock.drain()
        moderate.assert_not_awaited()
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()
        self.assertIsNone(mod._dispatcher)

    async def test_the_same_event_is_only_ever_redacted_once(self) -> None:
        """The notifier can deliver one event more than once - the local
        persister and the replication stream both reach it - and a second
        pass must not produce a second redaction."""
        homeserver = HomeServerDouble()
        api = _module_api(homeserver)
        mod = _tier2_module(self, api, self._tier2_config())
        event = _event("you suck")
        with patch(MODERATE_TEXT, self._verdict()) as moderate:
            await mod.on_new_event(event, {})
            await mod.on_new_event(event, {})
            homeserver.clock.drain()
        moderate.assert_awaited_once()
        cast(AsyncMock, api.create_and_send_event_into_room).assert_awaited_once()

    async def test_an_already_redacted_target_is_not_redacted_again(self) -> None:
        """The pre-send re-read, which is what makes the send idempotent once
        the in-flight set has let go of the id."""
        homeserver = HomeServerDouble(EventStoreDouble(redacted=True))
        api = _module_api(homeserver)
        mod = _tier2_module(self, api, self._tier2_config())
        with patch(MODERATE_TEXT, self._verdict()):
            await mod._check_and_redact(self._job("you suck"))
        self.assertEqual(homeserver.store.reads, ["$evt1"])
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()

    async def test_a_target_that_has_vanished_is_not_redacted(self) -> None:
        homeserver = HomeServerDouble(EventStoreDouble(missing=True))
        api = _module_api(homeserver)
        mod = _tier2_module(self, api, self._tier2_config())
        with patch(MODERATE_TEXT, self._verdict()):
            await mod._check_and_redact(self._job("you suck"))
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()

    async def test_a_failed_re_read_leaves_the_message_standing(self) -> None:
        """Moderation fails open. The precondition for sending a redaction is
        a read that SAID the event is still there, not the absence of an
        answer."""
        store = EventStoreDouble()
        store.error = RuntimeError("database is unhappy")
        homeserver = HomeServerDouble(store)
        api = _module_api(homeserver)
        mod = _tier2_module(self, api, self._tier2_config())
        with patch(MODERATE_TEXT, self._verdict()):
            await mod._check_and_redact(self._job("you suck"))
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()

    async def test_a_full_queue_does_not_raise_into_the_notifier(self) -> None:
        """`on_new_event` is awaited inline by the notifier for every event on
        the homeserver. Overload has to be a counted refusal, never an
        exception and never a wait."""
        homeserver = HomeServerDouble()
        api = _module_api(homeserver)
        mod = _tier2_module(
            self, api, self._tier2_config(moderation_tier2_queue_size=1)
        )
        with patch(MODERATE_TEXT, self._verdict()):
            for index in range(50):
                await mod.on_new_event(_event("you suck", event_id=f"$e{index}"), {})
        assert mod._dispatcher is not None
        self.assertLessEqual(mod._dispatcher.queue_depth, 1)

    async def test_flagged_result_redacts_as_sender(self) -> None:
        api = _module_api()
        mod = _tier2_module(self, api, self._tier2_config())
        with patch(
            MODERATE_TEXT,
            self._verdict(categories=["harassment/threatening"]),
        ):
            await mod._check_and_redact(
                self._job("threatening text", sender="@offender:example.org")
            )
        send = cast(AsyncMock, api.create_and_send_event_into_room)
        send.assert_awaited_once()
        await_args = send.await_args
        assert await_args is not None
        sent = await_args.args[0]
        self.assertEqual(sent["type"], "m.room.redaction")
        # `room_id` is mandatory and autospec checks the METHOD's signature,
        # not the event dictionary's contents, so it has to be asserted here.
        job = self._job("threatening text", sender="@offender:example.org")
        self.assertEqual(sent["room_id"], job.room_id)
        self.assertEqual(sent["sender"], "@offender:example.org")
        self.assertEqual(sent["redacts"], job.event_id)
        self.assertEqual(sent["content"]["redacts"], job.event_id)
        self.assertIn("harassment", sent["content"]["reason"])

    async def test_self_harm_is_preserved_not_redacted(self) -> None:
        """A disclosure of self-harm stays in the room.

        The learner is asking for help and the message is the only record
        that they did; redacting it removes that and tells nobody. This is
        the one category whose disposition is not redaction.
        """
        api = _module_api()
        mod = _tier2_module(self, api, self._tier2_config())
        with patch(
            MODERATE_TEXT,
            self._verdict(categories=["self-harm/intent"]),
        ):
            await mod._check_and_redact(self._job("i want to hurt myself"))
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()

    async def test_self_harm_preserved_even_alongside_a_redactable_category(
        self,
    ) -> None:
        """Preserving wins when a verdict carries both.

        `_summarize_categories` returns the FIRST recognised category, so
        deciding on the summary would redact this one on the strength of the
        label that happens to sort ahead of the disclosure.
        """
        api = _module_api()
        mod = _tier2_module(self, api, self._tier2_config())
        with patch(
            MODERATE_TEXT,
            self._verdict(categories=["harassment", "self-harm/intent"]),
        ):
            await mod._check_and_redact(self._job("mixed"))
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()

    async def test_unflagged_result_does_not_redact(self) -> None:
        api = _module_api()
        mod = _tier2_module(self, api, self._tier2_config())
        with patch(
            MODERATE_TEXT,
            self._verdict(flagged=False, categories=[]),
        ) as moderate:
            await mod._check_and_redact(self._job("hi"))
        # The check must have RUN. Without this the test passes when the call
        # raised instead - a different failure, caught by the same fail-open
        # handler, that also redacts nothing.
        moderate.assert_awaited_once()
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()

    async def test_moderation_outage_fails_open(self) -> None:
        """Both the expected failure and an unexpected one.

        Injecting only `ModerationCheckError` leaves the widened catch
        untested, and the whole point of widening it was that an exception the
        client was not supposed to raise - a `UnicodeDecodeError` out of
        `json.loads` was the real one - must not escape into
        `run_as_background_process`, which logs whatever reaches it.
        """
        for error in (
            ModerationCheckError("down"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
            RuntimeError("something nobody predicted"),
        ):
            with self.subTest(error=type(error).__name__):
                api = _module_api()
                mod = _tier2_module(self, api, self._tier2_config())
                with patch(
                    MODERATE_TEXT,
                    self._outage(error),
                ) as moderate:
                    await mod._check_and_redact(self._job("hi"))
                moderate.assert_awaited_once()
                cast(
                    AsyncMock, api.create_and_send_event_into_room
                ).assert_not_awaited()

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
                mod = _tier2_module(self, api, self._tier2_config())
                captured = _CapturingRecords()
                root = logging.getLogger()
                previous_level = root.level
                root.addHandler(captured)
                # DEBUG, because the production line that names the category
                # is INFO and the root logger defaults to WARNING - a capture
                # that cannot see the record proves nothing about it.
                root.setLevel(logging.DEBUG)
                try:
                    with patch(
                        MODERATE_TEXT,
                        self._verdict(categories=[category]),
                    ):
                        await mod._check_and_redact(self._job("x"))
                finally:
                    root.removeHandler(captured)
                    root.setLevel(previous_level)
                self.assertTrue(captured.seen, "nothing was logged at all")
                send = cast(AsyncMock, api.create_and_send_event_into_room)
                assert send.await_args is not None
                reason = send.await_args.args[0]["content"]["reason"]
                self.assertTrue(reason.endswith(f": {UNKNOWN_CATEGORY}"), reason)
                if category:
                    self.assertNotIn(category, reason)
                    # The log line is the other half of the same rule, and a
                    # test that checks only the room misses a module that logs
                    # the raw value beside the safe one.
                    self.assertNotIn(category, "\n".join(captured.seen))

    async def test_a_known_category_survives_an_unknown_one_beside_it(self) -> None:
        api = _module_api()
        mod = _tier2_module(self, api, self._tier2_config())
        with patch(
            MODERATE_TEXT,
            self._verdict(categories=["@alice:example.org", "sexual/minors"]),
        ):
            await mod._check_and_redact(self._job("x"))
        send = cast(AsyncMock, api.create_and_send_event_into_room)
        assert send.await_args is not None
        self.assertTrue(send.await_args.args[0]["content"]["reason"].endswith("sexual"))


MODERATE_TEXT = "synapse_pangea_chat.moderation.choreo_client.moderate_text"


class TestSelfHarmIsNeverRedacted(unittest.IsolatedAsyncioTestCase):
    """The one guarantee in this feature that is absolute.

    Deleting a disclosure of self-harm is itself a harm: the learner is asking
    for help, the message is the only record that they did, and a redaction
    removes it from the room while telling nobody. "Never" therefore cannot
    rest on anything a process can lose - and it did. Preserving a message
    recorded no decision anywhere; the only thing standing between the
    disclosure and a redaction was the dispatcher's in-flight claim, which is
    released the moment the job finishes. A second verdict on the same event,
    a restart, or a second instance redacted it.

    Every test here drives the real handler against a shared, durable table.
    """

    MODERATE = MODERATE_TEXT

    def _module(self, api: ModuleApi, homeserver: HomeServerDouble) -> ChatModeration:
        return _tier2_module(self, api, _tier2_config())

    def _pair(
        self, db_pool: Optional[DbPoolDouble] = None
    ) -> Tuple[ModuleApi, HomeServerDouble]:
        """A module api and homeserver sharing one database.

        Passing the SAME `db_pool` to a second pair is how a restart and a
        second instance are expressed: new process, new memory, same table.
        """
        store = EventStoreDouble()
        if db_pool is not None:
            store.db_pool = db_pool
        homeserver = HomeServerDouble(store)
        return _module_api(homeserver), homeserver

    def _verdict(self, *categories: str) -> AsyncMock:
        mock = create_autospec(moderate_text)
        mock.return_value = {"flagged": True, "categories": list(categories)}
        return cast(AsyncMock, mock)

    def _job(self, event_id: str = "$disclosure", text: str = "x") -> ModerationJob:
        return ModerationJob(
            event_id=event_id,
            room_id="!room:example.org",
            sender="@learner:example.org",
            text=text,
            enqueued_at=0.0,
        )

    async def test_a_later_differing_verdict_does_not_redact(self) -> None:
        """The reproduction. Deliver the event, get `self-harm/intent`, let
        the job finish; deliver it again and get `harassment`. The second
        verdict redacted the disclosure the first one had protected."""
        api, homeserver = self._pair()
        mod = self._module(api, homeserver)
        with patch(self.MODERATE, self._verdict("self-harm/intent")):
            await mod._check_and_redact(self._job())
        with patch(self.MODERATE, self._verdict("harassment")):
            await mod._check_and_redact(self._job())
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()

    async def test_the_protection_survives_a_restart(self) -> None:
        """In-memory state is lost on a restart, and a guarantee that is lost
        on a restart is not one. The table is the guarantee."""
        db_pool = DbPoolDouble()
        api, homeserver = self._pair(db_pool)
        mod = self._module(api, homeserver)
        with patch(self.MODERATE, self._verdict("self-harm/intent")):
            await mod._check_and_redact(self._job())

        # A second process: nothing in common but the database.
        restarted_api, restarted_hs = self._pair(db_pool)
        restarted = self._module(restarted_api, restarted_hs)
        with patch(self.MODERATE, self._verdict("harassment")):
            await restarted._check_and_redact(self._job())
        cast(
            AsyncMock, restarted_api.create_and_send_event_into_room
        ).assert_not_awaited()

    async def test_a_second_worker_cannot_redact_what_another_preserved(
        self,
    ) -> None:
        """Two instances both running background tasks is a misconfiguration
        the in-memory guard cannot detect - and it is exactly the case where
        one instance's protection has to bind the other."""
        db_pool = DbPoolDouble()
        first_api, first_hs = self._pair(db_pool)
        second_api, second_hs = self._pair(db_pool)
        first = self._module(first_api, first_hs)
        second = self._module(second_api, second_hs)
        with patch(self.MODERATE, self._verdict("self-harm/intent")):
            await first._check_and_redact(self._job())
        with patch(self.MODERATE, self._verdict("sexual/minors")):
            await second._check_and_redact(self._job())
        cast(AsyncMock, second_api.create_and_send_event_into_room).assert_not_awaited()

    async def test_two_workers_racing_on_one_event_redact_nothing(self) -> None:
        """Both verdicts genuinely in flight at once, in two instances.

        The two coroutines are interleaved at the service call, so neither has
        finished when the other starts - which is the shape a read-then-redact
        cannot survive: both read a table that says nothing, one writes
        `preserved` and the other sends the redaction. The claim is one atomic
        insert, so exactly one decision lands and the loser is told which.
        """
        db_pool = DbPoolDouble()
        first_api, first_hs = self._pair(db_pool)
        second_api, second_hs = self._pair(db_pool)
        first = self._module(first_api, first_hs)
        second = self._module(second_api, second_hs)

        async def _verdict_for(text: str, *args: Any, **kwargs: Any) -> Any:
            # A real suspension, so the two jobs are interleaved rather than
            # run one after the other.
            await asyncio.sleep(0)
            category = "self-harm/intent" if text == "disclosure" else "harassment"
            return {"flagged": True, "categories": [category]}

        moderate = create_autospec(moderate_text, side_effect=_verdict_for)
        with patch(self.MODERATE, moderate):
            await asyncio.gather(
                first._check_and_redact(self._job(text="disclosure")),
                second._check_and_redact(self._job(text="abuse")),
            )
        self.assertEqual(moderate.await_count, 2, "the two jobs did not both run")
        cast(AsyncMock, second_api.create_and_send_event_into_room).assert_not_awaited()
        cast(AsyncMock, first_api.create_and_send_event_into_room).assert_not_awaited()

    async def test_two_instances_do_not_both_redact_one_message(self) -> None:
        """The claim is what the in-flight set could never be: shared. Two
        instances both configured to run background tasks used to send two
        redactions for one message, each blind to the other."""
        db_pool = DbPoolDouble()
        first_api, first_hs = self._pair(db_pool)
        second_api, second_hs = self._pair(db_pool)
        first = self._module(first_api, first_hs)
        second = self._module(second_api, second_hs)
        with patch(self.MODERATE, self._verdict("harassment")):
            await first._check_and_redact(self._job())
            await second._check_and_redact(self._job())
        cast(AsyncMock, first_api.create_and_send_event_into_room).assert_awaited_once()
        cast(AsyncMock, second_api.create_and_send_event_into_room).assert_not_awaited()

    async def test_a_written_off_job_cannot_redact_after_shutdown(self) -> None:
        """The redaction path's own refusal, asserted at the point of action.

        The dispatcher cancels an abandoned worker, but `CancelledError` is an
        `Exception` and every handler in this module catches those by design.
        So the last line is here: once the drain has ended, no verdict sends
        anything into a room.
        """
        reader = MetricReader()
        reader.snapshot(
            "pangea_moderation_tier2_redaction_skipped_total", cause="shutdown"
        )
        api, homeserver = self._pair()
        mod = self._module(api, homeserver)
        assert mod._dispatcher is not None
        mod._dispatcher._drained = True
        with patch(self.MODERATE, self._verdict("harassment")):
            await mod._check_and_redact(self._job())
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()
        self.assertEqual(
            reader.delta(
                "pangea_moderation_tier2_redaction_skipped_total", cause="shutdown"
            ),
            1.0,
        )

    async def test_a_disclosure_is_still_recorded_on_the_way_down(self) -> None:
        """A preserve can only ever keep a message up, so shutting down is not
        a reason to lose it: a restart that does not know why a message was
        left standing is a restart that can redact it."""
        db_pool = DbPoolDouble()
        api, homeserver = self._pair(db_pool)
        mod = self._module(api, homeserver)
        assert mod._dispatcher is not None
        mod._dispatcher._drained = True
        with patch(self.MODERATE, self._verdict("self-harm/intent")):
            await mod._check_and_redact(self._job())
        rows = db_pool.connection.execute(
            f"SELECT disposition FROM {DISPOSITION_TABLE}"
        ).fetchall()
        self.assertEqual(rows, [("preserved",)])

    async def test_a_failed_send_gives_the_claim_back(self) -> None:
        """A claim that outlived a failed send would turn a transient failure
        - the sender is briefly unable to send - into a permanent one: the
        message could never be taken down by anybody, ever again."""
        db_pool = DbPoolDouble()
        api, homeserver = self._pair(db_pool)
        mod = self._module(api, homeserver)
        send = cast(AsyncMock, api.create_and_send_event_into_room)
        send.side_effect = RuntimeError("the sender has left the room")
        with patch(self.MODERATE, self._verdict("harassment")):
            await mod._check_and_redact(self._job())

        retry_api, retry_hs = self._pair(db_pool)
        retry = self._module(retry_api, retry_hs)
        with patch(self.MODERATE, self._verdict("harassment")):
            await retry._check_and_redact(self._job())
        cast(AsyncMock, retry_api.create_and_send_event_into_room).assert_awaited_once()

    async def test_an_edit_arriving_later_does_not_take_the_original(
        self,
    ) -> None:
        """A disclosure sitting under a later harmful edit. The edit is its
        own event and may be redacted on its own merits; the original, which
        carries the disclosure, may not be - by any path."""
        db_pool = DbPoolDouble()
        api, homeserver = self._pair(db_pool)
        mod = self._module(api, homeserver)
        with patch(self.MODERATE, self._verdict("self-harm/intent")):
            await mod._check_and_redact(self._job("$original"))
        with patch(self.MODERATE, self._verdict("harassment")):
            await mod._check_and_redact(self._job("$edit"))
        send = cast(AsyncMock, api.create_and_send_event_into_room)
        redacted = [call.args[0]["redacts"] for call in send.await_args_list]
        self.assertEqual(redacted, ["$edit"])

    async def test_an_unreadable_disposition_never_redacts(self) -> None:
        """The one place this module does NOT fail towards action. If we
        cannot establish that an event was not preserved, we do not redact it:
        a message left standing is recoverable and a deleted disclosure is
        not."""
        reader = MetricReader()
        reader.snapshot(
            "pangea_moderation_tier2_redaction_skipped_total",
            cause="disposition_unknown",
        )
        api, homeserver = self._pair()
        mod = self._module(api, homeserver)
        homeserver.store.db_pool.error = RuntimeError("database is unhappy")
        with patch(self.MODERATE, self._verdict("harassment")):
            await mod._check_and_redact(self._job())
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()
        self.assertEqual(
            reader.delta(
                "pangea_moderation_tier2_redaction_skipped_total",
                cause="disposition_unknown",
            ),
            1.0,
        )

    async def test_a_preserve_that_cannot_be_recorded_is_counted(self) -> None:
        """A write that failed is a guarantee that is not durable, and the
        operator has to be able to see it. The message is still preserved, and
        this process still holds the line in memory - but the record that
        would bind a restart is missing and says so."""
        reader = MetricReader()
        reader.snapshot("pangea_moderation_tier2_disposition_write_failed_total")
        api, homeserver = self._pair()
        mod = self._module(api, homeserver)
        homeserver.store.db_pool.error = RuntimeError("database is unhappy")
        with patch(self.MODERATE, self._verdict("self-harm/intent")):
            await mod._check_and_redact(self._job())
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()
        self.assertEqual(
            reader.delta("pangea_moderation_tier2_disposition_write_failed_total"),
            1.0,
        )

    def test_every_statement_names_the_table(self) -> None:
        """The statements carry the table name as a literal, so that nothing
        in this module formats a query. This is what stops the constant and
        the SQL drifting into two different table names."""
        for statement in STATEMENTS:
            with self.subTest(statement=statement.split()[0]):
                self.assertIn(DISPOSITION_TABLE, statement)
                self.assertNotIn("{", statement)

    async def test_the_record_carries_no_sender_and_no_message(self) -> None:
        """The table is a safeguarding record and it is read by people. It
        holds the room, the event and the category - never the learner's
        Matrix ID and never a word of what they wrote."""
        api, homeserver = self._pair()
        mod = self._module(api, homeserver)
        with patch(self.MODERATE, self._verdict("self-harm/intent")):
            await mod._check_and_redact(self._job(text="i want to hurt myself"))
        rows = homeserver.store.db_pool.connection.execute(
            f"SELECT * FROM {DISPOSITION_TABLE}"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        flattened = " ".join(str(value) for value in rows[0])
        self.assertNotIn("@learner", flattened)
        self.assertNotIn("hurt myself", flattened)
        self.assertIn("self_harm", flattened)


class TestExtractionFailureIsNotACleanNegative(unittest.IsolatedAsyncioTestCase):
    """ "We could not read this" and "there is nothing here" are different
    facts, and the extractor used to report them the same way.

    This is the class `tier1_prefilter.check_text` already names for the RULES
    - a partial failure converted into a clean negative - reappearing one
    layer up, in the extraction that feeds them. A single `formatted_body`
    that breaks the parser discarded the plain `body` that had already been
    read, returned "no text", and skipped BOTH tiers with nothing counted: a
    bypass any sender can trigger on every message, invisible on every
    dashboard.
    """

    _BOMB = "<![CDATA[>" * 300 + "call 415-555-2671" + "]]>" * 300

    async def test_a_payload_that_breaks_the_parser_still_reaches_tier_1(self) -> None:
        """The reproduction. ~4 KB of nested CDATA sections; the recovered
        remainder was parsed by a nested parser per section, so 300 of them
        was 300 frames of recursion. `RecursionError` left the whole event
        unmoderated even though the plain body had already been read and says
        `call 415-555-2671`."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "call 415-555-2671",
                "format": "org.matrix.custom.html",
                "formatted_body": self._BOMB,
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    def test_the_renderer_no_longer_recurses_per_section(self) -> None:
        """Fixed at the mechanism, not by catching the error: HTML5 has no
        CDATA in an HTML body, so `<![` opens a bogus comment that ends at the
        first `>` and everything after it is text. Reading it that way is both
        correct and flat."""
        self.assertIn("call 415-555-2671", _displayed_text(self._BOMB))
        self.assertEqual(
            _displayed_text("<![CDATA[>call 415-555-2671]]>"),
            "call 415-555-2671]]>",
        )
        # An unterminated section was a second way to lose the text outright:
        # Python's parser waits for a `]]>` that never arrives.
        self.assertIn(
            "call 415-555-2671", _displayed_text("<![CDATA[>call 415-555-2671")
        )

    async def test_a_surface_that_cannot_be_read_does_not_cost_the_others(
        self,
    ) -> None:
        """The class, asserted against a renderer that simply explodes, so it
        holds for whatever the next unparseable payload turns out to be."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "call 415-555-2671",
                "format": "org.matrix.custom.html",
                "formatted_body": "<b>hello</b>",
            }
        )
        with patch(
            "synapse_pangea_chat.moderation._displayed_text",
            side_effect=RecursionError("boom"),
        ):
            self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_a_surface_that_cannot_be_read_is_counted(self) -> None:
        """Fail-open must not mean fail-silent. An unreadable surface is an
        unknown, and an uncounted unknown is indistinguishable from a clean
        message on every dashboard an operator has."""
        reader = MetricReader()
        reader.snapshot("pangea_moderation_extraction_incomplete_total", tier="tier1")
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "an ordinary sentence",
                "format": "org.matrix.custom.html",
                "formatted_body": "<b>hello</b>",
            }
        )
        with patch(
            "synapse_pangea_chat.moderation._displayed_text",
            side_effect=RecursionError("boom"),
        ):
            self.assertEqual(await mod.check_event_for_spam(event), NOT_SPAM)
        self.assertEqual(
            reader.delta("pangea_moderation_extraction_incomplete_total", tier="tier1"),
            1.0,
            "an unreadable surface went uncounted",
        )

    async def test_an_unreadable_surface_still_reaches_tier_2(self) -> None:
        """Escalation is the other half. Tier 1 cannot ask anybody; Tier 2
        can, and a message we could not fully read is exactly the one that
        needs the tier that reads it in context."""
        homeserver = HomeServerDouble()
        mod = _tier2_module(self, _module_api(homeserver), _tier2_config())
        dispatcher = mod._dispatcher
        assert dispatcher is not None
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "an ordinary sentence",
                "format": "org.matrix.custom.html",
                "formatted_body": "<b>hello</b>",
            }
        )
        with patch(
            "synapse_pangea_chat.moderation._displayed_text",
            side_effect=RecursionError("boom"),
        ):
            await mod.on_new_event(event, {})
        self.assertEqual(dispatcher.queue_depth, 1)

    async def test_an_event_with_no_readable_text_at_all_is_counted(self) -> None:
        """The floor of the rule: when nothing could be read, Tier 2 has
        nothing to ask about - and that is a DROP, which is counted, not a
        message that passed."""
        reader = MetricReader()
        reader.snapshot(
            "pangea_moderation_tier2_dropped_total", cause="extraction_failed"
        )
        homeserver = HomeServerDouble()
        mod = _tier2_module(self, _module_api(homeserver), _tier2_config())
        event = _event(content={"msgtype": "m.text", "body": "hello"})
        with patch(
            "synapse_pangea_chat.moderation._surface_text",
            side_effect=RuntimeError("boom"),
        ):
            await mod.on_new_event(event, {})
        self.assertEqual(
            reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="extraction_failed"
            ),
            1.0,
        )

    async def test_a_dispatch_failure_is_counted(self) -> None:
        """The same rule at the dispatch boundary: `on_new_event` swallowed
        every exception and returned, so a message that never reached the
        queue was reported nowhere."""
        reader = MetricReader()
        reader.snapshot("pangea_moderation_tier2_dropped_total", cause="dispatch_error")
        homeserver = HomeServerDouble()
        mod = _tier2_module(self, _module_api(homeserver), _tier2_config())
        with patch.object(
            mod, "_room_has_activity_plan", side_effect=RuntimeError("boom")
        ):
            await mod.on_new_event(_event("hello there"), {})
        self.assertEqual(
            reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="dispatch_error"
            ),
            1.0,
        )


class TestTableRepair(unittest.IsolatedAsyncioTestCase):
    """Text inside a table but outside a cell is DISPLAYED BEFORE THE TABLE.

    HTML5 calls it foster parenting, and it is not an obscure corner: it is
    what every browser does with `<table>41<tr><td>notes</td></tr>5-555-2671`,
    which reads as `415-555-2671` followed by a one-cell table. The extractor
    returned `41\n\nnotes\n\n5-555-2671` - source order, with the cell
    breaks between - so the number a reader sees was never shown to either
    tier. Same rule as every other case in this file: what the rules see is
    never less than what a reader sees.
    """

    FOSTERED = "<table>41<tr><td>notes</td></tr>5-555-2671</table>"

    def test_the_fostered_run_is_moderated_as_one_string(self) -> None:
        self.assertIn("415-555-2671", _displayed_text(self.FOSTERED))

    def test_cell_text_is_still_separated(self) -> None:
        """The repair adds a surface; it does not join cells that render
        apart, which would invent a number out of two columns."""
        cells = _displayed_text("<table><tr><td>415</td><td>5552671</td></tr></table>")
        self.assertNotIn("4155552671", cells)

    def test_two_tables_do_not_run_together(self) -> None:
        first = "<table>41</table>"
        second = "<table>5-555-2671</table>"
        self.assertNotIn("415-555-2671", _displayed_text(first + second))

    async def test_the_displayed_number_blocks_before_send(self) -> None:
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "look",
                "format": "org.matrix.custom.html",
                "formatted_body": self.FOSTERED,
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)


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

    async def test_a_surface_without_a_msgtype_is_still_read(self) -> None:
        """The outer body is benign here on purpose: with a blocking outer
        body the test passes whether or not the inner surface was read at
        all."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "* an ordinary correction",
                "m.new_content": {"body": "call 415-555-2671"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"},
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

    async def test_a_malformed_msgtype_does_not_cost_the_check(self) -> None:
        """A `msgtype` that is a list or an object is unhashable, and a set
        membership test on it raised out of extraction - which the fail-open
        handler caught, discarding the outer body it had already read."""
        for msgtype in ([1, 2], {"a": 1}, 3):
            with self.subTest(msgtype=msgtype):
                mod = _moderation(_config())
                event = _event(
                    content={"msgtype": msgtype, "body": "call 415-555-2671"}
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

    def test_attribute_placement_follows_the_rendering(self) -> None:
        """Where an attribute's text goes decides whether a number survives,
        and the two kinds go to different places.

        An image's alternative text stands IN the flow - it replaces the image
        - so it has to be spliced where the tag was, or `call <img alt="415">`
        loses the join. A `title` is a tooltip, shown nowhere between the
        characters either side, so splicing it there splits
        `41<b title="x">5</b>-555-2671`, which reads as one number, in half.
        Asserted on the renderer, because an end-to-end fixture passes on
        either placement as long as SOME rule fires."""
        self.assertEqual(
            _displayed_text('call <img src="mxc://x/y" alt="415">-555-2671'),
            "call 415-555-2671",
        )
        self.assertEqual(
            _displayed_text('41<b title="notes">5</b>-555-2671'), "415-555-2671"
        )
        self.assertEqual(_displayed_text('<a title="notes">go</a>'), "go\nnotes")

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

    def test_block_breaks_follow_the_tree_a_renderer_builds(self) -> None:
        """Where a line break goes needs a little tree context, and both
        directions cost a real result. An unmatched `</div>` is ignored by
        HTML5, and breaking there split a displayed number in half; a `<td>`
        outside a table is ignored too, while inside one it separates cells
        that render apart, and running them together invented a number that is
        on screen as two."""
        self.assertEqual(
            _displayed_text("call 41</div>5-555-2671"), "call 415-555-2671"
        )
        self.assertEqual(_displayed_text("call 41<td>5-555-2671"), "call 415-555-2671")
        cells = _displayed_text("<table><tr><td>415</td><td>5552671</td></tr></table>")
        self.assertEqual(cells.split(), ["415", "5552671"])
        self.assertEqual(
            _displayed_text("<div>a</div><div>b</div>").split(), ["a", "b"]
        )

    def test_an_empty_first_duplicate_attribute_wins(self) -> None:
        """HTML5 keeps the FIRST of two duplicate attributes, empty or not.
        Skipping the empty one and reading its non-empty twin invented text
        the renderer had discarded."""
        self.assertEqual(
            _displayed_text('hello <img src="x" alt="" alt="415-555-2671">'),
            "hello ",
        )

    def test_a_list_start_marker_is_displayed_text(self) -> None:
        """An ordered list's `start` is rendered as its first item's marker,
        and the Matrix spec permits the attribute."""
        self.assertIn(
            "4155552671",
            _displayed_text('<ol start="4155552671"><li>x</li></ol>'),
        )

    def test_a_self_closing_script_does_not_hide_the_rest(self) -> None:
        """A trailing slash does not make `<script>` void, but suppression has
        to end at the real close tag: a counter that could be incremented twice
        never fell back to zero, and every visible character after the close
        tag disappeared."""
        self.assertEqual(
            _displayed_text("hello<script/>x<script>y</script>visible"),
            "hellovisible",
        )

    def test_a_doctype_ends_where_html5_ends_it(self) -> None:
        """A `>` inside a quoted public identifier DOES end the doctype - an
        abrupt-doctype-public-identifier parse error - so the text after it is
        on screen. A quote-aware scan that read to the closing quote deleted
        displayed text, mangled doctype-shaped text inside an `alt` value, and
        recursed once per declaration; Python's own parser already agrees with
        the spec here."""
        self.assertIn(
            "call 415-555-2671",
            _displayed_text('<!DOCTYPE html PUBLIC "a>call 415-555-2671">hello'),
        )
        self.assertIn(
            "<!DOCTYPE x>", _displayed_text('<img alt="<!DOCTYPE x>" src="y">')
        )

    def test_many_declarations_do_not_stall_or_recurse(self) -> None:
        """Both failure modes the preprocessing had: a regex with alternation
        over quoted runs backtracked quadratically, and the hand-written scan
        that replaced it recursed once per declaration and raised
        `RecursionError` at about 1,200 - which the fail-open handler then
        turned into an unmoderated message."""
        start = time.monotonic()
        _displayed_text("<!DOCTYPE html>" * 4000)
        _displayed_text("<!DOCTYPE " * 4000)
        self.assertLess(time.monotonic() - start, 0.5)

    def test_a_nul_becomes_a_replacement_character(self) -> None:
        """HTML5 replaces U+0000 with U+FFFD rather than dropping it, so a NUL
        between digits is a replacement character on screen and not a phone
        number. Deleting it joined the digits and invented the match."""
        self.assertEqual(
            _displayed_text("call 41\x005-555-2671"), "call 41\ufffd5-555-2671"
        )

    def test_a_non_numeric_list_start_is_not_displayed(self) -> None:
        """A non-numeric `start` is ignored and the list renders its ordinary
        markers, so the value is on screen nowhere."""
        self.assertNotIn(
            "415-555-2671",
            _displayed_text('<ol start="call 415-555-2671"><li>x</li></ol>'),
        )

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

    async def test_html_that_a_renderer_normalises_is_still_matched(self) -> None:
        """Every case here reads as one phone number on screen, and each used
        to reach the rules as something else: an unparsed CDATA remainder, a
        table tag with no table around it, a NUL the tokenizer ignores, an
        `<image>` HTML5 parses as `img`, and alternative text that stands in
        the flow where the image was."""
        for formatted in (
            "<![CDATA[>call &#52;15-555-2671]]>",
            "<![CDATA[>call 4<b>1</b>5-555-2671]]>",
            "call 41</td>5-555-2671",
            "call 41<td>5-555-2671",
            '<image src="mxc://x/y" alt="call 415-555-2671">',
            'call <img src="mxc://x/y" alt="415">-555-2671',
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

    async def test_markup_a_renderer_hides_is_not_matched(self) -> None:
        """The permissive direction, and every one of these reads "hello" on
        screen: a `filename` on a text message is not an attachment name, a
        `formatted_body` under an unsupported format is ignored by every
        client, a trailing slash does not make `<script>` void, HTML5 keeps
        the FIRST of two duplicate attributes, and a doctype's quoted string
        may contain a `>`."""
        cases: List[Dict[str, Any]] = [
            {"msgtype": "m.text", "body": "hello", "filename": "call 415-555-2671"},
            {
                "msgtype": "m.text",
                "body": "hello",
                "format": "x-not-html",
                "formatted_body": "call 415-555-2671",
            },
            {
                "msgtype": "m.text",
                "body": "hello",
                "format": "org.matrix.custom.html",
                "formatted_body": "hello<script/>call 415-555-2671</script>",
            },
            {
                "msgtype": "m.text",
                "body": "hello",
                "format": "org.matrix.custom.html",
                "formatted_body": (
                    'hello <img src="mxc://x/y" alt="notes" alt="call 415-555-2671">'
                ),
            },
            {
                "msgtype": "m.text",
                "body": "hello",
                "format": "org.matrix.custom.html",
                "formatted_body": '<![CDATA[>hello<b alt="415-555-2671"></b>]]>',
            },
        ]
        for content in cases:
            with self.subTest(content=content):
                mod = _moderation(_config())
                self.assertEqual(
                    await mod.check_event_for_spam(_event(content=content)),
                    NOT_SPAM,
                )

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
        # A transport that is already gone raises from `stopProducing`. The
        # teardown is best-effort; ending the caller's wait is not.
        self.teardown_raises = False
        if underlying is not None:
            # twisted's proxy keeps the real transport on `_producer`, and the
            # real transport is where `abortConnection` lives.
            self._producer = underlying

    def stopProducing(self) -> None:
        self.stopped = True
        if self.teardown_raises:
            raise RuntimeError("transport is already gone")

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
        teardown_raises: bool = False,
    ) -> None:
        self.code = code
        self._chunks = chunks if chunks is not None else ([body] if body else None)
        self.protocol: Any = None
        self.transport = _FakeTransport(underlying=underlying)
        self.transport.teardown_raises = teardown_raises

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


class _SynapseClock:
    """Synapse's `Clock` surface over a twisted test `Clock`.

    The client is written against the homeserver's clock - `time()` and
    `call_later()` - because that is what it gets in production. A test still
    wants `advance()` and `getDelayedCalls()`, so the twisted clock stays
    underneath and this is the two-method adapter over it. Nothing is faked
    here that the client relies on; a scheduled call is still a real
    `IDelayedCall` with `active()` and `cancel()`.

    `call_later` fires its callback under a live `LoggingContext`, the way
    `synapse.util.Clock.call_later` does rather than the way a bare reactor
    does. That is not cosmetic: a deadline callback that resumes a coroutine
    without `PreserveLoggingContext` leaks the context, and a fake that fired
    bare would hide it.
    """

    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    def time(self) -> float:
        return self.clock.seconds()

    def call_later(self, delay: Any, callback: Any, *args: Any, **kwargs: Any) -> Any:
        def _wrapped(*inner: Any, **inner_kwargs: Any) -> None:
            with PreserveLoggingContext(
                LoggingContext(name="call_later", server_name="example.org")
            ):
                callback(*inner, **inner_kwargs)

        return self.clock.callLater(float(delay), _wrapped, *args, **kwargs)


def _consume(body_producer: Any) -> bytes:
    """The bytes the agent would put on the wire, and the interface check.

    `IBodyProducer.providedBy` rather than a duck-type test: that is the
    contract `Agent.request` actually requires, so passing a raw `BytesIO`
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


class _LogcontextLeakWatch(logging.Handler):
    """Collects Synapse's own "you mishandled a logcontext" warnings.

    The deadline paths fire from a reactor callback and resume a coroutine,
    which is the handoff commit 33f7ead was written about. Asserting only on
    the BEHAVIOUR of a timeout - that it errbacks, that it tears the
    connection down - passes just as well while leaking, and the leak is what
    kills Synapse's timers on 1.159.
    """

    MARKERS = (
        "Expected logging context",
        "Background process re-entered without a proc",
        "Looping call died",
    )

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.leaks: List[str] = []
        self._loggers = [
            logging.getLogger("synapse.logging.context"),
            logging.getLogger("synapse.metrics.background_process_metrics"),
        ]

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if any(marker in message for marker in self.MARKERS):
            self.leaks.append(message)

    def __enter__(self) -> "_LogcontextLeakWatch":
        for logger in self._loggers:
            logger.addHandler(self)
            logger.setLevel(logging.WARNING)
        return self

    def __exit__(self, *_exc: Any) -> None:
        for logger in self._loggers:
            logger.removeHandler(self)


class _DeadClock(_SynapseClock):
    """A clock that refuses to schedule, as Synapse's does after shutdown."""

    def call_later(self, delay: Any, callback: Any, *args: Any, **kwargs: Any) -> Any:
        raise Exception("Cannot start delayed call. Clock has been shutdown")


class _FakeAgent:
    """An `IAgent` double that checks the request it was handed.

    An agent that accepts any arguments and returns a canned response tests
    the response handling and nothing else: the method, the URI and the auth
    header could all be wrong and every test would pass.

    The Deferred it returns carries a CANCELLER that tears the connection
    down, which is what twisted's own `Agent` gives and what
    `SimpleHttpClient.request` does not - a double without one would let the
    header-deadline test pass against a client that could never abort a
    stalled connect.
    """

    def __init__(
        self, response: "_FakeResponse", headers_after: Optional[float] = None
    ) -> None:
        self.response = response
        self.headers_after = headers_after
        self.requests: List[Tuple[bytes, bytes, Any, Any]] = []
        self.sent_bodies: List[bytes] = []
        self.clock: Optional[Clock] = None
        self.cancelled = False
        self.canceller_raises = False
        self.canceller_is_bare = False
        self.canceller_raises_base = False
        # Whatever the request Deferred was fired with, recorded before the
        # client's own callbacks consume it.
        self.source_results: List[Any] = []

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

        def _cancel(deferred: Any) -> None:
            # What twisted's own HTTP client delivers when a request is
            # aborted before its response: `ResponseNeverReceived`, NOT a bare
            # `CancelledError`. The difference decides whether the deadline
            # can be told apart from an ordinary connection failure, so a
            # double that errbacked `CancelledError` would let a client that
            # could not tell them apart pass.
            self.cancelled = True
            if self.canceller_raises:
                raise RuntimeError("canceller blew up")
            if self.canceller_raises_base:
                # A `BaseException`, which is what an `except Exception` guard
                # is supposed to let past - so it is the one thing that can
                # leave the deadline's teardown frame.
                raise KeyboardInterrupt("interrupted mid-teardown")
            if self.canceller_is_bare:
                # And the other real shape: a canceller that fires nothing, so
                # twisted errbacks with a bare `CancelledError`. Synapse's
                # proxy CONNECT waits on a Deferred with no canceller at all,
                # which is exactly this.
                return
            self.response.transport.aborted = True
            deferred.errback(
                ResponseNeverReceived([Failure(ConnectionAborted("aborted"))])
            )

        deferred: Any = defer.Deferred(_cancel)

        def _record(result: Any) -> Any:
            self.source_results.append(result)
            return result

        deferred.addBoth(_record)

        def _deliver() -> None:
            # A real client does not deliver a response on a connection it has
            # already aborted. An unguarded `callback` here would raise
            # `AlreadyCalledError` for reasons that are the double's, not the
            # client's.
            if not deferred.called:
                deferred.callback(self.response)

        self.clock.callLater(self.headers_after, _deliver)
        return deferred


class _FailingAgent:
    """An agent whose request fails the way a transport failure does."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.requests: List[Any] = []
        self.sent_bodies: List[bytes] = []

    def request(
        self, method: bytes, uri: bytes, headers: Any = None, bodyProducer: Any = None
    ) -> Any:
        # Raised from inside an ACTIVE `except` block, which is how twisted
        # delivers a transport failure: it resumes the awaiting coroutine from
        # within its own handler, so the original becomes our `__context__`
        # whatever our own frame does. A `defer.fail` built outside a handler
        # never exercises that.
        try:
            raise self.error
        except Exception:
            return defer.fail()


class _CapturingRecords(logging.Handler):
    """Every record, formatted and raw, for the assertions that need both."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.seen: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.seen.append(record.getMessage())
        except Exception:
            self.seen.append(str(record.msg))
        self.seen.append(repr(record.args))


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
                agent=agent,
                clock=_SynapseClock(clock),
            )
        )
        results: List[Any] = []
        deferred.addBoth(results.append)
        return deferred, results

    def _call_watched(self, agent: Any, clock: Clock) -> Tuple[Any, List[Any], Any]:
        """`_call`, with Synapse's logcontext complaints collected."""
        watch = _LogcontextLeakWatch()
        with watch:
            deferred, results = self._call(agent, clock)
            return deferred, results, watch

    def test_a_body_deadline_does_not_leak_its_reactor_context(self) -> None:
        """The deadline fires from a `call_later` callback and resumes the
        coroutine awaiting the body. Without `PreserveLoggingContext` around
        the teardown the reactor is handed back whatever context the resumed
        coroutine left set, and 1.159's `clock.py` reports it."""
        clock = Clock()
        agent = _FakeAgent(_FakeResponse())
        watch = _LogcontextLeakWatch()
        with watch:
            _deferred, results = self._call(agent, clock)
            clock.advance(REQUEST_TIMEOUT_SECONDS + 1)
        results[0].trap(ModerationCheckError)
        self.assertEqual(watch.leaks, [], "the body deadline leaked a logcontext")

    def test_a_header_deadline_does_not_leak_its_reactor_context(self) -> None:
        clock = Clock()
        agent = _FakeAgent(_FakeResponse(), headers_after=REQUEST_TIMEOUT_SECONDS * 10)
        agent.clock = clock
        watch = _LogcontextLeakWatch()
        with watch:
            _deferred, results = self._call(agent, clock)
            clock.advance(REQUEST_TIMEOUT_SECONDS + 1)
        results[0].trap(ModerationCheckError)
        self.assertEqual(watch.leaks, [], "the header deadline leaked a logcontext")

    def test_a_header_deadline_aborts_the_connection(self) -> None:
        """Headers that never arrive have to end the CONNECTION, not just our
        wait. `SimpleHttpClient.request` cannot do this - it wraps the request
        in a `timeout_deferred` whose Deferred has no canceller, so an outside
        cancel stops at the wrapper and the socket stays up until Synapse's
        own sixty-second timeout. Going to the agent directly is what buys
        this, and this is the test that says so."""
        clock = Clock()
        agent = _FakeAgent(_FakeResponse(), headers_after=REQUEST_TIMEOUT_SECONDS * 10)
        agent.clock = clock
        _deferred, results = self._call(agent, clock)
        self.assertEqual(results, [])
        clock.advance(REQUEST_TIMEOUT_SECONDS + 1)
        self.assertEqual(len(results), 1)
        error = results[0].value
        self.assertIsInstance(error, ModerationCheckError)
        self.assertTrue(agent.cancelled, "the connect was never cancelled")

    def test_a_deadline_is_reported_as_a_timeout_not_a_transport_error(self) -> None:
        """The breaker and the dashboards both read `kind`. Twisted's
        exception for an aborted request says only that the response never
        arrived; it cannot say that WE ended it, so the deadline records that
        itself."""
        for headers_after in (REQUEST_TIMEOUT_SECONDS * 10, None):
            with self.subTest(stalls="headers" if headers_after else "body"):
                clock = Clock()
                agent = _FakeAgent(_FakeResponse(), headers_after=headers_after)
                agent.clock = clock
                _deferred, results = self._call(agent, clock)
                clock.advance(REQUEST_TIMEOUT_SECONDS + 1)
                error = results[0].value
                self.assertIsInstance(error, ModerationCheckError)
                self.assertEqual(error.kind, "timeout")

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
        timeout exists to fix, reached through a different door."""
        clock = Clock()
        response = _FakeResponse()
        agent = _FakeAgent(response)
        deferred, results = self._call(agent, clock)
        self.assertEqual(results, [])
        deferred.cancel()
        self.assertEqual(len(results), 1)
        self.assertTrue(response.transport.stopped, "the peer was not stopped")
        self.assertTrue(
            response.transport.lost or response.transport.aborted,
            "the connection was left open",
        )

    def test_a_cancelled_check_stays_cancelled(self) -> None:
        """The cancellation has to SURVIVE the transport.

        Cancelling a check means stopping the worker running it - a drain, a
        supervisor. The body reader's canceller used to errback with a timeout
        of its own, which pre-empted the `CancelledError` twisted was about to
        deliver: the worker then saw an ordinary moderation failure, absorbed
        it by contract, and carried on running behind a Deferred that had
        already fired. The pool grew by one every time.
        """
        clock = Clock()
        response = _FakeResponse()
        agent = _FakeAgent(response)
        deferred, results = self._call(agent, clock)
        deferred.cancel()
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], Failure)
        results[0].trap(defer.CancelledError)

    def test_a_cancel_while_awaiting_headers_stays_cancelled(self) -> None:
        """The header phase, asserted separately from the body phase.

        Cancelling a real request awaiting headers errbacks with
        `ResponseNeverReceived`, which is an ordinary transport failure - so
        the conversion that has to be prevented happens on a different route
        here than it does mid-body, and a test that covered only one of them
        left the other free to swallow a worker's stop signal.
        """
        clock = Clock()
        response = _FakeResponse()
        agent = _FakeAgent(response, headers_after=REQUEST_TIMEOUT_SECONDS * 10)
        agent.clock = clock
        deferred, results = self._call(agent, clock)
        self.assertEqual(results, [])
        deferred.cancel()
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], Failure)
        results[0].trap(defer.CancelledError)
        self.assertTrue(agent.cancelled, "the request was left in flight")

    def test_a_deadline_never_fires_the_agents_own_deferred(self) -> None:
        """The wait ends on a Deferred WE own; the agent's is only cancelled.

        Forcing an errback onto the agent's Deferred would mean its producer
        can fire an already-called Deferred later - an `AlreadyCalledError` out
        of the reactor, on the commonest timeout path there is. A response
        arriving after the deadline is the case the deadline exists for, so
        that is not a rare corner.
        """
        clock = Clock()
        response = _FakeResponse()
        agent = _FakeAgent(response, headers_after=REQUEST_TIMEOUT_SECONDS * 4)
        agent.clock = clock
        _deferred, results = self._call(agent, clock)
        clock.advance(REQUEST_TIMEOUT_SECONDS + 1)
        self.assertEqual(len(results), 1)
        results[0].trap(ModerationCheckError)
        # The agent's own Deferred was ended by its CANCELLER, not handed our
        # error. `ResponseNeverReceived` is what its canceller delivers.
        self.assertEqual(len(agent.source_results), 1)
        source = agent.source_results[0]
        self.assertIsInstance(source, Failure)
        self.assertNotIsInstance(source.value, ModerationCheckError)
        # And the late arrival changes nothing.
        clock.advance(REQUEST_TIMEOUT_SECONDS * 4)
        self.assertEqual(len(results), 1)

    def test_a_deadline_is_a_timeout_even_when_teardown_cancels_bare(
        self,
    ) -> None:
        """Our own deadline must never look like somebody stopping the worker.

        Some teardowns errback the source with a bare `CancelledError` rather
        than a transport error - Synapse's proxy CONNECT waits on a Deferred
        with no canceller, so cancelling the request there produces exactly
        that. Forwarded to the caller it reached the cancellation rule, which
        killed the worker and never told the breaker anything: a stalled proxy
        then looked like a healthy endpoint with a shrinking pool.
        """
        clock = Clock()
        response = _FakeResponse()
        agent = _FakeAgent(response, headers_after=REQUEST_TIMEOUT_SECONDS * 10)
        agent.clock = clock
        agent.canceller_is_bare = True
        _deferred, results = self._call(agent, clock)
        clock.advance(REQUEST_TIMEOUT_SECONDS + 1)
        self.assertEqual(len(results), 1)
        error = results[0].value
        self.assertNotIsInstance(
            error,
            defer.CancelledError,
            "our own deadline was reported as a cancellation, which stops the "
            "worker instead of counting a failure",
        )
        self.assertIsInstance(error, ModerationCheckError)
        self.assertEqual(error.kind, "timeout")

    def test_a_teardown_that_escapes_still_settles_the_deadline(self) -> None:
        """The deadline suppresses every later result once it starts, so it
        MUST finish. A teardown that leaves the frame without the errback
        running parks the caller with its deadline spent and every subsequent
        result discarded - a check that can no longer complete by any route.
        """
        clock = Clock()
        response = _FakeResponse()
        agent = _FakeAgent(response, headers_after=REQUEST_TIMEOUT_SECONDS * 10)
        agent.clock = clock
        agent.canceller_raises_base = True
        _deferred, results = self._call(agent, clock)
        self.assertEqual(results, [])
        with self.assertRaises(KeyboardInterrupt):
            clock.advance(REQUEST_TIMEOUT_SECONDS + 1)
        self.assertEqual(len(results), 1, "the check was left unable to finish")
        results[0].trap(ModerationCheckError)

    def test_a_config_error_status_is_not_relabelled_by_a_stalled_body(
        self,
    ) -> None:
        """Status before body, because the two answer different questions.

        A 401 whose body then stalls was reported as a timeout - and a timeout
        opens the breaker, while a bad service-account token must never open
        it: the token repeats forever, so opening would disable moderation
        until a human noticed, with the breaker's gauge blaming the provider.
        """
        clock = Clock()
        response = _FakeResponse(code=401)
        agent = _FakeAgent(response)
        _deferred, results = self._call(agent, clock)
        self.assertEqual(len(results), 1, "the status check waited for the body")
        error = results[0].value
        self.assertIsInstance(error, ModerationCheckError)
        self.assertEqual(error.kind, "config_error")
        self.assertTrue(
            response.transport.lost or response.transport.aborted,
            "the refused response's connection was left open, so it never "
            "returns to the pool",
        )

    def test_a_canceller_that_raises_still_ends_the_wait(self) -> None:
        """Teardown is best-effort; ending the wait is not.

        A canceller that raises left the coroutine parked with its deadline
        already spent - a check that neither completes nor fails, holding a
        worker and, when it is the half-open probe, the breaker's only permit,
        for the life of the process.
        """
        clock = Clock()
        response = _FakeResponse()
        agent = _FakeAgent(response, headers_after=REQUEST_TIMEOUT_SECONDS * 10)
        agent.clock = clock
        agent.canceller_raises = True
        _deferred, results = self._call(agent, clock)
        self.assertEqual(results, [])
        clock.advance(REQUEST_TIMEOUT_SECONDS + 1)
        self.assertEqual(len(results), 1, "the check was never resumed")
        error = results[0].value
        self.assertIsInstance(error, ModerationCheckError)
        self.assertEqual(error.kind, "timeout")

    def test_an_oversized_body_reports_the_cap_even_if_teardown_raises(self) -> None:
        """The size cap's own errback, isolated from the deadline's.

        `_fail` sets `_done` before tearing down, so a teardown that raised
        used to skip the errback entirely and let the exception escape through
        `dataReceived` instead - the check then failed with whatever the
        transport raised rather than with the reason it was refused, and on
        the paths where no deadline is left to catch it, not at all.
        """
        clock = Clock()
        chunk = b"x" * 65536
        response = _FakeResponse(
            chunks=[chunk] * (MAX_RESPONSE_BYTES // len(chunk) + 2),
            teardown_raises=True,
        )
        agent = _FakeAgent(response)
        _deferred, results = self._call(agent, clock)
        self.assertEqual(len(results), 1)
        error = results[0].value
        self.assertIsInstance(error, ModerationCheckError)
        self.assertIn("more than", str(error), str(error))

    def test_a_body_teardown_that_raises_still_ends_the_wait(self) -> None:
        clock = Clock()
        response = _FakeResponse(teardown_raises=True)
        agent = _FakeAgent(response)
        _deferred, results = self._call(agent, clock)
        self.assertEqual(results, [])
        clock.advance(REQUEST_TIMEOUT_SECONDS + 1)
        self.assertEqual(len(results), 1, "the body read was never resumed")
        results[0].trap(ModerationCheckError)

    def test_an_unschedulable_deadline_ends_the_exchange_now(self) -> None:
        """`Clock.call_later` raises once the clock is shut down, and the
        request has already gone out by then. Reporting a failure and walking
        away would leave the exchange alive with nobody reading it and no
        timer left to end it."""
        clock = Clock()
        response = _FakeResponse()
        agent = _FakeAgent(response, headers_after=REQUEST_TIMEOUT_SECONDS * 10)
        agent.clock = clock
        deferred = defer.ensureDeferred(
            moderate_text(
                "some text",
                base_url="http://choreo.invalid",
                access_token="syt_x",
                agent=agent,
                clock=_DeadClock(clock),
            )
        )
        results: List[Any] = []
        deferred.addBoth(results.append)
        self.assertEqual(len(results), 1, "the check was left pending")
        results[0].trap(ModerationCheckError)
        self.assertTrue(agent.cancelled, "the request was left in flight")

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
        """Valid JSON, so the only thing that can fail it is the cap. A body of
        junk bytes fails for a second reason and the test passes even when the
        cap is raised."""
        prefix = b'{"flagged": false, "categories": [], "note": "'
        suffix = b'"}'
        padding = b"y" * (MAX_RESPONSE_BYTES + 1 - len(prefix) - len(suffix))
        body = prefix + padding + suffix
        self.assertEqual(len(body), MAX_RESPONSE_BYTES + 1)
        clock = Clock()
        response = _FakeResponse(body=body)
        agent = _FakeAgent(response)
        _deferred, results = self._call(agent, clock)
        self.assertIsInstance(results[0], Failure, results[0])
        results[0].trap(ModerationCheckError)
        self.assertTrue(response.transport.lost or response.transport.aborted)

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

    def test_a_response_with_no_verdict_is_refused(self) -> None:
        """Absent is not False. `{}`, or an error object returned with a 200,
        was read as "this message is fine" - a verdict we were never given,
        reached by a default."""
        for body in (b"{}", b'{"error": "provider failed"}', b'{"categories": []}'):
            with self.subTest(body=body):
                clock = Clock()
                agent = _FakeAgent(_FakeResponse(body=body))
                _deferred, results = self._call(agent, clock)
                self.assertIsInstance(results[0], Failure, results[0])
                results[0].trap(ModerationCheckError)

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
        """A body that is otherwise a perfectly good verdict, so the status is
        the only thing left that can fail it. `{}` fails missing-verdict
        validation on its own, and the test passed with the status check
        deleted."""
        clock = Clock()
        agent = _FakeAgent(
            _FakeResponse(code=502, body=b'{"flagged": false, "categories": []}')
        )
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
        self._assert_chain_severed(error := results[0].value)
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
        self._assert_chain_severed(error)
        self.assertNotIn(self.PAYLOAD, self._everything_reachable_from(error))

    def test_a_rule_failure_carries_no_chain_either(self) -> None:
        """`Tier1RuleError` promises "a rule identifier and nothing else", and
        `raise ... from None` does not deliver it: `__context__` still holds
        the matcher's own exception, which routinely quotes the message body
        it failed on."""
        payload = "@alice:example.org and the message body"
        with patch.object(
            tier1_prefilter.phonenumbers,
            "PhoneNumberMatcher",
            side_effect=ValueError(payload),
        ):
            with self.assertRaises(Tier1RuleError) as caught:
                check_text("call me", ["US"])
        error = caught.exception
        self._assert_chain_severed(error)
        self.assertNotIn(payload, self._everything_reachable_from(error))

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
    def _real_chain(error: BaseException) -> Tuple[Any, Any]:
        """`(__cause__, __context__)` read from `BaseException`'s own slots.

        `vars(BaseException)` rather than attribute access on the class,
        because mypy narrows `BaseException.__context__` to the VALUE type -
        it is a descriptor, and the descriptor is what has to be invoked here.
        """
        slots = vars(BaseException)
        return (
            slots["__cause__"].__get__(error, type(error)),
            slots["__context__"].__get__(error, type(error)),
        )

    def _assert_chain_severed(self, error: BaseException) -> None:
        """Read through `BaseException`'s own descriptors, not the instance.

        A subclass can shadow `__context__` with a property returning None,
        which hides the chain from an ordinary read while the real slot still
        holds the original - and an earlier revision of this branch did
        exactly that. An assertion on `error.__context__` accepts the mask;
        this one does not.
        """
        cause, context = self._real_chain(error)
        self.assertIsNone(cause)
        self.assertIsNone(context)

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
            # Through the base descriptors, for the reason in
            # `_assert_chain_severed`.
            slots = vars(BaseException)
            current = slots["__cause__"].__get__(current, type(current)) or slots[
                "__context__"
            ].__get__(current, type(current))
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
        """Both directions, because a test that only ever disables Tier 2
        passes when Tier-1 registration is made unconditional."""
        tier1_only = _module_api()
        ChatModeration(tier1_only, _config(moderation_tier1_enabled=True))
        tier1_only.register_spam_checker_callbacks.assert_called_once()
        tier1_only.register_third_party_rules_callbacks.assert_not_called()

        tier2_only = _module_api()
        ChatModeration(
            tier2_only,
            _config(
                moderation_tier1_enabled=False,
                moderation_tier2_enabled=True,
                moderation_choreo_base_url="http://choreo.invalid",
                moderation_choreo_access_token="syt_x",
            ),
        )
        tier2_only.register_spam_checker_callbacks.assert_not_called()
        tier2_only.register_third_party_rules_callbacks.assert_called_once()


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
            # Non-ASCII in the PATH, which the host check does not see: the
            # request URI is built as bytes, so twisted refuses it.
            "https://choreo.invalid/ü",
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
            # DEL is a control character above 0x21, which a "< 0x21" test
            # misses and twisted's `_ensureValidURI` rejects.
            "https://choreo.invalid/\x7fx",
            # `urlparse` reports no port for a bare trailing colon, so the
            # range check never sees it; twisted keeps the colon in the host.
            "https://choreo.invalid:",
            "https://[::1]:",
            # Structurally invalid hostnames: twisted marks these bad and
            # fails the connection before it ever resolves.
            "https://a..b.invalid",
            "https://a;b.invalid",
            "https://" + "x" * 64 + ".invalid",
            "https://[not-an-address]",
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
            # A bracketed IPv6 address ending in compressed zero groups is a
            # host, not a dangling port separator.
            "https://[::1]",
            "https://[2001:db8::]",
            "https://[::1]:8443",
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
