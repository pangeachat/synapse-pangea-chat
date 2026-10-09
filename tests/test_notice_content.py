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
        # The transport claim keeps its own small table; give it real SQL and
        # close it with the test, or the e2e base's ResourceWarning guard trips
        # when the connection is collected during a later test.
        import weakref

        from tests.test_notice_schedule import SQLPool

        pool = SQLPool()
        # Suites that borrow this fixture through a throwaway instance never run
        # its cleanups, so the pool closes with the handler it belongs to.
        weakref.finalize(handler, pool.db.close)
        self.addCleanup(pool.db.close)
        handler._transport_claims._db = pool
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
        self.assertNotIn("duplicate", result)

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


class TestCallerOwnedTransportClaim(unittest.IsolatedAsyncioTestCase):
    """A caller-owned record skips the ledger, never the transport claim: one delivery call
    per decision means one transport (engagement-system doc)."""

    def handler(self):
        fixture = TestStructuredDelivery()
        handler, api = fixture.handler(active=False, sent=0)
        self.addCleanup(fixture.doCleanups)
        return handler, api

    async def test_replaying_the_same_request_sends_once_and_answers_with_the_result(
        self,
    ):
        handler, api = self.handler()
        body = {**request_body("email-only"), "notification_log_id": "row-7"}
        first = await handler.deliver(body)
        second = await handler.deliver(dict(body))
        send = api._hs.get_send_email_handler.return_value.send_email
        self.assertEqual(send.await_count, 1)
        self.assertEqual(first["channel"], "email")
        self.assertNotIn("duplicate", first)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["channel"], "email")
        self.assertEqual(second["log_status"], "caller")
        handler._delivery_log.reserve.assert_not_awaited()

    async def test_a_different_payload_under_the_same_row_is_a_conflict(self):
        handler, api = self.handler()
        body = {**request_body("email-only"), "notification_log_id": "row-7"}
        await handler.deliver(body)
        changed = {**body, "email": {**body["email"], "subject": "Something else"}}
        with self.assertRaises(DeliveryConflict):
            await handler.deliver(changed)
        self.assertEqual(
            api._hs.get_send_email_handler.return_value.send_email.await_count, 1
        )

    async def test_a_transport_failure_after_the_claim_blocks_a_retry_until_reconciled(
        self,
    ):
        handler, api = self.handler()
        api._hs.get_send_email_handler.return_value.send_email.side_effect = (
            RuntimeError("smtp down")
        )
        body = {**request_body("email-only"), "notification_log_id": "row-7"}
        # A failed email is a result with a send_failed reason, not an exception; the claim
        # stays pending, so a retry reconciles instead of sending again.
        first = await handler.deliver(body)
        self.assertEqual(first["channel"], "none")
        self.assertTrue(first["reason"].endswith("send_failed"))
        with self.assertRaises(DeliveryConflict):
            await handler.deliver(dict(body))
        self.assertEqual(
            api._hs.get_send_email_handler.return_value.send_email.await_count, 1
        )

    async def test_schedule_key_is_the_row_whether_or_not_context_travels(self):
        with_context = NoticeRequest.parse(
            {**request_body("email-only"), "notification_log_id": "row-7"}
        )
        without = {**request_body("email-only"), "notification_log_id": "row-7"}
        del without["log"]
        self.assertEqual(
            with_context.schedule_key(), NoticeRequest.parse(without).schedule_key()
        )
        self.assertIn("record:row-7", with_context.schedule_key())

    async def test_eligibility_conditions_need_the_decision_context(self):
        body = {
            **request_body("email-only"),
            "notification_log_id": "row-7",
            "scheduled_at": "2026-10-10T10:00:00+00:00",
            "sender_id": "@admin:test",
            "notice_content": {"notice_type": "check_in"},
            "eligibility": {"recipient_not_returned": True},
        }
        del body["notice_event_id"]
        del body["log"]
        with self.assertRaises(ValueError):
            NoticeRequest.parse(body)


class TestCallerOwnedBoundsAndPaths(unittest.IsolatedAsyncioTestCase):
    def test_id_bounds(self):
        for bad in ("", "x" * 129, 7, None):
            body = {**request_body("email-only"), "notification_log_id": bad}
            with self.subTest(bad=bad):
                if bad is None:
                    # An explicit null is the same as absent: the module records.
                    self.assertFalse(NoticeRequest.parse(body).caller_owns_record)
                else:
                    with self.assertRaises(ValueError):
                        NoticeRequest.parse(body)

    async def test_push_replay_sends_once(self):
        fixture = TestStructuredDelivery()
        handler, api = fixture.handler(active=False, sent=1)
        self.addCleanup(fixture.doCleanups)
        body = {**request_body("push-only"), "notification_log_id": "row-9"}
        first = await handler.deliver(body)
        second = await handler.deliver(dict(body))
        self.assertEqual(first["channel"], "push")
        self.assertNotIn("duplicate", first)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["push"], {"attempted": 1, "sent": 1, "failed": 0})

    async def test_scheduled_request_with_an_id_and_no_log_enqueues_and_fires(self):
        from synapse_pangea_chat.notice_delivery.schedule import NoticeSchedule
        from tests.test_notice_delivery_unit import NOW_MS
        from tests.test_notice_schedule import SQLPool, scheduled_body

        api = _api()
        pool = SQLPool()
        self.addCleanup(pool.db.close)
        api._hs.get_datastores.return_value.main.db_pool = pool
        execute = AsyncMock(return_value={"channel": "email", "log_status": "caller"})
        queue = NoticeSchedule(api, execute)
        body = {**scheduled_body(), "notification_log_id": "row-11"}
        del body["log"]
        accepted = await queue.enqueue(body)
        self.assertEqual(accepted["status"], "queued")
        api._hs.get_clock.return_value.time_msec.return_value = NOW_MS + 60000
        await queue.run_due()
        execute.assert_awaited_once()
        self.assertEqual(
            (await queue.get(accepted["schedule_id"]))["status"], "complete"
        )


class TestCallerOwnedEventPersistedBeforeTransport(unittest.IsolatedAsyncioTestCase):
    async def test_created_notice_event_is_on_the_claim_when_transport_starts(self):
        from tests.test_notice_schedule import scheduled_body

        fixture = TestStructuredDelivery()
        handler, api = fixture.handler(active=False, sent=0)
        pool = handler._transport_claims._db
        self.addCleanup(fixture.doCleanups)
        api.create_and_send_event_into_room = AsyncMock(
            return_value=SimpleNamespace(event_id="$created")
        )
        seen = {}

        async def send_email(**kwargs):
            seen["row"] = pool.db.execute(
                "SELECT phase, notice_event_id FROM pangea_notice_transport_claim WHERE record_id = ?",
                ("row-13",),
            ).fetchone()

        api._hs.get_send_email_handler.return_value.send_email = AsyncMock(
            side_effect=send_email
        )
        body = {
            **scheduled_body(),
            "notification_log_id": "row-13",
            "_schedule_id": "sch-1",
        }
        with patch.object(
            handler, "_validate_scheduled_target", AsyncMock(return_value=None)
        ):
            result = await handler._deliver_scheduled(body)
        self.assertEqual(result["notice_event_id"], "$created")
        self.assertEqual(seen["row"], ("claimed", "$created"))
