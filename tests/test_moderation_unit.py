"""Unit tests for server-side chat moderation (Tier 1 pre-filter + callback
filtering logic). No Synapse process — ModuleApi is mocked."""

import unittest
from types import MappingProxyType
from typing import Any, Dict, List, Optional, cast
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.api.errors import Codes
from synapse.events import EventBase
from synapse.module_api import NOT_SPAM

from synapse_pangea_chat import PangeaChat
from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.moderation import ChatModeration, _normalize_category
from synapse_pangea_chat.moderation.tier1_prefilter import (
    REASON_CONTACT_DETAILS,
    REASON_LOCATION_DETAILS,
    REASON_PROFANITY,
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
    return ChatModeration(MagicMock(), config)


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
        with patch(
            "synapse_pangea_chat.moderation.run_as_background_process"
        ) as run_bg:
            await mod.on_new_event(_event("something", sender="@bot:example.org"), {})
            run_bg.assert_not_called()
            await mod.on_new_event(
                _event("something", sender="@botimposter:example.org.evil.com"),
                {},
            )
            run_bg.assert_called_once()

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

    async def test_edit_moderates_replacement_text(self) -> None:
        mod = _moderation(_config())
        event = _event(
            content={
                "msgtype": "m.text",
                "body": "* innocuous",
                "m.new_content": {"msgtype": "m.text", "body": "call 415-555-2671"},
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

    async def test_activity_room_skipped(self) -> None:
        mod = _moderation(self._tier2_config())
        state = {(PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE, ""): MagicMock()}
        with (
            patch.object(ChatModeration, "_check_and_redact", AsyncMock()),
            patch("synapse_pangea_chat.moderation.run_as_background_process") as bg,
        ):
            await mod.on_new_event(_event("you suck"), state)
            bg.assert_not_called()

    async def test_plain_room_dispatches(self) -> None:
        mod = _moderation(self._tier2_config())
        with patch("synapse_pangea_chat.moderation.run_as_background_process") as bg:
            await mod.on_new_event(_event("you suck"), {})
            bg.assert_called_once()

    async def test_flagged_result_redacts_as_sender(self) -> None:
        api = MagicMock()
        api.create_and_send_event_into_room = AsyncMock()
        mod = ChatModeration(api, self._tier2_config())
        event = _event("threatening text", sender="@offender:example.org")
        with patch(
            "synapse_pangea_chat.moderation.moderate_text",
            AsyncMock(
                return_value={
                    "flagged": True,
                    "categories": ["self-harm/intent"],
                    "evaluated": True,
                }
            ),
        ):
            await mod._check_and_redact(event, "threatening text")
        api.create_and_send_event_into_room.assert_awaited_once()
        await_args = api.create_and_send_event_into_room.await_args
        assert await_args is not None
        sent = await_args.args[0]
        self.assertEqual(sent["type"], "m.room.redaction")
        self.assertEqual(sent["sender"], "@offender:example.org")
        self.assertEqual(sent["redacts"], event.event_id)
        self.assertEqual(sent["content"]["redacts"], event.event_id)
        self.assertIn("self_harm", sent["content"]["reason"])

    async def test_unflagged_result_does_not_redact(self) -> None:
        api = MagicMock()
        api.create_and_send_event_into_room = AsyncMock()
        mod = ChatModeration(api, self._tier2_config())
        with patch(
            "synapse_pangea_chat.moderation.moderate_text",
            AsyncMock(return_value={"flagged": False, "evaluated": True}),
        ):
            await mod._check_and_redact(_event("hi"), "hi")
        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_moderation_outage_fails_open(self) -> None:
        from synapse_pangea_chat.moderation.choreo_client import ModerationCheckError

        api = MagicMock()
        api.create_and_send_event_into_room = AsyncMock()
        mod = ChatModeration(api, self._tier2_config())
        with patch(
            "synapse_pangea_chat.moderation.moderate_text",
            AsyncMock(side_effect=ModerationCheckError("down")),
        ):
            await mod._check_and_redact(_event("hi"), "hi")
        api.create_and_send_event_into_room.assert_not_awaited()


class TestEditContentExtraction(unittest.IsolatedAsyncioTestCase):
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
