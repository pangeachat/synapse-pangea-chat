from __future__ import annotations

import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat import PangeaChat
from synapse_pangea_chat.nudge_delivery import categories, tokens
from synapse_pangea_chat.nudge_delivery.click import NudgeClick
from synapse_pangea_chat.nudge_delivery.common import app_url
from synapse_pangea_chat.nudge_delivery.deliver import (
    CHANNEL_EMAIL,
    CHANNEL_IN_APP,
    CHANNEL_NONE,
    CHANNEL_PUSH,
    CHANNEL_REFUSED,
    DeliverNudge,
)
from synapse_pangea_chat.nudge_delivery.push_rule import (
    ensure_bot_notice_push_rule,
    reset_confirmed_users_for_tests,
)
from synapse_pangea_chat.nudge_delivery.unsubscribe import NudgeUnsubscribe

SECRET = b"unit-test-secret"
NOW_MS = 1_700_000_000_000
USER = "@alice:my.domain.name"


def _config(**overrides):
    config = MagicMock()
    config.nudge_email_enabled = True
    config.nudge_suppress_notice_push_rules = True
    config.nudge_token_secret = SECRET.decode()
    config.nudge_token_ttl_days = 90
    config.nudge_email_postal_address = "1 Test St"
    config.nudge_public_requests_per_burst = 30
    config.nudge_public_burst_duration_seconds = 60
    config.app_base_url = "https://app.example.test"
    config.send_push_sygnal_url = "https://sygnal.example.test"
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _api(
    *,
    account_data=None,
    threepids=(),
    currently_active=False,
    public_baseurl="https://matrix.example.test/",
):
    api = MagicMock()
    api._hs.get_clock.return_value.time_msec.return_value = NOW_MS
    api._hs.get_clock.return_value.sleep = AsyncMock()
    api._hs.config.server.public_baseurl = public_baseurl
    api._hs.config.server.presence_enabled = True
    api._hs.config.server.track_presence = True
    api._hs.config.email.email_app_name = "Pangea Chat"
    api._hs.config.key.macaroon_secret_key = b"macaroon"
    api._hs.get_presence_handler.return_value.current_state_for_user = AsyncMock(
        return_value=SimpleNamespace(
            state="online" if currently_active else "offline",
            currently_active=currently_active,
        )
    )
    api._hs.get_datastores.return_value.main.user_get_threepids = AsyncMock(
        return_value=list(threepids)
    )
    api._hs.get_send_email_handler.return_value.send_email = AsyncMock()
    api.account_data_manager.get_global = AsyncMock(return_value=account_data)
    api.account_data_manager.put_global = AsyncMock()
    api.is_user_admin = AsyncMock(return_value=True)
    api.create_and_send_event_into_room = AsyncMock()

    def read_templates(names, custom_template_directory=None):
        return [
            MagicMock(render=MagicMock(return_value=f"<rendered {name}>"))
            for name in names
        ]

    api.read_templates = read_templates
    return api


def _direct_push(sent: int, attempted: int | None = None):
    direct_push = MagicMock()
    direct_push._send_push = AsyncMock(
        return_value={
            "user_id": USER,
            "attempted": sent if attempted is None else attempted,
            "sent": sent,
            "failed": 0,
            "devices": {},
            "errors": [],
        }
    )
    return direct_push


class TestCategories(unittest.TestCase):
    def test_parse_preferences_tolerates_garbage(self):
        for content in (
            None,
            "x",
            [],
            {"refused": "no"},
            {"refused": [1, "not_a_category"]},
        ):
            with self.subTest(content=content):
                prefs = categories.parse_preferences(content)
                self.assertEqual(prefs.refused, frozenset())
                self.assertFalse(prefs.all_off)

    def test_credential_is_never_refused(self):
        prefs = categories.parse_preferences(
            {"all_off": True, "refused": ["credential"]}
        )
        self.assertFalse(categories.is_refused(prefs, "credential"))

    def test_global_off_covers_nudges_and_marketing_not_event_mail(self):
        prefs = categories.parse_preferences({"all_off": True})
        self.assertTrue(categories.is_refused(prefs, "activity_nudges"))
        self.assertTrue(categories.is_refused(prefs, "trial_marketing"))
        self.assertFalse(categories.is_refused(prefs, "missed_message"))
        self.assertFalse(categories.is_refused(prefs, "course_invite"))

    def test_with_refusal_only_adds(self):
        prefs = categories.parse_preferences({"refused": ["suggestions"]})
        updated = categories.with_refusal(
            prefs,
            categories=("activity_nudges",),
            now_ms=NOW_MS,
            source="unsubscribe_link",
        )
        self.assertEqual(updated["refused"], ["activity_nudges", "suggestions"])
        self.assertFalse(updated["all_off"])
        self.assertEqual(updated["updated_ts"], NOW_MS)
        updated_all = categories.with_refusal(
            prefs, all_off=True, now_ms=NOW_MS, source="app"
        )
        self.assertTrue(updated_all["all_off"])
        self.assertEqual(updated_all["refused"], ["suggestions"])


class TestTokens(unittest.TestCase):
    def test_round_trip_and_expiry(self):
        token = tokens.sign_token(
            SECRET, {"k": "click", "u": USER}, now_ms=NOW_MS, ttl_ms=1000
        )
        self.assertEqual(
            tokens.verify_token(SECRET, token, now_ms=NOW_MS + 999)["u"], USER
        )
        self.assertIsNone(tokens.verify_token(SECRET, token, now_ms=NOW_MS + 1001))

    def test_rejects_tampering_and_wrong_secret(self):
        token = tokens.sign_token(
            SECRET, {"k": "unsub", "u": USER}, now_ms=NOW_MS, ttl_ms=1000
        )
        payload_part, signature = token.split(".")
        self.assertIsNone(
            tokens.verify_token(SECRET, payload_part + "x." + signature, now_ms=NOW_MS)
        )
        self.assertIsNone(tokens.verify_token(b"other", token, now_ms=NOW_MS))
        for junk in (None, "", "no-dot", ".", "a.b.c"):
            with self.subTest(junk=junk):
                self.assertIsNone(tokens.verify_token(SECRET, junk, now_ms=NOW_MS))


class TestAppUrl(unittest.TestCase):
    def test_activity_link_shapes(self):
        self.assertEqual(
            app_url("https://app.x/", activity_id=None, session_room_id=None),
            "https://app.x/",
        )
        self.assertEqual(
            app_url("https://app.x", activity_id="act-1", session_room_id="!s:x"),
            "https://app.x/act-1?roomid=%21s%3Ax",
        )


class TestConfig(unittest.TestCase):
    def test_defaults_and_validation(self):
        config = PangeaChat.parse_config(
            {"cms_base_url": "x", "cms_service_api_key": "y"}
        )
        self.assertFalse(config.nudge_email_enabled)
        self.assertTrue(config.nudge_suppress_notice_push_rules)
        self.assertEqual(config.nudge_token_ttl_days, 90)
        with self.assertRaisesRegex(ValueError, "nudge_token_ttl_days"):
            PangeaChat.parse_config(
                {
                    "cms_base_url": "x",
                    "cms_service_api_key": "y",
                    "nudge_token_ttl_days": 0,
                }
            )
        with self.assertRaisesRegex(ValueError, "nudge_email_enabled"):
            PangeaChat.parse_config(
                {
                    "cms_base_url": "x",
                    "cms_service_api_key": "y",
                    "nudge_email_enabled": "yes",
                }
            )


class TestPushRule(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_confirmed_users_for_tests()

    async def test_installs_once_then_remembers(self):
        api = MagicMock()
        store = api._hs.get_datastores.return_value.main
        store.db_pool.simple_select_one_onecol = AsyncMock(return_value=None)
        store.add_push_rule = AsyncMock()
        self.assertTrue(await ensure_bot_notice_push_rule(api, USER))
        self.assertFalse(await ensure_bot_notice_push_rule(api, USER))
        store.add_push_rule.assert_awaited_once()
        kwargs = store.add_push_rule.await_args.kwargs
        self.assertEqual(kwargs["actions"], ["dont_notify"])
        self.assertEqual(kwargs["conditions"][0]["pattern"], "p.room.notice")
        api._hs.get_push_rules_handler.return_value.notify_user.assert_called_once_with(
            USER
        )

    async def test_lost_insert_race_is_success(self):
        api = MagicMock()
        store = api._hs.get_datastores.return_value.main
        store.db_pool.simple_select_one_onecol = AsyncMock(
            side_effect=[None, "global/override/p.rule.bot_notice"]
        )
        store.add_push_rule = AsyncMock(side_effect=RuntimeError("unique violation"))
        self.assertFalse(await ensure_bot_notice_push_rule(api, USER))


class TestDeliverNudge(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_confirmed_users_for_tests()

    def _handler(self, api, config=None, sent=0, attempted=None):
        return DeliverNudge(api, config or _config(), _direct_push(sent, attempted))

    def _body(self, **overrides):
        body = {
            "user_id": USER,
            "category": "activity_nudges",
            "variant": "do_activity",
            "body": "Ready for an activity?",
            "notice_event_id": "$notice:x",
            "notice_room_id": "!dm:x",
            "content": {"pangea.activity.id": "act-1"},
            "activity_id": "act-1",
        }
        body.update(overrides)
        return body

    def test_validate(self):
        self.assertEqual(DeliverNudge._validate({}), "Missing user_id")
        self.assertIn(
            "category",
            DeliverNudge._validate(
                {"user_id": USER, "category": "credential", "body": "x"}
            ),
        )
        self.assertEqual(
            DeliverNudge._validate({"user_id": USER, "category": "suggestions"}),
            "Missing body",
        )
        self.assertIsNone(DeliverNudge._validate(self._body()))

    async def test_refused_category_sends_nothing(self):
        api = _api(account_data={"refused": ["activity_nudges"]})
        handler = self._handler(api, sent=1)
        with patch(
            "synapse_pangea_chat.nudge_delivery.deliver.ensure_bot_notice_push_rule",
            new=AsyncMock(),
        ) as rule:
            result = await handler.deliver(self._body())
        self.assertEqual(result["channel"], CHANNEL_REFUSED)
        self.assertEqual(result["reason"], "category_refused")
        handler._direct_push._send_push.assert_not_awaited()
        rule.assert_not_awaited()

    async def test_all_off_reason(self):
        api = _api(account_data={"all_off": True})
        result = await self._handler(api, sent=1).deliver(self._body())
        self.assertEqual(result["channel"], CHANNEL_REFUSED)
        self.assertEqual(result["reason"], "all_off")

    async def test_in_app_skips_push_and_email(self):
        api = _api(currently_active=True)
        handler = self._handler(api, sent=1)
        with patch(
            "synapse_pangea_chat.nudge_delivery.deliver.ensure_bot_notice_push_rule",
            new=AsyncMock(return_value=True),
        ):
            result = await handler.deliver(self._body())
        self.assertEqual(result["channel"], CHANNEL_IN_APP)
        self.assertTrue(result["push_rule_installed"])
        handler._direct_push._send_push.assert_not_awaited()
        api._hs.get_send_email_handler.return_value.send_email.assert_not_awaited()

    async def test_working_pusher_means_push_only(self):
        api = _api(
            threepids=[SimpleNamespace(medium="email", address="alice@example.test")]
        )
        handler = self._handler(api, sent=1)
        with patch(
            "synapse_pangea_chat.nudge_delivery.deliver.ensure_bot_notice_push_rule",
            new=AsyncMock(return_value=False),
        ):
            result = await handler.deliver(self._body())
        self.assertEqual(result["channel"], CHANNEL_PUSH)
        kwargs = handler._direct_push._send_push.await_args.kwargs
        self.assertEqual(kwargs["pusher_kinds"], ("http",))
        api._hs.get_send_email_handler.return_value.send_email.assert_not_awaited()

    async def test_no_pusher_falls_back_to_email_with_unsubscribe_headers(self):
        api = _api(
            threepids=[SimpleNamespace(medium="email", address="alice@example.test")]
        )
        handler = self._handler(api, sent=0, attempted=0)
        with patch(
            "synapse_pangea_chat.nudge_delivery.deliver.ensure_bot_notice_push_rule",
            new=AsyncMock(return_value=False),
        ):
            result = await handler.deliver(self._body(title="Your next activity"))
        self.assertEqual(result["channel"], CHANNEL_EMAIL)
        send = api._hs.get_send_email_handler.return_value.send_email
        send.assert_awaited_once()
        kwargs = send.await_args.kwargs
        self.assertEqual(kwargs["email_address"], "alice@example.test")
        self.assertEqual(kwargs["subject"], "Your next activity")
        headers = kwargs["additional_headers"]
        self.assertIn(
            "/_synapse/client/pangea/v1/unsubscribe?t=", headers["List-Unsubscribe"]
        )
        self.assertEqual(headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")

    async def test_email_disabled_reports_reason(self):
        api = _api(
            threepids=[SimpleNamespace(medium="email", address="alice@example.test")]
        )
        handler = self._handler(
            api, config=_config(nudge_email_enabled=False), sent=0, attempted=0
        )
        with patch(
            "synapse_pangea_chat.nudge_delivery.deliver.ensure_bot_notice_push_rule",
            new=AsyncMock(return_value=False),
        ):
            result = await handler.deliver(self._body())
        self.assertEqual(result["channel"], CHANNEL_NONE)
        self.assertEqual(result["reason"], "email_disabled")

    async def test_no_address_reports_reason_and_failed_push_is_named(self):
        api = _api(threepids=[SimpleNamespace(medium="msisdn", address="+1555")])
        handler = self._handler(api, sent=0, attempted=1)
        with patch(
            "synapse_pangea_chat.nudge_delivery.deliver.ensure_bot_notice_push_rule",
            new=AsyncMock(return_value=False),
        ):
            result = await handler.deliver(self._body())
        self.assertEqual(result["channel"], CHANNEL_NONE)
        self.assertEqual(result["reason"], "push_failed_then_no_email_address")


class _FakeRequest:
    def __init__(self, *, args=None, body=b"", host="203.0.113.5"):
        self.args = args or {}
        self.content = BytesIO(body)
        self._host = host
        self.responses = []

    def getClientAddress(self):
        return SimpleNamespace(host=self._host)


def _capture_html(monkey_target, request_holder):
    def fake(request, code, html):
        request_holder.append((code, html))

    return patch(monkey_target, new=fake)


class TestUnsubscribe(unittest.IsolatedAsyncioTestCase):
    def _handler(self, api):
        return NudgeUnsubscribe(api, _config())

    def _token(self, category="activity_nudges", kind="unsub"):
        return tokens.sign_token(
            SECRET, {"k": kind, "u": USER, "c": category}, now_ms=NOW_MS, ttl_ms=10_000
        )

    async def test_get_renders_confirmation_not_action(self):
        api = _api()
        handler = self._handler(api)
        captured = []
        with _capture_html(
            "synapse_pangea_chat.nudge_delivery.unsubscribe.respond_with_html", captured
        ):
            await handler._async_render_GET(
                _FakeRequest(args={b"t": [self._token().encode()]})
            )
        self.assertEqual(captured[0][0], 200)
        self.assertIn("nudge_unsubscribe_confirm.html", captured[0][1])
        api.account_data_manager.put_global.assert_not_awaited()

    async def test_get_with_bad_token_is_400(self):
        api = _api()
        captured = []
        with _capture_html(
            "synapse_pangea_chat.nudge_delivery.unsubscribe.respond_with_html", captured
        ):
            await self._handler(api)._async_render_GET(
                _FakeRequest(args={b"t": [b"junk"]})
            )
        self.assertEqual(captured[0][0], 400)

    async def test_post_form_refuses_category(self):
        api = _api(account_data={"refused": ["suggestions"]})
        captured = []
        body = f"t={self._token()}&scope=category".encode()
        with _capture_html(
            "synapse_pangea_chat.nudge_delivery.unsubscribe.respond_with_html", captured
        ):
            await self._handler(api)._async_render_POST(_FakeRequest(body=body))
        self.assertEqual(captured[0][0], 200)
        put = api.account_data_manager.put_global
        put.assert_awaited_once()
        user_id, data_type, content = put.await_args.args
        self.assertEqual(user_id, USER)
        self.assertEqual(
            data_type, categories.COMMUNICATION_PREFERENCES_ACCOUNT_DATA_TYPE
        )
        self.assertEqual(content["refused"], ["activity_nudges", "suggestions"])
        self.assertFalse(content["all_off"])
        self.assertEqual(content["source"], "unsubscribe_link")

    async def test_one_click_post_uses_query_token(self):
        api = _api()
        captured = []
        request = _FakeRequest(
            args={b"t": [self._token().encode()]}, body=b"List-Unsubscribe=One-Click"
        )
        with _capture_html(
            "synapse_pangea_chat.nudge_delivery.unsubscribe.respond_with_html", captured
        ):
            await self._handler(api)._async_render_POST(request)
        self.assertEqual(captured[0][0], 200)
        content = api.account_data_manager.put_global.await_args.args[2]
        self.assertEqual(content["refused"], ["activity_nudges"])

    async def test_post_scope_all_sets_global_off(self):
        api = _api()
        captured = []
        body = f"t={self._token()}&scope=all".encode()
        with _capture_html(
            "synapse_pangea_chat.nudge_delivery.unsubscribe.respond_with_html", captured
        ):
            await self._handler(api)._async_render_POST(_FakeRequest(body=body))
        content = api.account_data_manager.put_global.await_args.args[2]
        self.assertTrue(content["all_off"])

    async def test_click_token_is_not_an_unsubscribe_token(self):
        api = _api()
        captured = []
        body = f"t={self._token(kind='click')}".encode()
        with _capture_html(
            "synapse_pangea_chat.nudge_delivery.unsubscribe.respond_with_html", captured
        ):
            await self._handler(api)._async_render_POST(_FakeRequest(body=body))
        self.assertEqual(captured[0][0], 400)
        api.account_data_manager.put_global.assert_not_awaited()

    async def test_preferences_form_adds_multiple_refusals_without_reenabling(self):
        from urllib.parse import urlencode

        api = _api(account_data={"refused": ["suggestions"], "all_off": False})
        enabled = sorted(
            categories.GLOBAL_OFF_CATEGORIES - {"activity_nudges", "teacher_setup"}
        )
        body = urlencode(
            {
                "t": self._token(),
                "scope": "preferences",
                "reminders_enabled": "yes",
                "enabled": enabled,
            },
            doseq=True,
        ).encode()
        captured = []
        with _capture_html(
            "synapse_pangea_chat.nudge_delivery.unsubscribe.respond_with_html", captured
        ):
            await self._handler(api)._async_render_POST(_FakeRequest(body=body))
        self.assertEqual(captured[0][0], 200)
        content = api.account_data_manager.put_global.await_args.args[2]
        self.assertEqual(
            content["refused"], ["activity_nudges", "suggestions", "teacher_setup"]
        )
        self.assertFalse(content["all_off"])

    async def test_preferences_form_cannot_clear_existing_global_off(self):
        from urllib.parse import urlencode

        api = _api(account_data={"all_off": True})
        body = urlencode(
            {
                "t": self._token(),
                "scope": "preferences",
                "reminders_enabled": "yes",
                "enabled": sorted(categories.GLOBAL_OFF_CATEGORIES),
            },
            doseq=True,
        ).encode()
        with _capture_html(
            "synapse_pangea_chat.nudge_delivery.unsubscribe.respond_with_html", []
        ):
            await self._handler(api)._async_render_POST(_FakeRequest(body=body))
        self.assertTrue(
            api.account_data_manager.put_global.await_args.args[2]["all_off"]
        )

    async def test_preferences_form_rejects_unknown_categories(self):
        api = _api()
        captured = []
        body = f"t={self._token()}&scope=preferences&enabled=credential".encode()
        with _capture_html(
            "synapse_pangea_chat.nudge_delivery.unsubscribe.respond_with_html", captured
        ):
            await self._handler(api)._async_render_POST(_FakeRequest(body=body))
        self.assertEqual(captured[0][0], 400)
        api.account_data_manager.put_global.assert_not_awaited()


class TestClick(unittest.IsolatedAsyncioTestCase):
    def _token(self, **extra):
        payload = {
            "k": "click",
            "u": USER,
            "e": "$notice:x",
            "r": "!dm:x",
            "v": "do_activity",
            "a": "act-1",
            "s": "!s:x",
        }
        payload.update(extra)
        return tokens.sign_token(SECRET, payload, now_ms=NOW_MS, ttl_ms=10_000)

    async def test_records_open_then_redirects_to_activity(self):
        api = _api()
        handler = NudgeClick(api, _config())
        redirects = []
        with patch(
            "synapse_pangea_chat.nudge_delivery.click.respond_with_redirect",
            new=lambda request, url, *a, **k: redirects.append(url),
        ):
            await handler._async_render_GET(
                _FakeRequest(args={b"t": [self._token().encode()]})
            )
        self.assertEqual(redirects, [b"https://app.example.test/act-1?roomid=%21s%3Ax"])
        event = api.create_and_send_event_into_room.await_args.args[0]
        self.assertEqual(event["type"], "p.room.notice.opened")
        self.assertEqual(event["sender"], USER)
        self.assertEqual(event["room_id"], "!dm:x")
        self.assertEqual(event["content"]["notification_event_id"], "$notice:x")
        self.assertEqual(event["content"]["check_in_type"], "do_activity")
        self.assertEqual(event["content"]["opened_at_ts"], NOW_MS)

    async def test_bad_token_redirects_home_without_record(self):
        api = _api()
        handler = NudgeClick(api, _config())
        redirects = []
        with patch(
            "synapse_pangea_chat.nudge_delivery.click.respond_with_redirect",
            new=lambda request, url, *a, **k: redirects.append(url),
        ):
            await handler._async_render_GET(_FakeRequest(args={b"t": [b"junk"]}))
        self.assertEqual(redirects, [b"https://app.example.test/"])
        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_record_failure_still_redirects(self):
        api = _api()
        api.create_and_send_event_into_room = AsyncMock(
            side_effect=RuntimeError("not in room")
        )
        handler = NudgeClick(api, _config())
        redirects = []
        with patch(
            "synapse_pangea_chat.nudge_delivery.click.respond_with_redirect",
            new=lambda request, url, *a, **k: redirects.append(url),
        ):
            await handler._async_render_GET(
                _FakeRequest(args={b"t": [self._token().encode()]})
            )
        self.assertEqual(len(redirects), 1)
        self.assertTrue(redirects[0].startswith(b"https://app.example.test/act-1"))


if __name__ == "__main__":
    unittest.main()


class TestParsePreferencesFrozen(unittest.TestCase):
    def test_frozen_tuple_refusals_are_read(self) -> None:
        from synapse_pangea_chat.nudge_delivery.categories import parse_preferences

        prefs = parse_preferences(
            {"refused": ("activity_nudges", "bogus"), "all_off": False}
        )
        self.assertEqual(prefs.refused, frozenset({"activity_nudges"}))


class TestPrepareNudge(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_confirmed_users_for_tests()

    async def test_installs_rule_and_reports(self):
        from synapse_pangea_chat.nudge_delivery.prepare import PrepareNudge

        handler = PrepareNudge(_api(), _config())
        with patch(
            "synapse_pangea_chat.nudge_delivery.prepare.ensure_bot_notice_push_rule",
            new=AsyncMock(return_value=True),
        ) as ensure:
            result = await handler.prepare(USER)
        ensure.assert_awaited_once()
        self.assertEqual(
            result,
            {"user_id": USER, "push_rule_installed": True, "suppression_enabled": True},
        )

    async def test_suppression_off_installs_nothing(self):
        from synapse_pangea_chat.nudge_delivery.prepare import PrepareNudge

        handler = PrepareNudge(_api(), _config(nudge_suppress_notice_push_rules=False))
        with patch(
            "synapse_pangea_chat.nudge_delivery.prepare.ensure_bot_notice_push_rule",
            new=AsyncMock(return_value=True),
        ) as ensure:
            result = await handler.prepare(USER)
        ensure.assert_not_awaited()
        self.assertFalse(result["suppression_enabled"])


class TestInAppRequiresOnline(unittest.IsolatedAsyncioTestCase):
    async def test_stale_currently_active_while_offline_is_not_in_app(self):
        api = _api(currently_active=True)
        api._hs.get_presence_handler.return_value.current_state_for_user = AsyncMock(
            return_value=SimpleNamespace(state="offline", currently_active=True)
        )
        handler = DeliverNudge(api, _config(), _direct_push(0, 0))
        self.assertFalse(await handler._is_in_app(USER))


class TestEmailSubjectIsOneLine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_confirmed_users_for_tests()

    async def test_multiline_body_becomes_a_single_line_subject(self):
        api = _api(
            threepids=[SimpleNamespace(medium="email", address="alice@example.test")]
        )
        handler = DeliverNudge(
            api, _config(nudge_email_enabled=True), _direct_push(0, 0)
        )
        with patch(
            "synapse_pangea_chat.nudge_delivery.deliver.ensure_bot_notice_push_rule",
            new=AsyncMock(return_value=False),
        ):
            result = await handler.deliver(
                {
                    "user_id": USER,
                    "category": "activity_nudges",
                    "variant": "do_activity",
                    "body": "Line one\r\nline two\n\nline three",
                }
            )
        self.assertEqual(result["channel"], CHANNEL_EMAIL)
        send_email = api._hs.get_send_email_handler.return_value.send_email
        subject = send_email.await_args.kwargs.get("subject")
        if subject is None:
            subject = next(
                a
                for a in send_email.await_args.args
                if isinstance(a, str) and "Line one" in a
            )
        self.assertEqual(subject, "Line one line two line three")


class TestReviewRoundThree(unittest.TestCase):
    def test_non_ascii_token_is_invalid_not_an_error(self):
        from synapse_pangea_chat.nudge_delivery.tokens import verify_token

        self.assertIsNone(verify_token(b"s", "é.x", now_ms=NOW_MS))
        self.assertIsNone(verify_token(b"s", "abc.é", now_ms=NOW_MS))

    def test_email_requires_postal_address(self):
        from synapse_pangea_chat import PangeaChat

        base = {"cms_base_url": "x", "cms_service_api_key": "y"}
        with self.assertRaisesRegex(ValueError, "nudge_email_postal_address"):
            PangeaChat.parse_config({**base, "nudge_email_enabled": True})
        config = PangeaChat.parse_config(
            {
                **base,
                "nudge_email_enabled": True,
                "nudge_email_postal_address": "1 Main St",
            }
        )
        self.assertTrue(config.nudge_email_enabled)
