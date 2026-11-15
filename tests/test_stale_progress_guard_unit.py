"""Unit tests for the stale course progress guard
(stale-progress-guard.instructions.md)."""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional, cast
from unittest import mock

from synapse.util.frozenutils import freeze

from synapse_pangea_chat.stale_progress_guard.correct import (
    correct_record,
    retired_ids_in_course_state,
)
from synapse_pangea_chat.stale_progress_guard.guard import StaleProgressGuard
from synapse_pangea_chat.stale_progress_guard.retired_map import RetiredCanDoMap

MAP = {"x1": "M", "x2": "M", "z1": "Z"}
SEEN = "2026-09-10T12:00:00.000Z"
EARLIER = "2026-09-01T08:00:00.000Z"
LATER = "2026-10-01T08:00:00.000Z"
NOW = "2026-11-16T10:00:00.000Z"


def _entry(
    xp: int = 100,
    at: Optional[str] = None,
    threshold: Optional[int] = None,
    replayed: bool = False,
    completed: Optional[bool] = None,
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "xp": xp,
        "chat": xp,
        "speaking": 0,
        "goals": 0,
        "practice": 0,
        "completed": (at is not None or replayed) if completed is None else completed,
        "first_completed_at": at,
    }
    if threshold is not None:
        entry["xp_to_complete_at_first_completion"] = threshold
    if replayed:
        entry["completed_before_tracking"] = True
    return entry


def _record(can_dos: Dict[str, Any], computed_at: str = NOW) -> Dict[str, Any]:
    return {
        "computed_at": computed_at,
        "app_version": "5.1.0",
        "xp_to_complete": 500,
        "can_dos": can_dos,
    }


class TestCorrectRecord(unittest.TestCase):
    def test_a_write_naming_no_retired_id_passes_untouched(self) -> None:
        stored = _record({"M": _entry(700, SEEN, 500)})
        incoming = _record({"M": _entry(800, SEEN, 500), "Q": _entry(5)})

        self.assertIsNone(correct_record(incoming, stored, MAP).content)
        # Even one that drops a Can-Do the stored record has: only a stale
        # write is corrected, so a course's removed Can-Do can still go.
        self.assertIsNone(
            correct_record(_record({"Q": _entry(5)}), stored, MAP).content
        )

    def test_a_stale_write_keeps_the_stored_merged_entry_and_its_date(self) -> None:
        merged = _entry(700, SEEN, 500)
        stored = _record({"M": merged, "Q": _entry(40)}, computed_at=EARLIER)
        # The stale app rebuilt the record from the old outline: old ids, and
        # no dates, because none carried over under those ids.
        incoming = _record({"x1": _entry(300), "x2": _entry(200), "Q": _entry(50)})

        result = correct_record(incoming, stored, MAP)

        assert result.content is not None
        self.assertEqual(result.content["can_dos"], {"M": merged, "Q": _entry(50)})
        # When and by which version the figures were computed are the write's.
        self.assertEqual(result.content["computed_at"], NOW)
        self.assertEqual(result.report["retired_keys"], 2)

    def test_a_can_do_the_stale_write_left_out_is_kept(self) -> None:
        stored = _record({"M": _entry(700, SEEN, 500), "Z": _entry(900, LATER, 500)})
        incoming = _record({"x1": _entry(300)})

        result = correct_record(incoming, stored, MAP)

        assert result.content is not None
        self.assertEqual(
            result.content["can_dos"],
            {"M": _entry(700, SEEN, 500), "Z": _entry(900, LATER, 500)},
        )
        self.assertEqual(result.report["kept_from_stored"], 1)

    def test_a_completion_the_stale_app_dated_completes_the_merged_can_do(
        self,
    ) -> None:
        stored = _record({"M": _entry(400)})
        incoming = _record({"x1": _entry(520, LATER, 500)})

        result = correct_record(incoming, stored, MAP)

        assert result.content is not None
        entry = result.content["can_dos"]["M"]
        self.assertEqual(entry["xp"], 400)  # the stored figures stay
        self.assertTrue(entry["completed"])
        self.assertEqual(entry["first_completed_at"], LATER)
        self.assertEqual(entry["xp_to_complete_at_first_completion"], 500)
        self.assertEqual(result.report["completions_kept"], 1)

    def test_the_earliest_completion_wins(self) -> None:
        stored = _record({"M": _entry(700, LATER, 800)})
        incoming = _record({"x2": _entry(500, EARLIER, 400, replayed=True)})

        result = correct_record(incoming, stored, MAP)

        assert result.content is not None
        entry = result.content["can_dos"]["M"]
        self.assertEqual(entry["first_completed_at"], EARLIER)
        self.assertEqual(entry["xp_to_complete_at_first_completion"], 400)
        self.assertTrue(entry["completed_before_tracking"])
        self.assertEqual(entry["xp"], 700)

    def test_a_later_stale_date_never_replaces_the_stored_one(self) -> None:
        stored = _record({"M": _entry(700, EARLIER, 500)})
        incoming = _record({"x1": _entry(800, LATER, 600)})

        result = correct_record(incoming, stored, MAP)

        assert result.content is not None
        self.assertEqual(result.content["can_dos"]["M"], _entry(700, EARLIER, 500))
        self.assertEqual(result.report["completions_kept"], 0)

    def test_completed_without_a_date_keeps_the_replay_mark(self) -> None:
        stored = _record({"M": _entry(100)})
        incoming = _record({"x1": _entry(600, replayed=True)})

        result = correct_record(incoming, stored, MAP)

        assert result.content is not None
        entry = result.content["can_dos"]["M"]
        self.assertTrue(entry["completed"])
        self.assertIsNone(entry["first_completed_at"])
        self.assertTrue(entry["completed_before_tracking"])

    def test_a_merged_can_do_the_store_lacks_takes_the_old_entry_with_most_xp(
        self,
    ) -> None:
        stored = _record({"Q": _entry(1)})
        incoming = _record({"x1": _entry(300), "x2": _entry(450, SEEN, 500)})

        result = correct_record(incoming, stored, MAP)

        assert result.content is not None
        self.assertEqual(
            result.content["can_dos"], {"M": _entry(450, SEEN, 500), "Q": _entry(1)}
        )

    def test_an_unmigrated_stored_record_is_moved_too(self) -> None:
        stored = _record({"x1": _entry(600, EARLIER, 500), "z1": _entry(30)})
        incoming = _record({"x2": _entry(200)})

        result = correct_record(incoming, stored, MAP)

        assert result.content is not None
        self.assertEqual(set(result.content["can_dos"]), {"M", "Z"})
        self.assertEqual(result.content["can_dos"]["M"]["first_completed_at"], EARLIER)
        self.assertEqual(result.content["can_dos"]["M"]["xp"], 600)

    def test_a_merged_entry_the_write_carries_gives_the_figures(self) -> None:
        stored = _record({"M": _entry(300)})
        incoming = _record({"M": _entry(650), "x1": _entry(100, EARLIER, 500)})

        result = correct_record(incoming, stored, MAP)

        assert result.content is not None
        entry = result.content["can_dos"]["M"]
        self.assertEqual(entry["xp"], 650)
        self.assertEqual(entry["first_completed_at"], EARLIER)

    def test_no_stored_record_and_frozen_content(self) -> None:
        incoming = freeze(_record({"x1": _entry(300)}))

        result = correct_record(incoming, None, MAP)

        assert result.content is not None
        self.assertEqual(result.content["can_dos"], {"M": _entry(300)})

    def test_every_other_field_of_the_write_stays(self) -> None:
        incoming = {**_record({"x1": _entry(1)}), "later_field": {"a": 1}}

        result = correct_record(incoming, _record({}), MAP)

        assert result.content is not None
        self.assertEqual(result.content["later_field"], {"a": 1})
        self.assertEqual(result.content["xp_to_complete"], 500)
        self.assertEqual(result.content["app_version"], "5.1.0")

    def test_an_unreadable_write_or_entry(self) -> None:
        self.assertIsNone(correct_record({"can_dos": []}, None, MAP).content)
        result = correct_record(
            _record({"x1": "nonsense"}), _record({"M": _entry(2)}), MAP
        )
        assert result.content is not None
        self.assertEqual(result.content["can_dos"], {"M": _entry(2)})


class TestRetiredIdsInCourseState(unittest.TestCase):
    def test_course_list(self) -> None:
        content = {
            "missions": [{"lo_id": "x1"}, {"lo_id": "M"}, "junk"],
            "removed_missions": [{"lo_id": "z1"}],
        }
        self.assertEqual(
            retired_ids_in_course_state("pangea.course_missions", content, MAP), 2
        )

    def test_pins(self) -> None:
        content = {"pinned_activities_by_objective": {"x2": ["a"], "M": ["b"]}}
        self.assertEqual(
            retired_ids_in_course_state("pangea.teacher_mode", content, MAP), 1
        )
        self.assertEqual(retired_ids_in_course_state("pangea.teacher_mode", {}, MAP), 0)


class _Event:
    def __init__(
        self,
        type: str,
        content: Dict[str, Any],
        sender: str = "@ana:test",
        state_key: Optional[str] = "!course:test",
    ):
        self.type = type
        self.content = freeze(content)
        self.sender = sender
        self.state_key = state_key
        self.room_id = "!analytics:test"
        self.event_id = "$e"

    def is_state(self) -> bool:
        return self.state_key is not None

    def get_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "room_id": self.room_id,
            "sender": self.sender,
            "state_key": self.state_key,
            "content": self.content,
            "prev_events": ["$p"],
        }


class _Retired:
    def __init__(self, value: Dict[str, str]):
        self.value = value
        self.calls = 0

    async def current(self) -> Dict[str, str]:
        self.calls += 1
        return self.value


async def _allowed(
    guard: StaleProgressGuard, event: "_Event", state: Dict[Any, Any]
) -> Any:
    return await guard.check_event_allowed(cast(Any, event), state)


def _guard(retired: Any) -> StaleProgressGuard:
    api = mock.Mock()
    api.is_mine.side_effect = lambda user_id: user_id.endswith(":test")
    return StaleProgressGuard(api, retired)


class TestGuard(unittest.IsolatedAsyncioTestCase):
    async def test_other_events_return_without_reading_the_map(self) -> None:
        retired = _Retired(MAP)
        guard = _guard(retired)

        allowed = await _allowed(
            guard, _Event("m.room.message", {"body": "hi"}, state_key=None), {}
        )

        self.assertEqual(allowed, (True, None))
        self.assertEqual(retired.calls, 0)

    async def test_no_map_is_a_no_op(self) -> None:
        guard = _guard(_Retired({}))
        event = _Event("pangea.course_progress", _record({"x1": _entry(1)}))

        self.assertEqual(await _allowed(guard, event, {}), (True, None))

    async def test_a_stale_write_is_replaced_by_the_corrected_record(self) -> None:
        guard = _guard(_Retired(MAP))
        stored = _Event("pangea.course_progress", _record({"M": _entry(700, SEEN)}))
        event = _Event("pangea.course_progress", _record({"x1": _entry(300)}))

        allowed, replacement = await _allowed(
            guard, event, {("pangea.course_progress", "!course:test"): stored}
        )

        self.assertTrue(allowed)
        assert replacement is not None
        self.assertEqual(replacement["content"]["can_dos"], {"M": _entry(700, SEEN)})
        for key in ("type", "room_id", "sender", "state_key", "prev_events"):
            self.assertEqual(replacement[key], event.get_dict()[key])

    async def test_a_remote_sender_is_left_alone(self) -> None:
        guard = _guard(_Retired(MAP))
        event = _Event(
            "pangea.course_progress",
            _record({"x1": _entry(1)}),
            sender="@ana:elsewhere",
        )

        self.assertEqual(await _allowed(guard, event, {}), (True, None))

    async def test_a_failure_lets_the_write_through_and_is_captured(self) -> None:
        class Broken:
            async def current(self) -> Dict[str, str]:
                raise RuntimeError("database gone")

        guard = _guard(Broken())
        event = _Event("pangea.course_progress", _record({"x1": _entry(1)}))
        with mock.patch(
            "synapse_pangea_chat.stale_progress_guard.guard.capture_exception"
        ) as capture:
            allowed = await _allowed(guard, event, {})

        self.assertEqual(allowed, (True, None))
        capture.assert_called_once()

    async def test_a_course_list_with_a_retired_id_is_reported_not_blocked(
        self,
    ) -> None:
        guard = _guard(_Retired(MAP))
        event = _Event(
            "pangea.course_missions", {"missions": [{"lo_id": "x1"}]}, state_key=""
        )
        with mock.patch(
            "synapse_pangea_chat.stale_progress_guard.guard.capture_exception"
        ) as capture:
            allowed = await _allowed(guard, event, {})

        self.assertEqual(allowed, (True, None))
        capture.assert_called_once()
        message = str(capture.call_args.args[0])
        self.assertIn("pangea.course_missions", message)
        self.assertIn("!analytics:test", message)
        self.assertIn("1", message)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000

    def time_msec(self) -> int:
        return self.now


class TestRetiredCanDoMap(unittest.IsolatedAsyncioTestCase):
    def _map(
        self, rows: List[Dict[str, str]]
    ) -> "tuple[RetiredCanDoMap, _Clock, mock.AsyncMock]":
        clock = _Clock()
        load = mock.AsyncMock(side_effect=lambda: dict(rows[-1]))
        retired = RetiredCanDoMap(mock.Mock(), clock, ttl_ms=60_000)
        retired._load = load  # type: ignore[method-assign]
        return retired, clock, load

    async def test_read_once_a_minute(self) -> None:
        rows = [{"x1": "M"}]
        retired, clock, load = self._map(rows)

        self.assertEqual(await retired.current(), {"x1": "M"})
        rows.append({"x2": "M"})
        clock.now += 59_000
        self.assertEqual(await retired.current(), {"x1": "M"})
        clock.now += 2_000
        self.assertEqual(await retired.current(), {"x2": "M"})
        self.assertEqual(load.await_count, 2)

    async def test_a_change_on_this_process_is_read_at_once(self) -> None:
        rows = [{"x1": "M"}]
        retired, _, load = self._map(rows)
        await retired.current()
        retired.invalidate()
        rows.append({})

        self.assertEqual(await retired.current(), {})
        self.assertEqual(load.await_count, 2)


if __name__ == "__main__":
    unittest.main()
