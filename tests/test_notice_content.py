"""Structured content, channel restrictions, reservation and failure behavior."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from synapse_pangea_chat.notice_delivery.deliver import DeliverNotice
from synapse_pangea_chat.notice_delivery.delivery_log import (
    DeliveryConflict,
    DeliveryLog,
)
from synapse_pangea_chat.notice_delivery.rate_limit import AdminRateLimiter
from synapse_pangea_chat.notice_delivery.request import NoticeRequest
from tests.test_notice_delivery_unit import USER, _api, _config, _direct_push


def request_body(method="use-available"):
    return {
        "user_id": USER,
        "category": "activity_nudges",
        "variant": "do_activity",
        "notice_event_id": "$notice:test",
        "notice_room_id": "!dm:test",
        "delivery_method": method,
        "push": {
            "title": "An activity",
            "body": "Try this",
            "content": {"pangea.activity_id": "activity-1"},
        },
        "email": {
            "subject": "Try an activity",
            "receiving_reason": "You're enrolled in Spanish <101>.",
            "html": '<a href="{{cta_url}}">Open</a><p>{{receiving_reason}}</p><a href="{{unsubscribe_url}}">Unsubscribe</a><p>{{postal_address}}</p>',
            "text": "Open {{cta_url}}\n{{receiving_reason}}\nUnsubscribe {{unsubscribe_url}}\n{{postal_address}}",
        },
        "activity_id": "activity-1",
        "log": {
            "run": {
                "run_id": "test-run",
                "runner": "skill",
                "funnel": "learner",
                "decided_at": "2026-09-27T12:00:00Z",
            },
            "state": {"engagement_status": "inactive"},
            "copy_key": "do_activity.v1",
        },
    }


class TestContentValidation(unittest.TestCase):
    def test_missing_channel_content_rejected(self):
        for key in ("push", "email", "log", "notice_room_id", "notice_event_id"):
            body = request_body()
            del body[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                NoticeRequest.parse(body)

    def test_each_email_body_requires_all_server_slots(self):
        for field in ("html", "text"):
            for slot in (
                "{{cta_url}}",
                "{{unsubscribe_url}}",
                "{{receiving_reason}}",
                "{{postal_address}}",
            ):
                body = request_body()
                body["email"][field] = body["email"][field].replace(slot, "")
                with self.subTest(field=field, slot=slot), self.assertRaises(
                    ValueError
                ):
                    NoticeRequest.parse(body)

    def test_forced_content_and_category_restrictions(self):
        body = request_body("email-only")
        del body["push"]
        self.assertIsNone(NoticeRequest.parse(body).push)
        body = request_body("push-only")
        del body["email"]
        self.assertIsNone(NoticeRequest.parse(body).email)
        for category in ("teacher_setup", "weekly_class_report", "campaigns"):
            body["category"] = category
            with self.assertRaisesRegex(ValueError, "email only"):
                NoticeRequest.parse(body)

    def test_allow_notifications_cannot_escape_in_app(self):
        body = request_body("email-only")
        body.update(category="onboarding_nudges", variant="allow_notifications")
        with self.assertRaises(ValueError):
            NoticeRequest.parse(body)
        body["delivery_method"] = "use-available"
        self.assertEqual(NoticeRequest.parse(body).method, "in-app-only")

    def test_credential_rejected(self):
        body = request_body()
        body["category"] = "credential"
        with self.assertRaises(ValueError):
            NoticeRequest.parse(body)

    def test_admin_burst_and_refill(self):
        limiter = AdminRateLimiter(600, 100)
        with patch(
            "synapse_pangea_chat.notice_delivery.rate_limit.time.monotonic",
            return_value=10,
        ):
            self.assertTrue(
                all(not limiter.is_rate_limited("operator") for _ in range(100))
            )
            self.assertTrue(limiter.is_rate_limited("operator"))
            self.assertFalse(limiter.is_rate_limited("other"))
        with patch(
            "synapse_pangea_chat.notice_delivery.rate_limit.time.monotonic",
            return_value=11,
        ):
            self.assertTrue(
                all(not limiter.is_rate_limited("operator") for _ in range(10))
            )
            self.assertTrue(limiter.is_rate_limited("operator"))


class TestStructuredDelivery(unittest.IsolatedAsyncioTestCase):
    def handler(self, *, active=False, sent=0, preferences=None):
        api = _api(
            currently_active=active,
            account_data=preferences,
            threepids=[SimpleNamespace(medium="email", address="alice@example.test")],
        )
        store = api._hs.get_datastores.return_value.main
        store.get_event = AsyncMock(
            return_value=SimpleNamespace(
                type="p.room.notice", room_id="!dm:test", sender="@admin:test"
            )
        )
        store.get_local_current_membership_for_user_in_room = AsyncMock(
            return_value=("join", "$join")
        )
        handler = DeliverNotice(
            api, _config(notice_suppress_notice_push_rules=False), _direct_push(sent, 1)
        )
        handler._delivery_log.reserve = AsyncMock(return_value=("42", None))
        handler._delivery_log.finish = AsyncMock()
        return handler, api

    async def test_force_email_while_active_and_replaces_slots_without_jinja(self):
        handler, api = self.handler(active=True, sent=1)
        body = request_body("email-only")
        body["email"]["html"] += "{% dangerous_template_code %}"
        result = await handler.deliver(body)
        self.assertEqual(result["channel"], "email")
        handler._direct_push._send_push.assert_not_awaited()
        kwargs = (
            api._hs.get_send_email_handler.return_value.send_email.await_args.kwargs
        )
        self.assertIn("Spanish &lt;101&gt;", kwargs["html"])
        self.assertIn("Spanish <101>", kwargs["text"])
        self.assertIn("{% dangerous_template_code %}", kwargs["html"])
        self.assertNotIn("{{cta_url}}", kwargs["html"])
        self.assertIn("/_synapse/client/pangea/v1/n?t=", kwargs["text"])
        handler._delivery_log.finish.assert_awaited_once()

    async def test_availability_prefers_in_app(self):
        handler, api = self.handler(active=True, sent=1)
        self.assertEqual((await handler.deliver(request_body()))["channel"], "in_app")
        handler._direct_push._send_push.assert_not_awaited()
        api._hs.get_send_email_handler.return_value.send_email.assert_not_awaited()

    async def test_force_push_failure_never_emails(self):
        handler, api = self.handler()
        result = await handler.deliver(request_body("push-only"))
        self.assertEqual(result["reason"], "push_failed")
        api._hs.get_send_email_handler.return_value.send_email.assert_not_awaited()

    async def test_push_title_and_metadata_forwarded(self):
        handler, _ = self.handler(sent=1)
        self.assertEqual((await handler.deliver(request_body()))["channel"], "push")
        payload = handler._direct_push._send_push.await_args.args[2]
        self.assertEqual(payload["title"], "An activity")
        self.assertEqual(payload["content"], {"pangea.activity_id": "activity-1"})

    async def test_forced_email_still_refused_and_logged(self):
        handler, api = self.handler(preferences={"all_off": True})
        result = await handler.deliver(request_body("email-only"))
        self.assertEqual(result["channel"], "refused")
        api._hs.get_send_email_handler.return_value.send_email.assert_not_awaited()
        handler._delivery_log.finish.assert_awaited_once()

    async def test_duplicate_does_not_send_again(self):
        handler, api = self.handler()
        handler._delivery_log.reserve.return_value = ("42", {"channel": "email"})
        self.assertTrue((await handler.deliver(request_body()))["duplicate"])
        api._hs.get_send_email_handler.return_value.send_email.assert_not_awaited()
        handler._delivery_log.finish.assert_not_awaited()

    async def test_invalid_notice_never_reserved_or_sent(self):
        handler, api = self.handler()
        handler._store.get_event.return_value = None
        with self.assertRaises(ValueError):
            await handler.deliver(request_body())
        handler._delivery_log.reserve.assert_not_awaited()

    async def test_log_outage_prevents_send(self):
        handler, api = self.handler()
        handler._delivery_log.reserve.side_effect = RuntimeError("CMS unavailable")
        with self.assertRaises(RuntimeError):
            await handler.deliver(request_body())
        api._hs.get_send_email_handler.return_value.send_email.assert_not_awaited()

    async def test_caller_owned_record_is_never_reserved_or_finished(self):
        handler, api = self.handler(active=False, sent=0)
        body = {**request_body("email-only"), "notification_log_id": "row-7"}
        result = await handler.deliver(body)
        handler._delivery_log.reserve.assert_not_awaited()
        handler._delivery_log.finish.assert_not_awaited()
        self.assertEqual(result["channel"], "email")
        self.assertEqual(result["notification_log_id"], "row-7")
        self.assertEqual(result["log_status"], "caller")
        self.assertFalse(result["duplicate"])

    async def test_caller_owned_record_needs_no_log_context(self):
        body = {**request_body("email-only"), "notification_log_id": "row-7"}
        del body["log"]
        req = NoticeRequest.parse(body)
        self.assertIsNone(req.log)
        self.assertTrue(req.caller_owns_record)
        without_either = request_body("email-only")
        del without_either["log"]
        with self.assertRaises(ValueError):
            NoticeRequest.parse(without_either)

    async def test_post_send_log_outage_reports_actual_delivery(self):
        handler, api = self.handler()
        handler._delivery_log.finish.side_effect = RuntimeError("CMS unavailable")
        result = await handler.deliver(request_body("email-only"))
        self.assertEqual(result["channel"], "email")
        self.assertEqual(result["log_status"], "pending_reconciliation")
        api._hs.get_send_email_handler.return_value.send_email.assert_awaited_once()


class TestDeliveryLog(unittest.IsolatedAsyncioTestCase):
    async def test_pending_duplicate_cannot_send(self):
        log = DeliveryLog(_api(), _config())
        req = NoticeRequest.parse(request_body())
        log._request = AsyncMock(
            side_effect=[
                (409, {}),
                (200, {"docs": [{"id": "42", "decision": log._decision(req)}]}),
            ]
        )
        with self.assertRaises(DeliveryConflict):
            await log.reserve(req)

    async def test_finish_strips_device_tokens(self):
        log = DeliveryLog(_api(), _config())
        log._request = AsyncMock(return_value=(200, {}))
        result = {
            "user_id": USER,
            "category": "activity_nudges",
            "channel": "push",
            "reason": None,
            "email": None,
            "push_rule_installed": True,
            "push": {
                "attempted": 1,
                "sent": 1,
                "failed": 0,
                "devices": {"secret": "pushkey"},
            },
        }
        await log.finish(
            "42", NoticeRequest.parse(request_body()), copy.deepcopy(result)
        )
        stored = log._request.await_args.args[2]["decision"]["delivery"]["response"]
        self.assertNotIn("devices", stored["push"])
        self.assertEqual(stored["push"]["sent"], 1)
