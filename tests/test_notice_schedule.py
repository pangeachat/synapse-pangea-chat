"""Scheduling timing, durable claims, and whole-notice side effects."""

import copy
import sqlite3
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from synapse_pangea_chat.notice_delivery.delivery_log import DeliveryConflict
from synapse_pangea_chat.notice_delivery.request import NoticeRequest
from synapse_pangea_chat.notice_delivery.schedule import NoticeSchedule
from tests import test_notice_content
from tests.test_notice_content import request_body
from tests.test_notice_delivery_unit import NOW_MS, _api


def scheduled_body():
    body = request_body("email-only")
    del body["notice_event_id"]
    body.update(
        scheduled_at=datetime.fromtimestamp(
            (NOW_MS + 60000) / 1000, timezone.utc
        ).isoformat(),
        sender_id="@admin:test",
        notice_content={"body": "Try this activity", "check_in_type": "do_activity"},
    )
    return body


class SQLPool:
    """Real SQL transactions without a Synapse reactor for queue tests."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.engine = None

    async def runInteraction(self, name, callback):
        with self.db:
            return callback(self.db.cursor())


class TestSchedule(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = _api()
        self.pool = SQLPool()
        self.addCleanup(self.pool.db.close)
        self.api._hs.get_datastores.return_value.main.db_pool = self.pool
        self.execute = AsyncMock(
            return_value={"channel": "email", "log_status": "complete"}
        )
        self.queue = NoticeSchedule(self.api, self.execute)

    async def test_no_early_delivery_and_restart_preserves_due_work(self):
        accepted = await self.queue.enqueue(scheduled_body())
        await self.queue.run_due()
        self.execute.assert_not_awaited()
        restarted = NoticeSchedule(self.api, self.execute)
        self.api._hs.get_clock.return_value.time_msec.return_value = NOW_MS + 60000
        await restarted.run_due()
        self.execute.assert_awaited_once()
        status = await restarted.get(accepted["schedule_id"])
        self.assertEqual(status["status"], "complete")
        self.assertIsNone(
            self.pool.db.execute(
                "SELECT payload FROM pangea_notice_schedule"
            ).fetchone()[0]
        )

    async def test_retry_is_deduplicated_and_changed_content_conflicts(self):
        body = scheduled_body()
        first = await self.queue.enqueue(body)
        second = await self.queue.enqueue(body)
        self.assertEqual(first["schedule_id"], second["schedule_id"])
        self.assertTrue(second["duplicate"])
        body["notice_content"]["body"] = "Different"
        with self.assertRaises(DeliveryConflict):
            await self.queue.enqueue(body)
        self.api._hs.get_clock.return_value.time_msec.return_value = NOW_MS + 60000
        await self.queue.run_due()
        await self.queue.run_due()
        self.execute.assert_awaited_once()
        self.assertTrue((await self.queue.enqueue(scheduled_body()))["duplicate"])

    async def test_interrupted_claim_is_never_replayed(self):
        accepted = await self.queue.enqueue(scheduled_body())
        self.api._hs.get_clock.return_value.time_msec.return_value = NOW_MS + 60000
        await self.pool.runInteraction("claim", self.queue._claim)
        restarted = NoticeSchedule(self.api, self.execute)
        await restarted.run_due()
        self.execute.assert_not_awaited()
        self.assertEqual(
            (await restarted.get(accepted["schedule_id"]))["status"],
            "pending_reconciliation",
        )

    async def test_send_exception_does_not_requeue(self):
        accepted = await self.queue.enqueue(scheduled_body())
        self.execute.side_effect = RuntimeError("connection lost after SMTP accepted")
        self.api._hs.get_clock.return_value.time_msec.return_value = NOW_MS + 60000
        with self.assertLogs(
            "synapse_pangea_chat.notice_delivery.schedule", level="ERROR"
        ):
            await self.queue.run_due()
        await self.queue.run_due()
        self.execute.assert_awaited_once()
        self.assertEqual(
            (await self.queue.get(accepted["schedule_id"]))["status"],
            "pending_reconciliation",
        )

    def test_rejects_existing_event_missing_timezone_or_missing_content(self):
        for changes in (
            {"notice_event_id": "$early"},
            {"scheduled_at": "2026-10-07T12:00:00"},
            {"notice_content": {}},
            {"scheduled_at": None},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                NoticeRequest.parse({**scheduled_body(), **changes})


class TestScheduledDelivery(unittest.IsolatedAsyncioTestCase):
    def handler(self):
        handler, api = test_notice_content.TestStructuredDelivery().handler()
        handler._config.notice_suppress_notice_push_rules = True
        api.create_and_send_event_into_room.return_value = SimpleNamespace(
            event_id="$deferred:test"
        )
        return handler, api

    async def test_enqueue_has_no_notice_or_transport_or_log_side_effect(self):
        handler, api = self.handler()
        handler.schedule.enqueue = AsyncMock(return_value={"status": "queued"})
        await handler.deliver(scheduled_body())
        api.create_and_send_event_into_room.assert_not_awaited()
        api._hs.get_send_email_handler.return_value.send_email.assert_not_awaited()
        handler._delivery_log.reserve.assert_not_awaited()

    async def test_preferences_changed_since_enqueue_suppress_entire_notice(self):
        handler, api = self.handler()
        api.account_data_manager.get_global.return_value = {"all_off": True}
        result = await handler._deliver_scheduled(scheduled_body())
        self.assertEqual(result["channel"], "refused")
        api.create_and_send_event_into_room.assert_not_awaited()
        api._hs.get_send_email_handler.return_value.send_email.assert_not_awaited()
        handler._delivery_log.finish.assert_awaited_once()

    async def test_lost_sender_permissions_or_membership_prevents_event(self):
        for permission in (True, False):
            handler, api = self.handler()
            api.is_user_admin.return_value = permission
            handler._store.get_local_current_membership_for_user_in_room.return_value = (
                "leave",
                None,
            )
            result = await handler._deliver_scheduled(scheduled_body())
            self.assertEqual(result["reason"], "scheduled_target_ineligible")
            api.create_and_send_event_into_room.assert_not_awaited()

    async def test_creation_follows_reservation_and_suppression_and_images_survive(
        self,
    ):
        handler, api = self.handler()
        body = scheduled_body()
        images = '<img src="https://content.pangea.chat/media/course.jpg" alt="Course"><img src="https://content.pangea.chat/media/activity.jpg" alt="Activity">'
        body["email"]["html"] += images
        steps = []

        async def reserve(req):
            steps.append("reserve")
            return "42", None

        async def suppress(*args):
            steps.append("suppress")
            return True

        async def create(event):
            steps.append("create")
            return SimpleNamespace(event_id="$deferred:test")

        handler._delivery_log.reserve.side_effect = reserve
        api.create_and_send_event_into_room.side_effect = create
        with patch(
            "synapse_pangea_chat.notice_delivery.deliver.ensure_bot_notice_push_rule",
            side_effect=suppress,
        ):
            result = await handler._deliver_scheduled(copy.deepcopy(body))
        self.assertEqual(steps, ["reserve", "suppress", "create"])
        self.assertEqual(result["notice_event_id"], "$deferred:test")
        sent = api._hs.get_send_email_handler.return_value.send_email.await_args.kwargs
        self.assertIn(images, sent["html"])
        self.assertIn("Open https://", sent["text"])
        self.assertEqual(
            handler._delivery_log.finish.await_args.args[1].notice_event_id,
            "$deferred:test",
        )

    async def test_availability_is_evaluated_at_delivery(self):
        handler, api = self.handler()
        body = scheduled_body()
        body["delivery_method"] = "use-available"
        api._hs.get_presence_handler.return_value.current_state_for_user.return_value = SimpleNamespace(
            state="online", currently_active=True
        )
        with patch(
            "synapse_pangea_chat.notice_delivery.deliver.ensure_bot_notice_push_rule",
            new=AsyncMock(return_value=True),
        ):
            result = await handler._deliver_scheduled(body)
        self.assertEqual(result["channel"], "in_app")
        api.create_and_send_event_into_room.assert_awaited_once()
        api._hs.get_send_email_handler.return_value.send_email.assert_not_awaited()

    async def test_log_outage_does_not_create_notice(self):
        handler, api = self.handler()
        handler._delivery_log.reserve.side_effect = RuntimeError("CMS down")
        with self.assertRaises(RuntimeError):
            await handler._deliver_scheduled(scheduled_body())
        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_revoked_requester_blocks_a_still_authorized_sender(self):
        handler, api = self.handler()
        body = scheduled_body()
        body["_requested_by"] = "@operator:test"
        api.is_user_admin.side_effect = lambda user: user != "@operator:test"
        result = await handler._deliver_scheduled(body)
        self.assertEqual(result["reason"], "scheduled_target_ineligible")
        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_uncertain_smtp_marks_log_for_reconciliation(self):
        handler, api = self.handler()
        api._hs.get_send_email_handler.return_value.send_email.side_effect = (
            RuntimeError("SMTP acknowledgement lost")
        )
        with patch(
            "synapse_pangea_chat.notice_delivery.deliver.ensure_bot_notice_push_rule",
            new=AsyncMock(return_value=True),
        ):
            result = await handler._deliver_scheduled(scheduled_body())
        self.assertEqual(result["log_status"], "pending_reconciliation")
        self.assertEqual(
            handler._delivery_log.finish.await_args.args[2]["log_status"],
            "pending_reconciliation",
        )
