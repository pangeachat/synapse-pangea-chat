from __future__ import annotations

import unittest

from synapse_pangea_chat import PangeaChat

_BASE_CONFIG = {
    "cms_base_url": "http://cms.example.test",
    "cms_service_api_key": "test-api-key",
}


class TestUserActivityConfig(unittest.TestCase):
    def test_parse_config_includes_user_activity_notification_bot_user_id(self):
        config = PangeaChat.parse_config(
            {
                **_BASE_CONFIG,
                "user_activity_notification_bot_user_id": "@bot:example.com",
            }
        )
        self.assertEqual(
            config.user_activity_notification_bot_user_id, "@bot:example.com"
        )

    def test_parse_config_user_activity_notification_bot_user_id_defaults_to_none(
        self,
    ):
        config = PangeaChat.parse_config({**_BASE_CONFIG})
        self.assertIsNone(config.user_activity_notification_bot_user_id)

    def test_parse_config_rejects_empty_user_activity_notification_bot_user_id(self):
        with self.assertRaisesRegex(
            ValueError, "user_activity_notification_bot_user_id"
        ):
            PangeaChat.parse_config(
                {
                    **_BASE_CONFIG,
                    "user_activity_notification_bot_user_id": "",
                }
            )
