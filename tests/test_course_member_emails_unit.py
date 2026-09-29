from __future__ import annotations

import unittest

from synapse_pangea_chat import PangeaChat
from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.course_member_emails.is_rate_limited import (
    is_rate_limited,
    request_log,
)
from synapse_pangea_chat.course_member_emails.members import (
    COURSE_ADMIN_POWER_LEVEL,
    effective_power_levels,
    pick_email_per_user,
    visible_member_ids,
)


def _is_mine(user_id: str) -> bool:
    return user_id.endswith(":home")


class TestEffectivePowerLevels(unittest.TestCase):
    def test_listed_default_and_creator(self):
        levels = effective_power_levels(
            ["@t:home", "@s:home", "@c:home"],
            {"users": {"@t:home": 100}, "users_default": 0},
            creators={"@c:home"},
        )
        self.assertEqual(
            levels,
            {"@t:home": 100, "@s:home": 0, "@c:home": COURSE_ADMIN_POWER_LEVEL},
        )

    def test_no_power_levels_event_means_default_zero(self):
        self.assertEqual(
            effective_power_levels(["@s:home"], None, set()), {"@s:home": 0}
        )

    def test_bad_values_fall_back_to_default(self):
        levels = effective_power_levels(
            ["@a:home", "@b:home"],
            {"users": {"@a:home": "high", "@b:home": True}, "users_default": 5},
            set(),
        )
        self.assertEqual(levels, {"@a:home": 5, "@b:home": 5})

    def test_string_numbers_are_honoured(self):
        levels = effective_power_levels(
            ["@a:home"], {"users": {"@a:home": "100"}}, set()
        )
        self.assertEqual(levels["@a:home"], 100)


class TestVisibleMemberIds(unittest.TestCase):
    def test_excludes_caller_bots_admins_and_remote_users(self):
        joined = [
            "@teacher:home",
            "@coteacher:home",
            "@bot:home",
            "@notes-bot:home",
            "@bot-helper:home",
            "@remote:elsewhere",
            "@bob:home",
            "@alice:home",
            "@mod:home",
        ]
        levels = {
            "@teacher:home": 100,
            "@coteacher:home": 150,
            "@mod:home": 50,
        }
        self.assertEqual(
            visible_member_ids(
                joined_member_ids=joined,
                power_levels=levels,
                caller_id="@teacher:home",
                is_mine=_is_mine,
            ),
            ["@alice:home", "@bob:home", "@mod:home"],
        )

    def test_a_human_whose_name_starts_with_bot_is_not_a_bot(self):
        self.assertEqual(
            visible_member_ids(
                joined_member_ids=["@bottomley:home"],
                power_levels={},
                caller_id="@teacher:home",
                is_mine=_is_mine,
            ),
            ["@bottomley:home"],
        )


class TestPickEmailPerUser(unittest.TestCase):
    def test_first_bound_address_wins(self):
        rows = [
            {"user_id": "@a:home", "address": "later@x.org", "added_at": 20},
            {"user_id": "@a:home", "address": "first@x.org", "added_at": 10},
            {"user_id": "@b:home", "address": "b@x.org", "added_at": None},
        ]
        self.assertEqual(
            pick_email_per_user(rows),
            {"@a:home": "first@x.org", "@b:home": "b@x.org"},
        )

    def test_ties_break_alphabetically(self):
        rows = [
            {"user_id": "@a:home", "address": "z@x.org", "added_at": 1},
            {"user_id": "@a:home", "address": "m@x.org", "added_at": 1},
        ]
        self.assertEqual(pick_email_per_user(rows), {"@a:home": "m@x.org"})

    def test_malformed_rows_are_skipped(self):
        rows = [{"user_id": None, "address": "a@x.org"}, {"user_id": "@a:home"}]
        self.assertEqual(pick_email_per_user(rows), {})


class TestRateLimit(unittest.TestCase):
    def setUp(self) -> None:
        request_log.clear()

    def test_limit_is_per_caller(self):
        config = PangeaChatConfig(
            course_member_emails_requests_per_burst=2,
            course_member_emails_burst_duration_seconds=60,
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
    def test_off_by_default(self):
        config = _parse()
        self.assertFalse(config.course_member_emails_enabled)
        self.assertEqual(config.course_member_emails_requests_per_burst, 20)
        self.assertEqual(config.course_member_emails_burst_duration_seconds, 60)

    def test_enabled(self):
        config = _parse(course_member_emails_enabled=True)
        self.assertTrue(config.course_member_emails_enabled)

    def test_invalid_values_are_refused(self):
        for bad in (
            {"course_member_emails_enabled": "yes"},
            {"course_member_emails_requests_per_burst": 0},
            {"course_member_emails_requests_per_burst": True},
            {"course_member_emails_burst_duration_seconds": "60"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "course_member_emails"):
                    _parse(**bad)
