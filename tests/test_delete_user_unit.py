from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from synapse.api.errors import StoreError

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.delete_user.delete_user import (
    MAX_SCHEDULE_ATTEMPTS,
    DeleteUser,
)

LOGGER_NAME = "synapse.module.synapse_pangea_chat.delete_user"


def _make_handler() -> DeleteUser:
    api = MagicMock()
    api._hs.hostname = "my.domain.name"
    clock = MagicMock()
    clock.time_msec.return_value = 1_000_000
    api._hs.get_clock.return_value = clock
    handler = DeleteUser(api, PangeaChatConfig())
    handler._ensure_schedule_table = AsyncMock()  # type: ignore[method-assign]
    handler._upsert_schedule = AsyncMock()  # type: ignore[method-assign]
    handler._delete_user_now = AsyncMock()  # type: ignore[method-assign]
    return handler


def _schedule(
    user_id: str = "@ghost:my.domain.name",
    requested_by: str = "@requester:my.domain.name",
    attempts: int = 0,
) -> dict:
    return {
        "user_id": user_id,
        "requested_by": requested_by,
        "requested_by_admin": False,
        "attempts": attempts,
    }


class TestProcessScheduledDeletesRetries(unittest.IsolatedAsyncioTestCase):
    async def test_success_does_not_reschedule(self):
        handler = _make_handler()
        handler._claim_due_schedules = AsyncMock(return_value=[_schedule()])

        await handler._process_scheduled_deletes()

        handler._delete_user_now.assert_awaited_once()
        handler._upsert_schedule.assert_not_awaited()

    async def test_transient_failure_reschedules_with_incremented_attempts(self):
        handler = _make_handler()
        handler._claim_due_schedules = AsyncMock(return_value=[_schedule(attempts=1)])
        handler._delete_user_now = AsyncMock(side_effect=RuntimeError("boom"))

        await handler._process_scheduled_deletes()

        handler._upsert_schedule.assert_awaited_once()
        kwargs = handler._upsert_schedule.await_args.kwargs
        self.assertEqual(kwargs["user_id"], "@ghost:my.domain.name")
        self.assertEqual(kwargs["attempts"], 2)

    async def test_transient_failure_preserves_original_requester(self):
        handler = _make_handler()
        handler._claim_due_schedules = AsyncMock(return_value=[_schedule()])
        handler._delete_user_now = AsyncMock(side_effect=RuntimeError("boom"))

        await handler._process_scheduled_deletes()

        kwargs = handler._upsert_schedule.await_args.kwargs
        self.assertEqual(kwargs["requested_by"], "@requester:my.domain.name")

    async def test_transient_failure_stops_after_max_attempts(self):
        handler = _make_handler()
        handler._claim_due_schedules = AsyncMock(
            return_value=[_schedule(attempts=MAX_SCHEDULE_ATTEMPTS - 1)]
        )
        handler._delete_user_now = AsyncMock(side_effect=RuntimeError("boom"))

        with self.assertLogs(LOGGER_NAME, level="ERROR") as logs:
            await handler._process_scheduled_deletes()

        handler._upsert_schedule.assert_not_awaited()
        self.assertTrue(any("giving up" in line for line in logs.output))
        self.assertTrue(any("@ghost:my.domain.name" in line for line in logs.output))

    async def test_user_not_found_fails_terminally_on_first_attempt(self):
        handler = _make_handler()
        handler._claim_due_schedules = AsyncMock(return_value=[_schedule(attempts=0)])
        handler._delete_user_now = AsyncMock(
            side_effect=StoreError(404, "No row found (users)")
        )

        with self.assertLogs(LOGGER_NAME, level="ERROR") as logs:
            await handler._process_scheduled_deletes()

        handler._upsert_schedule.assert_not_awaited()
        self.assertTrue(any("giving up" in line for line in logs.output))

    async def test_missing_attempts_defaults_to_zero(self):
        handler = _make_handler()
        schedule = _schedule()
        del schedule["attempts"]
        handler._claim_due_schedules = AsyncMock(return_value=[schedule])
        handler._delete_user_now = AsyncMock(side_effect=RuntimeError("boom"))

        await handler._process_scheduled_deletes()

        kwargs = handler._upsert_schedule.await_args.kwargs
        self.assertEqual(kwargs["attempts"], 1)

    async def test_failure_on_one_schedule_does_not_block_others(self):
        handler = _make_handler()
        failing = _schedule(user_id="@ghost:my.domain.name")
        succeeding = _schedule(user_id="@alive:my.domain.name")
        handler._claim_due_schedules = AsyncMock(return_value=[failing, succeeding])
        handler._delete_user_now = AsyncMock(
            side_effect=[RuntimeError("boom"), {"ok": 1}]
        )

        await handler._process_scheduled_deletes()

        self.assertEqual(handler._delete_user_now.await_count, 2)


if __name__ == "__main__":
    unittest.main()
