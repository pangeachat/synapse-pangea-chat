"""Unit tests for the one-time pangea.course_plan l2 backfill.

No Synapse or Postgres: the batch scan, the CMS lookup, the lease and the
event send are all stubbed, so what is under test is the repair decision —
which rooms get written, what content they get, and which are left alone.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.public_courses.backfill_l2 import (
    BATCH_SIZE,
    CoursePlanRow,
    PublicCoursesL2Backfill,
    needs_repair,
    repaired_content,
)
from synapse_pangea_chat.public_courses.get_public_courses import extract_plan_id
from synapse_pangea_chat.public_courses.select_state_sender import (
    required_power_for_state_event,
)

EVENT_TYPE = "pangea.course_plan"
PLAN_ID = "plan-abc"


def _make_backfill(
    rows: List[CoursePlanRow],
    languages: Optional[Dict[str, str]] = None,
    sender: Optional[str] = "@teacher:my.domain.name",
) -> PublicCoursesL2Backfill:
    """A backfill whose DB, CMS and event-send are stubs.

    ``rows`` is served as a single batch; ``languages`` is what the CMS
    resolves; ``sender`` is who (if anyone) may send state into a room.
    """
    api = MagicMock()
    api._hs.hostname = "my.domain.name"
    api._hs.get_instance_name.return_value = "master"
    clock = MagicMock()
    clock.time_msec.return_value = 1_000_000
    clock.sleep = AsyncMock()
    api._hs.get_clock.return_value = clock
    api.create_and_send_event_into_room = AsyncMock()

    backfill = PublicCoursesL2Backfill(api, PangeaChatConfig())

    backfill._ensure_lease_table = AsyncMock()  # type: ignore[method-assign]
    backfill._claim_lease = AsyncMock(return_value=True)  # type: ignore[method-assign]
    backfill._heartbeat_lease = AsyncMock()  # type: ignore[method-assign]
    backfill._release_lease = AsyncMock()  # type: ignore[method-assign]

    batches = [rows] if len(rows) < BATCH_SIZE else [rows, []]

    async def _fetch_batch(after_room_id: Optional[str]) -> List[CoursePlanRow]:
        return batches.pop(0) if batches else []

    backfill._fetch_batch = _fetch_batch  # type: ignore[method-assign]

    backfill._resolve_languages = AsyncMock(  # type: ignore[method-assign]
        return_value=languages if languages is not None else {}
    )

    # Only sender *selection* is stubbed; the real _write_repair builds and
    # sends the event, so the content under assertion is the content the
    # module would really write.
    backfill._select_state_sender = AsyncMock(  # type: ignore[attr-defined]
        return_value=sender
    )

    return backfill


def _sent_contents(backfill: PublicCoursesL2Backfill) -> List[Dict[str, Any]]:
    api = backfill._api
    return [call.args[0] for call in api.create_and_send_event_into_room.call_args_list]


class TestNeedsRepair(unittest.TestCase):
    def test_repaired_room_is_left_alone(self):
        self.assertFalse(needs_repair({"uuid": PLAN_ID, "l2": "es"}))

    def test_missing_l2_needs_repair(self):
        self.assertTrue(needs_repair({"uuid": PLAN_ID}))

    def test_empty_l2_needs_repair(self):
        self.assertTrue(needs_repair({"uuid": PLAN_ID, "l2": ""}))

    def test_legacy_key_needs_repair_even_with_l2(self):
        self.assertTrue(needs_repair({"course_plan_id": PLAN_ID, "l2": "es"}))

    def test_legacy_key_alongside_uuid_needs_repair(self):
        self.assertTrue(
            needs_repair({"uuid": PLAN_ID, "course_plan_id": PLAN_ID, "l2": "es"})
        )


class TestRepairedContent(unittest.TestCase):
    def test_legacy_key_is_normalised_and_value_preserved(self):
        content = repaired_content({"course_plan_id": PLAN_ID}, PLAN_ID, "es")
        self.assertEqual(content, {"uuid": PLAN_ID, "l2": "es"})

    def test_other_keys_are_preserved(self):
        content = repaired_content(
            {"course_plan_id": PLAN_ID, "custom": {"a": 1}, "note": "keep me"},
            PLAN_ID,
            "de",
        )
        self.assertEqual(
            content,
            {"uuid": PLAN_ID, "l2": "de", "custom": {"a": 1}, "note": "keep me"},
        )

    def test_does_not_mutate_the_original(self):
        original = {"course_plan_id": PLAN_ID}
        repaired_content(original, PLAN_ID, "es")
        self.assertEqual(original, {"course_plan_id": PLAN_ID})


class TestSharedPlanIdRule(unittest.TestCase):
    """The backfill must not carry its own copy of the eligibility rule."""

    def test_uuid_wins_over_legacy_key(self):
        self.assertEqual(
            extract_plan_id({"uuid": "a", "course_plan_id": "b"}),
            "a",
        )

    def test_empty_uuid_falls_back_to_legacy_key(self):
        self.assertEqual(extract_plan_id({"uuid": "", "course_plan_id": "b"}), "b")

    def test_no_plan_id_is_none(self):
        self.assertIsNone(extract_plan_id({"l2": "es"}))


class TestRequiredPower(unittest.TestCase):
    def test_defaults_to_state_default(self):
        self.assertEqual(required_power_for_state_event(None, EVENT_TYPE), 50)

    def test_per_event_override_wins(self):
        self.assertEqual(
            required_power_for_state_event(
                {"state_default": 50, "events": {EVENT_TYPE: 100}}, EVENT_TYPE
            ),
            100,
        )


class TestBackfillRun(unittest.IsolatedAsyncioTestCase):
    async def test_adds_l2_from_cms(self):
        backfill = _make_backfill(
            [CoursePlanRow("!a:my.domain.name", "", {"uuid": PLAN_ID})],
            languages={PLAN_ID: "es"},
        )

        summary = await backfill.run()

        self.assertEqual(summary.repaired, 1)
        self.assertEqual(summary.scanned, 1)
        self.assertEqual(
            _sent_contents(backfill)[0]["content"],
            {"uuid": PLAN_ID, "l2": "es"},
        )

    async def test_already_repaired_room_is_untouched(self):
        backfill = _make_backfill(
            [CoursePlanRow("!a:my.domain.name", "", {"uuid": PLAN_ID, "l2": "es"})],
            languages={PLAN_ID: "fr"},
        )

        summary = await backfill.run()

        self.assertEqual(summary.already_ok, 1)
        self.assertEqual(summary.repaired, 0)
        backfill._api.create_and_send_event_into_room.assert_not_awaited()

    async def test_rerun_after_repair_writes_nothing(self):
        row = CoursePlanRow("!a:my.domain.name", "", {"uuid": PLAN_ID})
        first = _make_backfill([row], languages={PLAN_ID: "es"})
        await first.run()
        written = _sent_contents(first)[0]["content"]

        second = _make_backfill(
            [CoursePlanRow(row.room_id, "", written)],
            languages={PLAN_ID: "es"},
        )
        summary = await second.run()

        self.assertEqual(summary.repaired, 0)
        self.assertEqual(summary.already_ok, 1)
        second._api.create_and_send_event_into_room.assert_not_awaited()

    async def test_normalises_legacy_key_without_touching_existing_l2(self):
        backfill = _make_backfill(
            [
                CoursePlanRow(
                    "!a:my.domain.name", "", {"course_plan_id": PLAN_ID, "l2": "es-MX"}
                )
            ],
            languages={},
        )

        summary = await backfill.run()

        self.assertEqual(summary.repaired, 1)
        self.assertEqual(
            _sent_contents(backfill)[0]["content"],
            {"uuid": PLAN_ID, "l2": "es-MX"},
        )
        # A room that already knows its language never asks the CMS.
        backfill._resolve_languages.assert_awaited_once_with([])

    async def test_unresolvable_plan_is_skipped_not_guessed(self):
        backfill = _make_backfill(
            [CoursePlanRow("!a:my.domain.name", "", {"uuid": "unknown-plan"})],
            languages={},
        )

        summary = await backfill.run()

        self.assertEqual(summary.skipped_no_cms, 1)
        self.assertEqual(summary.repaired, 0)
        backfill._api.create_and_send_event_into_room.assert_not_awaited()

    async def test_cms_unreachable_skips_batch_without_writing(self):
        backfill = _make_backfill(
            [CoursePlanRow("!a:my.domain.name", "", {"uuid": PLAN_ID})],
        )
        backfill._resolve_languages = AsyncMock(  # type: ignore[method-assign]
            return_value=None
        )

        summary = await backfill.run()

        self.assertEqual(summary.skipped_no_cms, 1)
        self.assertEqual(summary.repaired, 0)
        backfill._api.create_and_send_event_into_room.assert_not_awaited()

    async def test_room_without_plan_id_is_not_a_course(self):
        backfill = _make_backfill(
            [CoursePlanRow("!a:my.domain.name", "", {"uuid": ""})],
            languages={},
        )

        summary = await backfill.run()

        self.assertEqual(summary.skipped_no_plan_id, 1)
        self.assertEqual(summary.repaired, 0)

    async def test_no_eligible_sender_is_skipped(self):
        backfill = _make_backfill(
            [CoursePlanRow("!a:my.domain.name", "", {"uuid": PLAN_ID})],
            languages={PLAN_ID: "es"},
            sender=None,
        )

        summary = await backfill.run()

        self.assertEqual(summary.skipped_no_sender, 1)
        self.assertEqual(summary.repaired, 0)

    async def test_lease_held_elsewhere_means_no_work(self):
        backfill = _make_backfill(
            [CoursePlanRow("!a:my.domain.name", "", {"uuid": PLAN_ID})],
            languages={PLAN_ID: "es"},
        )
        backfill._claim_lease = AsyncMock(return_value=False)  # type: ignore[method-assign]

        summary = await backfill.run()

        self.assertEqual(summary.scanned, 0)
        self.assertEqual(summary.repaired, 0)
        backfill._api.create_and_send_event_into_room.assert_not_awaited()

    async def test_lease_is_released_even_when_a_room_fails(self):
        backfill = _make_backfill(
            [CoursePlanRow("!a:my.domain.name", "", {"uuid": PLAN_ID})],
            languages={PLAN_ID: "es"},
        )

        async def _boom(*_args: Any, **_kwargs: Any) -> bool:
            raise RuntimeError("event rejected")

        backfill._write_repair = _boom  # type: ignore[method-assign]

        summary = await backfill.run()

        self.assertEqual(summary.failed, 1)
        backfill._release_lease.assert_awaited_once()

    async def test_state_key_is_preserved(self):
        backfill = _make_backfill(
            [CoursePlanRow("!a:my.domain.name", "alt", {"uuid": PLAN_ID})],
            languages={PLAN_ID: "es"},
        )

        await backfill.run()

        self.assertEqual(_sent_contents(backfill)[0]["state_key"], "alt")


if __name__ == "__main__":
    unittest.main()
