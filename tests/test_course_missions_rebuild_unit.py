"""A claim whose Can-Do list read failed gets the list later, never over one
the teacher has published since (create-course-space.instructions.md)."""

import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.email_invite import course_missions_rebuild as module
from synapse_pangea_chat.email_invite.course_missions import (
    CMS_UNAVAILABLE,
    COURSE_MISSIONS_STATE_EVENT_TYPE,
    QUEST_NOT_FOUND,
    SELECTION_ALL_GONE,
    BuiltCourseMissions,
    CourseMissionsError,
    Problem,
)
from synapse_pangea_chat.email_invite.course_missions_rebuild import (
    REBUILD_DELAYS_SECONDS,
    CourseMissionsRebuild,
)

BUILT: Dict[str, Any] = {
    "version": 1,
    "revision": 1,
    "missions": [{"lo_id": "lo-a", "activity_ids": []}],
}
LIST_KEY = (COURSE_MISSIONS_STATE_EVENT_TYPE, "")


def _state(
    can_do_list: Optional[Dict[str, Any]], quest: str = "quest"
) -> Dict[Any, Any]:
    state: Dict[Any, Any] = {
        ("pangea.course_plan", ""): SimpleNamespace(content={"uuid": quest, "l2": "es"})
    }
    if can_do_list is not None:
        state[LIST_KEY] = SimpleNamespace(content=can_do_list)
    return state


class TestCourseMissionsRebuild(unittest.IsolatedAsyncioTestCase):
    def _rebuild(
        self, states: List[Any], read: Any
    ) -> tuple[CourseMissionsRebuild, MagicMock]:
        api = MagicMock()
        api.get_room_state = AsyncMock(side_effect=states)
        api.create_and_send_event_into_room = AsyncMock()
        reader = MagicMock()
        reader.read = AsyncMock(side_effect=[read])
        return CourseMissionsRebuild(api, reader), api

    def _scheduled_delays(self, api: MagicMock) -> List[float]:
        clock = api._hs.get_clock.return_value
        return [c.args[0].as_secs() for c in clock.call_later.call_args_list]

    async def test_a_course_still_without_a_list_gets_it_from_an_instructor(
        self,
    ) -> None:
        rebuild, api = self._rebuild(
            [_state(None), _state(None)], BuiltCourseMissions(BUILT)
        )

        with patch.object(module, "joined_local_instructors", return_value=["@t:x"]):
            await rebuild.attempt("!room:x", "quest", 0)

        api.create_and_send_event_into_room.assert_awaited_once_with(
            {
                "type": COURSE_MISSIONS_STATE_EVENT_TYPE,
                "state_key": "",
                "room_id": "!room:x",
                "sender": "@t:x",
                "content": BUILT,
            }
        )

    async def test_what_a_first_list_could_not_carry_over_is_reported(
        self,
    ) -> None:
        problems = [Problem(SELECTION_ALL_GONE, "lo-a", {"selected": 1, "kept": 0})]
        rebuild, _ = self._rebuild(
            [_state(None), _state(None)], BuiltCourseMissions(BUILT, problems)
        )

        with patch.object(
            module, "joined_local_instructors", return_value=["@t:x"]
        ), patch.object(module, "report_problems") as report:
            await rebuild.attempt("!room:x", "quest", 0)

        report.assert_called_once_with("quest", problems)

    async def test_a_replacement_does_not_report_its_claim_s_problems_again(
        self,
    ) -> None:
        claimed = {**BUILT, "revision": 1}
        problems = [Problem(SELECTION_ALL_GONE, "lo-a", {"selected": 1, "kept": 0})]
        rebuild, _ = self._rebuild(
            [_state(claimed), _state(claimed)], BuiltCourseMissions(BUILT, problems)
        )

        with patch.object(
            module, "joined_local_instructors", return_value=["@t:x"]
        ), patch.object(module, "report_problems") as report:
            await rebuild.attempt("!room:x", "quest", 0, claimed)

        report.assert_not_called()

    async def test_an_empty_list_left_by_an_old_failure_counts_as_none(self) -> None:
        rebuild, api = self._rebuild(
            [_state({}), _state({})], BuiltCourseMissions(BUILT)
        )

        with patch.object(module, "joined_local_instructors", return_value=["@t:x"]):
            await rebuild.attempt("!room:x", "quest", 0)

        api.create_and_send_event_into_room.assert_awaited_once()

    async def test_a_list_published_since_the_claim_is_left_alone(self) -> None:
        rebuild, api = self._rebuild([_state(BUILT)], BuiltCourseMissions(BUILT))

        await rebuild.attempt("!room:x", "quest", 0)

        rebuild._reader.read.assert_not_awaited()  # type: ignore[attr-defined]
        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_a_course_moved_to_another_quest_is_left_alone(self) -> None:
        # The app may leave a teacher who changed course with no list; the
        # old quest's Can-Dos must not land in the new course.
        rebuild, api = self._rebuild(
            [_state(None, quest="other")], BuiltCourseMissions(BUILT)
        )

        await rebuild.attempt("!room:x", "quest", 0)

        rebuild._reader.read.assert_not_awaited()  # type: ignore[attr-defined]
        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_a_course_moved_during_the_read_is_left_alone(self) -> None:
        rebuild, api = self._rebuild(
            [_state(None), _state(None, quest="other")], BuiltCourseMissions(BUILT)
        )

        with patch.object(module, "joined_local_instructors", return_value=["@t:x"]):
            await rebuild.attempt("!room:x", "quest", 0)

        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_a_list_published_during_the_read_is_left_alone(self) -> None:
        rebuild, api = self._rebuild(
            [_state(None), _state(BUILT)], BuiltCourseMissions(BUILT)
        )

        await rebuild.attempt("!room:x", "quest", 0)

        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_a_failure_that_may_pass_tries_again_later(self) -> None:
        rebuild, api = self._rebuild(
            [_state(None)], CourseMissionsError(CMS_UNAVAILABLE)
        )

        with patch.object(module, "capture_message") as capture:
            await rebuild.attempt("!room:x", "quest", 0)

        self.assertEqual(self._scheduled_delays(api), [REBUILD_DELAYS_SECONDS[1]])
        capture.assert_not_called()

    async def test_the_last_failure_is_captured_and_stops(self) -> None:
        last = len(REBUILD_DELAYS_SECONDS) - 1
        rebuild, api = self._rebuild(
            [_state(None)], CourseMissionsError(CMS_UNAVAILABLE)
        )

        with patch.object(module, "capture_message") as capture:
            await rebuild.attempt("!room:x", "quest", last)

        self.assertEqual(self._scheduled_delays(api), [])
        capture.assert_called_once()
        self.assertEqual(
            capture.call_args.args[0],
            "Can-Do list not built after the claim: cms_unavailable",
        )
        self.assertEqual(capture.call_args.kwargs["level"], "error")
        self.assertEqual(capture.call_args.kwargs["context"]["attempts"], last + 1)

    async def test_a_failure_that_cannot_pass_is_captured_at_once(self) -> None:
        rebuild, api = self._rebuild(
            [_state(None)], CourseMissionsError(QUEST_NOT_FOUND)
        )

        with patch.object(module, "capture_message") as capture:
            await rebuild.attempt("!room:x", "quest", 0)

        self.assertEqual(self._scheduled_delays(api), [])
        capture.assert_called_once()

    async def test_no_instructor_to_send_it_is_captured(self) -> None:
        rebuild, api = self._rebuild(
            [_state(None), _state(None)], BuiltCourseMissions(BUILT)
        )

        with patch.object(
            module, "joined_local_instructors", return_value=[]
        ), patch.object(module, "capture_message") as capture:
            await rebuild.attempt("!room:x", "quest", 0)

        api.create_and_send_event_into_room.assert_not_awaited()
        self.assertIn("no_sender", capture.call_args.args[0])

    async def test_a_synapse_failure_tries_again_later(self) -> None:
        rebuild, api = self._rebuild([RuntimeError("db")], BuiltCourseMissions(BUILT))

        with self.assertLogs(module.logger, "ERROR"):
            await rebuild.attempt("!room:x", "quest", 0)

        self.assertEqual(self._scheduled_delays(api), [REBUILD_DELAYS_SECONDS[1]])

    async def test_the_claim_s_partial_list_is_replaced_one_revision_up(
        self,
    ) -> None:
        claimed = {**BUILT, "revision": 1}
        # Synapse freezes state: lists come back as tuples.
        frozen = {**claimed, "missions": tuple(claimed["missions"])}
        rebuild, api = self._rebuild(
            [_state(frozen), _state(frozen)], BuiltCourseMissions(BUILT)
        )

        with patch.object(module, "joined_local_instructors", return_value=["@t:x"]):
            await rebuild.attempt("!room:x", "quest", 0, claimed)

        sent = api.create_and_send_event_into_room.await_args.args[0]
        self.assertEqual(sent["content"]["revision"], 2)
        rebuild._reader.read.assert_awaited_once_with(  # type: ignore[attr-defined]
            "quest", budget=module.REBUILD_BUDGET_SECONDS, report=False
        )

    async def test_a_list_the_teacher_changed_since_the_claim_is_left_alone(
        self,
    ) -> None:
        edited = {**BUILT, "revision": 2}
        rebuild, api = self._rebuild([_state(edited)], BuiltCourseMissions(BUILT))

        await rebuild.attempt("!room:x", "quest", 0, {**BUILT, "revision": 1})

        rebuild._reader.read.assert_not_awaited()  # type: ignore[attr-defined]
        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_still_unfinished_tries_again_with_the_same_expected_list(
        self,
    ) -> None:
        claimed = {**BUILT, "revision": 1}
        rebuild, api = self._rebuild(
            [_state(claimed)], BuiltCourseMissions(BUILT, [], ["lo-a"])
        )

        await rebuild.attempt("!room:x", "quest", 0, claimed)

        api.create_and_send_event_into_room.assert_not_awaited()
        call = api._hs.get_clock.return_value.call_later.call_args
        self.assertEqual(call.args[0].as_secs(), REBUILD_DELAYS_SECONDS[1])
        self.assertEqual(call.args[-1], claimed)

    def test_schedule_waits_the_first_delay(self) -> None:
        rebuild, api = self._rebuild([], None)

        rebuild.schedule("!room:x", "quest")

        self.assertEqual(self._scheduled_delays(api), [REBUILD_DELAYS_SECONDS[0]])


if __name__ == "__main__":
    unittest.main()
