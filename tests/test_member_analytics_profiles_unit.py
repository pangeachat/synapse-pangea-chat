from __future__ import annotations

import json
import unittest

from synapse_pangea_chat import PangeaChat
from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.member_analytics_profiles.is_rate_limited import (
    is_rate_limited,
    request_log,
)
from synapse_pangea_chat.member_analytics_profiles.member_analytics_profiles import (
    ANALYTICS_PROFILE_FIELD,
    analytics_profiles_by_member,
)

_PROFILE = {"analytics": {"es": {"level": 4, "stars": 12}}}


class TestAnalyticsProfilesByMember(unittest.TestCase):
    def test_every_member_is_listed_with_only_the_analytics_field(self):
        profiles = analytics_profiles_by_member(
            ["@a:home", "@b:home", "@c:home", "@d:home"],
            [
                ("@a:home", {ANALYTICS_PROFILE_FIELD: _PROFILE, "m.tz": "UTC"}),
                ("@b:home", {"m.tz": "UTC"}),
                ("@c:home", None),
            ],
        )
        self.assertEqual(
            profiles,
            {
                "@a:home": {ANALYTICS_PROFILE_FIELD: _PROFILE},
                "@b:home": {},
                "@c:home": {},
                "@d:home": {},
            },
        )

    def test_sqlite_text_column_is_decoded(self):
        profiles = analytics_profiles_by_member(
            ["@a:home"],
            [("@a:home", json.dumps({ANALYTICS_PROFILE_FIELD: _PROFILE}))],
        )
        self.assertEqual(profiles, {"@a:home": {ANALYTICS_PROFILE_FIELD: _PROFILE}})

    def test_a_row_for_someone_not_joined_is_ignored(self):
        profiles = analytics_profiles_by_member(
            ["@a:home"], [("@x:home", {ANALYTICS_PROFILE_FIELD: _PROFILE})]
        )
        self.assertEqual(profiles, {"@a:home": {}})


class TestRateLimit(unittest.TestCase):
    def setUp(self) -> None:
        request_log.clear()

    def test_limit_is_per_caller(self):
        config = PangeaChatConfig(
            member_analytics_profiles_requests_per_burst=2,
            member_analytics_profiles_burst_duration_seconds=60,
        )
        self.assertFalse(is_rate_limited("@a:home", config))
        self.assertFalse(is_rate_limited("@a:home", config))
        self.assertTrue(is_rate_limited("@a:home", config))
        self.assertFalse(is_rate_limited("@b:home", config))


_BASE_CONFIG = {
    "cms_base_url": "http://cms.example.test",
    "cms_service_api_key": "test-api-key",
}


def _parse(**overrides: object) -> PangeaChatConfig:
    return PangeaChat.parse_config({**_BASE_CONFIG, **overrides})


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        config = _parse()
        self.assertEqual(config.member_analytics_profiles_requests_per_burst, 30)
        self.assertEqual(config.member_analytics_profiles_burst_duration_seconds, 60)

    def test_invalid_values_are_refused(self):
        for bad in (
            {"member_analytics_profiles_requests_per_burst": 0},
            {"member_analytics_profiles_requests_per_burst": True},
            {"member_analytics_profiles_burst_duration_seconds": "60"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "member_analytics_profiles"):
                    _parse(**bad)
