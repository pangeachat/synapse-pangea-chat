import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from synapse_pangea_chat.notice_delivery.open_session_notice import (
    NOTICE_DELAY_MS,
    VARIANT,
    OpenSessionNotice,
)
from synapse_pangea_chat.notice_delivery.request import NoticeRequest
from tests.test_notice_schedule import TestScheduledDelivery

BOT = "@bot:test"
LEARNER = "@alice:test"
CREATED_MS = 1_790_000_000_000


def plan_event(*, replaces=False, state_key="", event_type="pangea.activity_plan"):
    return SimpleNamespace(
        type=event_type,
        room_id="!session:test",
        content={"activity_id": "activity-1"},
        unsigned={"replaces_state": "$old"} if replaces else {},
        get_state_key=lambda: state_key,
    )


class TestOpenSessionNotice(unittest.IsolatedAsyncioTestCase):
    def notice(self, *, creator=LEARNER, direct=None, dm_membership="join"):
        handler, api = TestScheduledDelivery().handler()
        handler.schedule.enqueue = AsyncMock(return_value={"status": "queued"})
        api.is_mine = MagicMock(return_value=True)
        api.get_room_state = AsyncMock(
            return_value={
                ("m.room.create", ""): SimpleNamespace(
                    sender=creator, origin_server_ts=CREATED_MS
                )
            }
        )
        api.account_data_manager.get_global = AsyncMock(
            return_value={BOT: ["!old-dm:test", "!dm:test"]}
            if direct is None
            else direct
        )
        store = api._hs.get_datastores.return_value.main
        store.get_local_current_membership_for_user_in_room = AsyncMock(
            return_value=(dm_membership, "$m")
        )
        return OpenSessionNotice(api, handler, BOT), handler

    async def test_a_new_session_schedules_one_push_at_half_an_hour(self):
        notice, handler = self.notice()
        await notice.on_new_event(plan_event(), {})
        handler.schedule.enqueue.assert_awaited_once()
        body = handler.schedule.enqueue.await_args.args[0]
        req = NoticeRequest.parse(body)
        self.assertEqual(
            (req.user_id, req.variant, req.method, req.notice_room_id),
            (LEARNER, VARIANT, "push-only", "!dm:test"),
        )
        self.assertEqual(req.log.run["runner"], "synapse")
        self.assertEqual(body["eligibility"], {"bot_fills_open_seat": True})
        self.assertEqual(
            req.push.content["pangea.activity.session_room_id"], "!session:test"
        )
        due = datetime.fromisoformat(body["scheduled_at"]).timestamp() * 1000
        self.assertEqual(due, CREATED_MS + NOTICE_DELAY_MS)

    async def test_the_dm_notice_is_a_check_in_the_bot_can_read(self):
        notice, handler = self.notice()
        await notice.on_new_event(plan_event(), {})
        content = handler.schedule.enqueue.await_args.args[0]["notice_content"]
        self.assertEqual(
            (content["notice_type"], content["check_in_type"]), ("check_in", VARIANT)
        )

    async def test_the_same_session_builds_the_same_request(self):
        # A second trigger must be a schedule duplicate, not a conflict.
        notice, handler = self.notice()
        await notice.on_new_event(plan_event(), {})
        await notice.on_new_event(plan_event(), {})
        first, second = [c.args[0] for c in handler.schedule.enqueue.await_args_list]
        self.assertEqual(first, second)

    async def test_other_events_and_rewrites_schedule_nothing(self):
        for event in (
            plan_event(replaces=True),
            plan_event(state_key="x"),
            plan_event(event_type="pangea.activity_roles"),
        ):
            notice, handler = self.notice()
            await notice.on_new_event(event, {})
            with self.subTest(event=event.type):
                handler.schedule.enqueue.assert_not_awaited()

    async def test_no_learner_or_no_bot_dm_schedules_nothing(self):
        for kwargs in (
            {"creator": BOT},
            {"direct": {}},
            {"dm_membership": "leave"},
        ):
            notice, handler = self.notice(**kwargs)
            await notice.on_new_event(plan_event(), {})
            with self.subTest(**kwargs):
                handler.schedule.enqueue.assert_not_awaited()

    async def test_a_failure_is_logged_not_raised(self):
        notice, handler = self.notice()
        handler.schedule.enqueue.side_effect = RuntimeError("db down")
        with self.assertLogs(
            "synapse.module.synapse_pangea_chat.notice_delivery.open_session_notice",
            level="ERROR",
        ):
            await notice.on_new_event(plan_event(), {})
