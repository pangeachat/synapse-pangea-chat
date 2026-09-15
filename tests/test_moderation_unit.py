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

from synapse_pangea_chat import PangeaChat
from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.moderation import (
    UNKNOWN_CATEGORY,
    ChatModeration,
    _displayed_text,
    _normalize_category,
    _summarize_categories,
)
from synapse_pangea_chat.moderation.choreo_client import (
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
        self.started: List[Tuple[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        bound = self.signature.bind(*args, **kwargs)
        desc = bound.arguments["desc"]
        func = bound.arguments["func"]
        extra = bound.arguments.get("args", ())
        if "server_name" in self.signature.parameters:
            server_name = bound.arguments["server_name"]
            if not isinstance(server_name, str):
                raise TypeError(
                    "run_as_background_process wants a str server_name, got "
                    f"{type(server_name).__name__}"
                )
        if not asyncio.iscoroutinefunction(func):
            raise TypeError(f"{type(func).__name__} object is not awaitable")
        coroutine = func(*extra)
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


class TestTier1FailsOpenAsAWhole(unittest.IsolatedAsyncioTestCase):
    """A rule that cannot answer must not be read as a rule that answered no.

    The phone matcher's exception used to be caught inside
    `contains_phone_number`, which returned `False`; `check_text` then ran the
    address rule and returned `Codes.FORBIDDEN`. A message was therefore
    REJECTED on the strength of a Tier 1 run that had already failed, in a tier
    whose whole contract is that a failure lets the message through.
    """

    RULE_PATCHES = (
        ("contains_phone_number", "meet me at 42 Maple Street"),
        ("contains_street_address", "you are a fucking idiot"),
        ("contains_profanity", "an ordinary sentence"),
    )

    def test_any_failing_rule_aborts_the_whole_tier(self) -> None:
        for rule, text in self.RULE_PATCHES:
            with self.subTest(rule=rule):
                with patch(
                    f"synapse_pangea_chat.moderation.tier1_prefilter.{rule}",
                    side_effect=RuntimeError("library quirk"),
                ):
                    with self.assertRaises(Tier1RuleError):
                        check_text(text, ["US"])

    async def test_a_failed_rule_never_produces_a_block(self) -> None:
        """The end-to-end shape of the same defect: the callback must allow."""
        for rule, text in self.RULE_PATCHES:
            with self.subTest(rule=rule):
                mod = _moderation(_config())
                with patch(
                    f"synapse_pangea_chat.moderation.tier1_prefilter.{rule}",
                    side_effect=RuntimeError("library quirk"),
                ):
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
            self.assertEqual(background.started, [])
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
        result: Dict[str, Any] = {
            "flagged": True,
            "categories": ["harassment"],
            "evaluated": True,
        }
        result.update(overrides)
        return AsyncMock(return_value=result)

    async def test_activity_room_skipped(self) -> None:
        mod = _moderation(self._tier2_config())
        state = {(PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE, ""): MagicMock()}
        background = _BackgroundProcessDouble()
        with patch(
            "synapse_pangea_chat.moderation.run_as_background_process", background
        ):
            await mod.on_new_event(_event("you suck"), state)
            self.assertEqual(background.started, [])

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
            AsyncMock(return_value={"flagged": False, "evaluated": True}),
        ):
            await mod._check_and_redact(_event("hi"), "hi")
        cast(AsyncMock, api.create_and_send_event_into_room).assert_not_awaited()

    async def test_moderation_outage_fails_open(self) -> None:
        from synapse_pangea_chat.moderation.choreo_client import ModerationCheckError

        api = _module_api()
        mod = ChatModeration(api, self._tier2_config())
        with patch(
            "synapse_pangea_chat.moderation.moderate_text",
            AsyncMock(side_effect=ModerationCheckError("down")),
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
        a non-edit is text nobody sees, and Tier 1 must not block on it."""
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "an ordinary sentence",
                "m.new_content": {"msgtype": "m.text", "body": "call 415-555-2671"},
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), NOT_SPAM)

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
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "* a harmless correction",
                "m.new_content": MappingProxyType(
                    {"msgtype": "m.text", "body": "call me: 415-555-2671"}
                ),
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$orig"},
            }
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

    async def test_an_unterminated_tag_does_not_swallow_the_tail(self) -> None:
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "ok",
                "format": "org.matrix.custom.html",
                "formatted_body": "see <x call 415-555-2671",
            }
        )
        self.assertEqual(await mod.check_event_for_spam(event), Codes.FORBIDDEN)

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


class _FakeResponse:
    """Just enough of `IResponse` for `readBody`, and no more."""

    version = (b"HTTP", 1, 1)
    phrase = b"OK"

    def __init__(self, code: int = 200, body: Optional[bytes] = None) -> None:
        self.code = code
        self._body = body
        self.protocol: Any = None

    def deliverBody(self, protocol: Any) -> None:
        self.protocol = protocol
        if self._body is None:
            # The stall: headers arrived, the body never does and the
            # connection is never closed.
            return
        protocol.dataReceived(self._body)
        protocol.connectionLost(Failure(ResponseDone()))


class _FakeAgent:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.requests: List[Any] = []

    def request(self, *args: Any, **kwargs: Any) -> Any:
        self.requests.append(args)
        return defer.succeed(self.response)


class TestChoreoClient(unittest.TestCase):
    """The transport, against a clock we control.

    A `Clock` rather than the real reactor is what makes the stall test a test:
    with the global reactor the assertion would be "wait 15 seconds and hope",
    and with no injectable clock at all there is no way to assert the absence
    of a scheduled timeout, which is the actual defect.
    """

    def _call(self, agent: _FakeAgent, clock: Clock) -> Tuple[Any, List[Any]]:
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
        """The body's timeout is what remains of the request's, not a fresh
        one: two full-length timeouts in series would double the worst case."""
        clock = Clock()
        agent = _FakeAgent(_FakeResponse())
        _deferred, results = self._call(agent, clock)
        clock.advance(REQUEST_TIMEOUT_SECONDS - 1)
        self.assertEqual(results, [])
        clock.advance(2)
        self.assertEqual(len(results), 1)
        results[0].trap(ModerationCheckError)

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

    def test_the_failure_carries_no_cause_chain(self) -> None:
        """ADR-10. `run_as_background_process` calls `logger.exception` on what
        reaches it, and that prints `__cause__` in full - so a transport error
        quoting the payload would be logged by a route our format strings never
        mention. The chain is dropped rather than relabelled."""
        clock = Clock()
        agent = _FakeAgent(
            _FakeResponse(body=b"a raw copy of the private message body")
        )
        _deferred, results = self._call(agent, clock)
        error = results[0].value
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)
        rendered = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        self.assertNotIn("a raw copy of the private message body", rendered)
        self.assertNotIn("JSONDecodeError", rendered)


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

    def test_a_usable_choreo_url_is_accepted(self) -> None:
        for url in (
            "https://choreo.example.org",
            "https://choreo.example.org/",
            "https://choreo.example.org/api",
            "http://127.0.0.1:8080",
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
