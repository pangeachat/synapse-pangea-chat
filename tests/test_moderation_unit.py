"""Unit tests for server-side chat moderation (Tier 1 pre-filter + callback
filtering logic). No Synapse process — ModuleApi is mocked."""

import unittest
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.api.errors import Codes
from synapse.module_api import NOT_SPAM

from synapse_pangea_chat import PangeaChat
from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.moderation import ChatModeration, _normalize_category
from synapse_pangea_chat.moderation.tier1_prefilter import (
    REASON_PHONE_NUMBER,
    REASON_PROFANITY,
    REASON_STREET_ADDRESS,
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
            check_text("call me at (415) 555-2671", ["US"]), REASON_PHONE_NUMBER
        )

    def test_international_phone_blocks_regardless_of_region(self) -> None:
        self.assertEqual(
            check_text("mon numéro est +33 6 12 34 56 78", ["US"]),
            REASON_PHONE_NUMBER,
        )

    def test_street_address_blocks(self) -> None:
        self.assertEqual(
            check_text("meet me at 42 Maple Street after class", ["US"]),
            REASON_STREET_ADDRESS,
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
            await mod.check_event_for_spam(FakeEvent("hola, ¿cómo estás?")),
            NOT_SPAM,
        )

    async def test_phone_number_forbidden(self) -> None:
        mod = _moderation(_config())
        self.assertEqual(
            await mod.check_event_for_spam(FakeEvent("call me: 415-555-2671")),
            Codes.FORBIDDEN,
        )

    async def test_exempt_sender_skipped(self) -> None:
        mod = _moderation(
            _config(moderation_exempt_user_id_patterns=[r"@bot.*:example\.org"])
        )
        event = FakeEvent("call me: 415-555-2671", sender="@bot:example.org")
        self.assertEqual(await mod.check_event_for_spam(event), NOT_SPAM)

    async def test_non_message_event_skipped(self) -> None:
        mod = _moderation(_config())
        event = FakeEvent(event_type="m.room.topic", content={"topic": "415-555-2671"})
        self.assertEqual(await mod.check_event_for_spam(event), NOT_SPAM)

    async def test_image_message_skipped(self) -> None:
        mod = _moderation(_config())
        event = FakeEvent(content={"msgtype": "m.image", "body": "415-555-2671.jpg"})
        self.assertEqual(await mod.check_event_for_spam(event), NOT_SPAM)

    async def test_edit_moderates_replacement_text(self) -> None:
        mod = _moderation(_config())
        event = FakeEvent(
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
                await mod.check_event_for_spam(FakeEvent("anything")), NOT_SPAM
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
        mod._check_and_redact = AsyncMock()  # type: ignore[method-assign]
        state = {(PANGEA_ACTIVITY_PLAN_STATE_EVENT_TYPE, ""): MagicMock()}
        with patch(
            "synapse.metrics.background_process_metrics.run_as_background_process"
        ) as bg:
            await mod.on_new_event(FakeEvent("you suck"), state)
            bg.assert_not_called()

    async def test_plain_room_dispatches(self) -> None:
        mod = _moderation(self._tier2_config())
        with patch(
            "synapse.metrics.background_process_metrics.run_as_background_process"
        ) as bg:
            await mod.on_new_event(FakeEvent("you suck"), {})
            bg.assert_called_once()

    async def test_flagged_result_redacts_as_sender(self) -> None:
        api = MagicMock()
        api.create_and_send_event_into_room = AsyncMock()
        mod = ChatModeration(api, self._tier2_config())
        event = FakeEvent("threatening text", sender="@offender:example.org")
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
        sent = api.create_and_send_event_into_room.await_args.args[0]
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
            await mod._check_and_redact(FakeEvent("hi"), "hi")
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
            await mod._check_and_redact(FakeEvent("hi"), "hi")
        api.create_and_send_event_into_room.assert_not_awaited()


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

    def test_invalid_exempt_regex_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PangeaChat.parse_config(
                {**self.BASE, "moderation": {"exempt_user_id_patterns": ["["]}}
            )

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
                    "exempt_user_id_patterns": [r"@bot:.*"],
                },
            }
        )
        self.assertTrue(cfg.moderation_tier1_enabled)
        self.assertEqual(cfg.moderation_tier1_phone_regions, ["US", "FR"])
        self.assertTrue(cfg.moderation_tier2_enabled)


if __name__ == "__main__":
    unittest.main()
