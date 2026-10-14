"""Unit tests for the set_course_plan decision and request parsing."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.api.errors import AuthError, InvalidClientTokenError

from synapse_pangea_chat.set_course_plan import set_course_plan as module
from synapse_pangea_chat.set_course_plan.set_course_plan import (
    MISMATCH,
    NOT_A_COURSE,
    UNCHANGED,
    UPDATE,
    PlanUpdate,
    SetCoursePlan,
    SetCoursePlanRequest,
    copy_revision,
    course_copy_content,
    parse_request,
    plan_update,
    planned_writes,
    topic_content,
)


class TestPlanUpdate(unittest.TestCase):
    def test_no_course_plan_is_not_a_course(self) -> None:
        contents: List[Optional[Dict[str, Any]]] = [
            None,
            {},
            {"uuid": ""},
            {"l2": "es"},
        ]
        for content in contents:
            with self.subTest(content=content):
                self.assertEqual(
                    plan_update(content, "old", "new").outcome, NOT_A_COURSE
                )

    def test_a_different_current_plan_is_refused(self) -> None:
        update = plan_update({"uuid": "teacher-pick", "l2": "es"}, "old", "new")

        self.assertEqual(update.outcome, MISMATCH)
        self.assertEqual(update.current_quest_id, "teacher-pick")
        self.assertIsNone(update.content)

    def test_the_expected_id_is_checked_before_already_there(self) -> None:
        # The doc orders the checks: a space that does not point where the
        # operator expected is refused, even if it already points at the target.
        update = plan_update({"uuid": "new"}, "old", "new")

        self.assertEqual(update.outcome, MISMATCH)

    def test_already_on_the_new_quest_is_unchanged(self) -> None:
        update = plan_update({"uuid": "new", "l2": "es"}, "new", "new")

        self.assertEqual(update.outcome, UNCHANGED)
        self.assertIsNone(update.content)

    def test_update_swaps_the_id_and_keeps_every_other_field(self) -> None:
        update = plan_update({"uuid": "old", "l2": "es", "extra": 1}, "old", "new")

        self.assertEqual(update.outcome, UPDATE)
        self.assertEqual(update.current_quest_id, "old")
        self.assertEqual(update.content, {"uuid": "new", "l2": "es", "extra": 1})

    def test_legacy_plan_id_is_read_and_then_dropped(self) -> None:
        update = plan_update({"course_plan_id": "old", "l2": "es"}, "old", "new")

        self.assertEqual(update.outcome, UPDATE)
        self.assertEqual(update.content, {"uuid": "new", "l2": "es"})


_COPY = {"version": 1, "revision": 2, "missions": [{"lo_id": "lo", "activity_ids": []}]}


class TestCourseCopy(unittest.TestCase):
    _BUILT = {"version": 1, "revision": 1, "missions": []}

    def test_the_first_copy_is_revision_one(self) -> None:
        self.assertEqual(course_copy_content(self._BUILT, None)["revision"], 1)

    def test_a_replacement_is_one_above_the_old_copy(self) -> None:
        old = {"version": 1, "revision": 3, "missions": []}

        self.assertEqual(course_copy_content(self._BUILT, old)["revision"], 4)

    def test_an_unreadable_old_revision_counts_as_none(self) -> None:
        olds: List[Dict[str, Any]] = [
            {},
            {"revision": "3"},
            {"revision": True},
            {"revision": 0},
        ]
        for old in olds:
            with self.subTest(old=old):
                self.assertIsNone(copy_revision(old))
                self.assertEqual(course_copy_content(self._BUILT, old)["revision"], 1)

    def test_a_copy_that_could_not_be_built_is_empty(self) -> None:
        old = {"version": 1, "revision": 3, "missions": []}

        self.assertEqual(course_copy_content(None, old), {})
        self.assertIsNone(copy_revision({}))


class TestPlannedWrites(unittest.TestCase):
    _MOVE = PlanUpdate(UPDATE, "old", {"uuid": "new", "l2": "es"})

    def test_a_move_writes_the_copy_then_plan_then_name_then_topic(self) -> None:
        writes = planned_writes(
            self._MOVE,
            "pangea.course_plan",
            _COPY,
            "Old title",
            "Old text",
            "New",
            "Text",
        )

        self.assertEqual(
            writes,
            [
                ("pangea.course_missions", _COPY),
                ("pangea.course_plan", {"uuid": "new", "l2": "es"}),
                ("m.room.name", {"name": "New"}),
                ("m.room.topic", topic_content("Text")),
            ],
        )

    def test_a_matching_name_or_topic_is_not_rewritten(self) -> None:
        writes = planned_writes(
            self._MOVE,
            "pangea.course_plan",
            _COPY,
            "Teacher's name",
            "",
            "Teacher's name",
            "",
        )

        self.assertEqual(
            [t for t, _ in writes], ["pangea.course_missions", "pangea.course_plan"]
        )

    def test_a_space_already_on_the_quest_keeps_its_copy(self) -> None:
        # A re-run after the plan landed must not wipe the teacher's later edits.
        writes = planned_writes(
            PlanUpdate(UNCHANGED, "new"),
            "pangea.course_plan",
            _COPY,
            "Old",
            None,
            "New",
            "",
        )

        self.assertEqual(writes, [("m.room.name", {"name": "New"})])

    def test_nothing_differs_nothing_written(self) -> None:
        writes = planned_writes(
            PlanUpdate(UNCHANGED, "new"),
            "pangea.course_plan",
            _COPY,
            "New",
            "Text",
            "New",
            "Text",
        )

        self.assertEqual(writes, [])

    def test_topic_matches_synapse_create_room_shape(self) -> None:
        self.assertEqual(
            topic_content("Text"),
            {"topic": "Text", "m.topic": {"m.text": [{"body": "Text"}]}},
        )


class TestParseRequest(unittest.TestCase):
    _VALID = {
        "room_id": "!space:pangea.chat",
        "quest_id": "new",
        "expected_quest_id": "old",
        "title": "New title",
        "description": "",
    }

    def test_valid_body(self) -> None:
        parsed, error = parse_request({**self._VALID, "dry_run": True})

        self.assertIsNone(error)
        self.assertEqual(
            parsed,
            SetCoursePlanRequest(
                "!space:pangea.chat", "new", "old", "New title", "", True
            ),
        )

    def test_dry_run_defaults_to_false(self) -> None:
        parsed, error = parse_request(self._VALID)

        self.assertIsNone(error)
        assert parsed is not None
        self.assertFalse(parsed.dry_run)

    def test_invalid_bodies_are_refused(self) -> None:
        bad = [
            "not an object",
            {**self._VALID, "room_id": "#alias:pangea.chat"},
            {**self._VALID, "room_id": 5},
            {**self._VALID, "quest_id": ""},
            {**self._VALID, "quest_id": "x" * 256},
            {k: v for k, v in self._VALID.items() if k != "expected_quest_id"},
            {**self._VALID, "dry_run": "yes"},
            {k: v for k, v in self._VALID.items() if k != "title"},
            {**self._VALID, "title": ""},
            {**self._VALID, "title": "x" * 256},
            {k: v for k, v in self._VALID.items() if k != "description"},
            {**self._VALID, "description": None},
            {**self._VALID, "description": "x" * 4097},
        ]
        for body in bad:
            with self.subTest(body=body):
                parsed, error = parse_request(body)
                self.assertIsNone(parsed)
                self.assertIsInstance(error, str)


class TestEndpoint(unittest.IsolatedAsyncioTestCase):
    """The handler's write order, sender and failure answers."""

    _BODY = {
        "room_id": "!space:test",
        "quest_id": "new",
        "expected_quest_id": "old",
        "title": "New title",
        "description": "New text",
    }
    _BUILT = {
        "version": 1,
        "revision": 1,
        "missions": [{"lo_id": "lo", "activity_ids": []}],
    }

    def _endpoint(
        self, built: Optional[Dict[str, Any]] = _BUILT
    ) -> tuple[SetCoursePlan, MagicMock]:
        api = MagicMock()
        api._hs.get_auth.return_value.get_user_by_req = AsyncMock(
            return_value=SimpleNamespace(
                user=SimpleNamespace(to_string=lambda: "@op:test")
            )
        )
        api.is_user_admin = AsyncMock(return_value=True)
        api.get_room_state = AsyncMock(
            return_value={
                ("pangea.course_plan", ""): SimpleNamespace(
                    content={"uuid": "old", "l2": "es"}
                ),
                ("pangea.course_missions", ""): SimpleNamespace(
                    content={"version": 1, "revision": 3, "missions": []}
                ),
                ("m.room.name", ""): SimpleNamespace(content={"name": "Old title"}),
            }
        )
        api.create_and_send_event_into_room = AsyncMock()
        config = SimpleNamespace(
            course_plan_state_event_type=None,
            cms_base_url="http://cms.test",
            cms_service_api_key="key",
        )
        endpoint = SetCoursePlan(api, config)  # type: ignore[arg-type]
        endpoint._course_missions.read = AsyncMock(return_value=built)  # type: ignore[method-assign]
        return endpoint, api

    async def _post(
        self, endpoint: SetCoursePlan, instructors: Optional[List[str]] = None
    ) -> tuple[int, Dict[str, Any]]:
        if instructors is None:
            instructors = ["@teacher:test"]
        with patch.object(
            module, "extract_body_json", AsyncMock(return_value=self._BODY)
        ), patch.object(
            module, "joined_local_instructors", return_value=instructors
        ), patch.object(
            module, "respond_with_json"
        ) as respond:
            await endpoint._async_render_POST(MagicMock())
        return respond.call_args.args[1], respond.call_args.args[2]

    @staticmethod
    def _sent(api: MagicMock) -> List[Dict[str, Any]]:
        return [c.args[0] for c in api.create_and_send_event_into_room.await_args_list]

    async def test_a_full_move_writes_four_events_as_one_instructor(self) -> None:
        endpoint, api = self._endpoint()

        status, body = await self._post(endpoint)

        self.assertEqual(status, 200, body)
        sent = self._sent(api)
        self.assertEqual(
            [e["type"] for e in sent],
            [
                "pangea.course_missions",
                "pangea.course_plan",
                "m.room.name",
                "m.room.topic",
            ],
        )
        self.assertEqual({e["sender"] for e in sent}, {"@teacher:test"})
        self.assertEqual(sent[0]["content"]["revision"], 4)
        self.assertEqual(
            body["course_copy"],
            {"built": True, "previous_revision": 3, "revision": 4, "missions": 1},
        )
        self.assertEqual(body["previous_name"], "Old title")
        self.assertIsNone(body["previous_topic"])

    async def test_a_copy_that_cannot_be_built_is_written_empty(self) -> None:
        endpoint, api = self._endpoint(built=None)

        with self.assertLogs(module.logger, level="WARNING"):
            status, body = await self._post(endpoint)

        self.assertEqual(status, 200, body)
        self.assertEqual(self._sent(api)[0]["content"], {})
        self.assertEqual(
            body["course_copy"],
            {"built": False, "previous_revision": 3, "revision": None, "missions": 0},
        )

    async def test_a_space_with_no_local_instructor_is_refused(self) -> None:
        endpoint, api = self._endpoint()

        with self.assertLogs(module.logger, level="WARNING"):
            status, body = await self._post(endpoint, instructors=[])

        self.assertEqual(status, 409, body)
        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_a_write_the_room_refuses_is_a_logged_conflict(self) -> None:
        endpoint, api = self._endpoint()
        api.create_and_send_event_into_room.side_effect = AuthError(
            403, "User @teacher:test not in room !space:test"
        )

        with self.assertLogs(module.logger, level="ERROR") as logs:
            status, body = await self._post(endpoint)

        self.assertEqual(status, 409)
        self.assertEqual(body["errcode"], "M_FORBIDDEN")
        self.assertEqual(body["written"], [])
        self.assertIn("@teacher:test", logs.output[0])

    async def test_a_refusal_after_the_copy_landed_says_what_landed(self) -> None:
        endpoint, api = self._endpoint()
        api.create_and_send_event_into_room.side_effect = [
            None,
            AuthError(403, "You don't have permission to post that to the room"),
        ]

        with self.assertLogs(module.logger, level="ERROR"):
            status, body = await self._post(endpoint)

        self.assertEqual(status, 409)
        self.assertEqual(body["written"], ["pangea.course_missions"])
        self.assertEqual(body["refused"], "pangea.course_plan")
        self.assertEqual(api.create_and_send_event_into_room.await_count, 2)

    async def test_a_bad_operator_token_is_still_unauthorized(self) -> None:
        endpoint, api = self._endpoint()
        api._hs.get_auth.return_value.get_user_by_req.side_effect = (
            InvalidClientTokenError()
        )

        status, body = await self._post(endpoint)

        self.assertEqual(status, 401)
        self.assertEqual(body["errcode"], "M_UNAUTHORIZED")
        api.get_room_state.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
