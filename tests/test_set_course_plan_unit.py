"""Unit tests for the set_course_plan decision and request parsing."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.api.errors import AuthError, InvalidClientTokenError

from synapse_pangea_chat.email_invite.course_missions import (
    CMS_UNAVAILABLE,
    QUEST_NOT_FOUND,
    SELECTION_ALL_GONE,
    BuiltCourseMissions,
    CourseMissionsError,
    Problem,
)
from synapse_pangea_chat.set_course_plan import set_course_plan as module
from synapse_pangea_chat.set_course_plan.merge_can_do_list import MergedCanDoList
from synapse_pangea_chat.set_course_plan.set_course_plan import (
    MISMATCH,
    NOT_A_COURSE,
    UNCHANGED,
    UPDATE,
    PlanUpdate,
    SetCoursePlan,
    SetCoursePlanRequest,
    can_do_list_content,
    can_do_list_revision,
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


_CAN_DO_LIST = {
    "version": 1,
    "revision": 2,
    "missions": [{"lo_id": "lo", "activity_ids": []}],
}


class TestCanDoList(unittest.TestCase):
    _BUILT = {"version": 1, "revision": 1, "missions": []}

    def test_a_readable_revision(self) -> None:
        self.assertEqual(can_do_list_revision({"version": 1, "revision": 3}), 3)

    def test_the_first_can_do_list_is_revision_one(self) -> None:
        self.assertEqual(can_do_list_content(self._BUILT, None)["revision"], 1)

    def test_a_replacement_is_one_above_the_old_can_do_list(self) -> None:
        old = {"version": 1, "revision": 3, "missions": []}

        self.assertEqual(can_do_list_content(self._BUILT, old)["revision"], 4)

    def test_an_unreadable_old_revision_counts_as_none(self) -> None:
        olds: List[Optional[Dict[str, Any]]] = [
            None,
            {},
            {"revision": "3"},
            {"revision": True},
            {"revision": 0},
        ]
        for old in olds:
            with self.subTest(old=old):
                self.assertIsNone(can_do_list_revision(old))
                self.assertEqual(can_do_list_content(self._BUILT, old)["revision"], 1)


class TestPlannedWrites(unittest.TestCase):
    _MOVE = PlanUpdate(UPDATE, "old", {"uuid": "new", "l2": "es"})

    def test_a_move_writes_the_can_do_list_then_plan_then_name_then_topic(self) -> None:
        writes = planned_writes(
            self._MOVE,
            "pangea.course_plan",
            _CAN_DO_LIST,
            "Old title",
            "Old text",
            "New",
            "Text",
        )

        self.assertEqual(
            writes,
            [
                ("pangea.course_missions", _CAN_DO_LIST),
                ("pangea.course_plan", {"uuid": "new", "l2": "es"}),
                ("m.room.name", {"name": "New"}),
                ("m.room.topic", topic_content("Text")),
            ],
        )

    def test_a_move_without_a_can_do_list_is_a_bug(self) -> None:
        with self.assertRaises(ValueError):
            planned_writes(self._MOVE, "pangea.course_plan", None, "", "", "New", "")

    def test_a_matching_name_or_topic_is_not_rewritten(self) -> None:
        writes = planned_writes(
            self._MOVE,
            "pangea.course_plan",
            _CAN_DO_LIST,
            "Teacher's name",
            "",
            "Teacher's name",
            "",
        )

        self.assertEqual(
            [t for t, _ in writes], ["pangea.course_missions", "pangea.course_plan"]
        )

    def test_a_space_already_on_the_quest_keeps_its_can_do_list(self) -> None:
        # A re-run after the plan landed must not wipe edits the teacher has
        # published since.
        writes = planned_writes(
            PlanUpdate(UNCHANGED, "new"),
            "pangea.course_plan",
            None,
            "Old",
            None,
            "New",
            "",
        )

        self.assertEqual(writes, [("m.room.name", {"name": "New"})])

    def test_an_empty_can_do_list_on_the_quest_is_rebuilt_alone(self) -> None:
        writes = planned_writes(
            PlanUpdate(UNCHANGED, "new"),
            "pangea.course_plan",
            _CAN_DO_LIST,
            "New",
            "Text",
            "New",
            "Text",
        )

        self.assertEqual(writes, [("pangea.course_missions", _CAN_DO_LIST)])

    def test_nothing_differs_nothing_written(self) -> None:
        writes = planned_writes(
            PlanUpdate(UNCHANGED, "new"),
            "pangea.course_plan",
            None,
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


class _EndpointHarness(unittest.IsolatedAsyncioTestCase):
    """A handler over a fake ModuleApi."""

    _BODY = {
        "room_id": "!space:test",
        "quest_id": "new",
        "expected_quest_id": "old",
        "title": "New title",
        "description": "New text",
    }
    _OLD_CAN_DO_LIST = {"version": 1, "revision": 3, "missions": []}
    _BUILT = {
        "version": 1,
        "revision": 1,
        "missions": [{"lo_id": "lo", "activity_ids": []}],
    }

    def _endpoint(
        self,
        can_do_list: Optional[Dict[str, Any]] = _OLD_CAN_DO_LIST,
        built: Optional[Dict[str, Any]] = _BUILT,
        on: str = "old",
        copy_event_id: str = "$copy",
    ) -> tuple[SetCoursePlan, MagicMock]:
        api = MagicMock()
        api._hs.get_auth.return_value.get_user_by_req = AsyncMock(
            return_value=SimpleNamespace(
                user=SimpleNamespace(to_string=lambda: "@op:test")
            )
        )
        api.is_user_admin = AsyncMock(return_value=True)
        state: Dict[Any, Any] = {
            ("pangea.course_plan", ""): SimpleNamespace(
                content={"uuid": on, "l2": "es"}
            ),
            ("m.room.name", ""): SimpleNamespace(content={"name": "Old title"}),
        }
        if can_do_list is not None:
            state[("pangea.course_missions", "")] = SimpleNamespace(
                content=can_do_list, event_id=copy_event_id
            )
        api.get_room_state = AsyncMock(return_value=state)
        api.create_and_send_event_into_room = AsyncMock()
        config = SimpleNamespace(
            course_plan_state_event_type=None,
            cms_base_url="http://cms.test",
            cms_service_api_key="key",
        )
        endpoint = SetCoursePlan(api, config)  # type: ignore[arg-type]
        endpoint._course_missions.read = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                CourseMissionsError(CMS_UNAVAILABLE, "CMS answered 503")
                if built is None
                else BuiltCourseMissions(built)
            ]
        )
        return endpoint, api

    async def _post(
        self,
        endpoint: SetCoursePlan,
        instructors: Optional[List[str]] = None,
        expected: str = "old",
        **extra: Any,
    ) -> tuple[int, Dict[str, Any]]:
        if instructors is None:
            instructors = ["@teacher:test"]
        body = {**self._BODY, "expected_quest_id": expected, **extra}
        with patch.object(
            module, "extract_body_json", AsyncMock(return_value=body)
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

    async def _refused(
        self,
        endpoint: SetCoursePlan,
        api: MagicMock,
        expected: str = "old",
        **extra: Any,
    ) -> Dict[str, Any]:
        with self.assertLogs(module.logger, level="ERROR"), patch(
            "synapse_pangea_chat.email_invite.course_missions.capture_message"
        ) as capture:
            status, body = await self._post(endpoint, expected=expected, **extra)

        api.create_and_send_event_into_room.assert_not_awaited()
        capture.assert_called_once()
        self.assertEqual(capture.call_args.kwargs["context"]["room_id"], "!space:test")
        return {"status": status, **body}


class TestEndpoint(_EndpointHarness):
    """The handler's write order, sender and failure answers."""

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
            body["can_do_list"],
            {
                "built": True,
                "previous_revision": 3,
                "revision": 4,
                "missions": 1,
                "problems": {},
            },
        )
        self.assertEqual(body["previous_name"], "Old title")
        self.assertIsNone(body["previous_topic"])

    async def test_a_space_with_no_can_do_list_gets_revision_one(self) -> None:
        endpoint, api = self._endpoint(can_do_list=None)

        status, body = await self._post(endpoint)

        self.assertEqual(status, 200, body)
        self.assertEqual(self._sent(api)[0]["content"]["revision"], 1)
        self.assertIsNone(body["can_do_list"]["previous_revision"])

    async def test_a_can_do_list_that_cannot_be_built_refuses_the_move(self) -> None:
        # Never an empty list in place of the course's own: the operator
        # gets the code and nothing is written, the plan included.
        endpoint, api = self._endpoint(built=None)

        answer = await self._refused(endpoint, api)

        self.assertEqual(answer["status"], 502)
        self.assertEqual(answer["code"], CMS_UNAVAILABLE)

    async def test_a_quest_that_is_gone_is_refused_as_the_request_s_fault(
        self,
    ) -> None:
        endpoint, api = self._endpoint()
        endpoint._course_missions.read = AsyncMock(  # type: ignore[method-assign]
            side_effect=CourseMissionsError(QUEST_NOT_FOUND)
        )

        answer = await self._refused(endpoint, api)

        self.assertEqual(answer["status"], 422)
        self.assertEqual(answer["code"], QUEST_NOT_FOUND)

    async def test_a_list_partly_ranked_in_time_is_refused(self) -> None:
        # Written, it would stay: a list with content is never rebuilt here.
        endpoint, api = self._endpoint()
        endpoint._course_missions.read = AsyncMock(  # type: ignore[method-assign]
            return_value=BuiltCourseMissions(self._BUILT, [], ["lo"])
        )

        with self.assertLogs(module.logger, level="ERROR"):
            status, body = await self._post(endpoint)

        self.assertEqual(status, 502)
        self.assertEqual(body["code"], "ranked_unfinished")
        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_what_the_list_could_not_carry_over_is_counted(self) -> None:
        endpoint, api = self._endpoint()
        endpoint._course_missions.read = AsyncMock(  # type: ignore[method-assign]
            return_value=BuiltCourseMissions(
                self._BUILT,
                [Problem(SELECTION_ALL_GONE, "lo", {"selected": 1, "kept": 0})],
            )
        )

        status, body = await self._post(endpoint)

        self.assertEqual(status, 200, body)
        self.assertEqual(body["can_do_list"]["problems"], {SELECTION_ALL_GONE: 1})

    async def test_an_empty_can_do_list_on_the_new_quest_is_rebuilt(self) -> None:
        endpoint, api = self._endpoint(can_do_list={}, on="new")

        status, body = await self._post(endpoint, expected="new")

        self.assertEqual(status, 200, body)
        sent = self._sent(api)
        self.assertEqual(
            [e["type"] for e in sent],
            ["pangea.course_missions", "m.room.name", "m.room.topic"],
        )
        self.assertEqual(sent[0]["content"]["revision"], 1)
        self.assertTrue(body["can_do_list"]["built"])

    async def test_a_missing_can_do_list_on_the_quest_is_written(self) -> None:
        # A space made before courses got their own Can-Do list (admin-dash-api#472).
        endpoint, api = self._endpoint(can_do_list=None, on="new")

        status, body = await self._post(endpoint, expected="new")

        self.assertEqual(status, 200, body)
        sent = self._sent(api)
        self.assertEqual(sent[0]["type"], "pangea.course_missions")
        self.assertEqual(sent[0]["content"]["revision"], 1)
        self.assertNotIn("pangea.course_plan", [e["type"] for e in sent])
        self.assertTrue(body["can_do_list"]["built"])

    async def test_a_failed_read_for_a_missing_can_do_list_is_refused(
        self,
    ) -> None:
        endpoint, api = self._endpoint(can_do_list=None, built=None, on="new")

        answer = await self._refused(endpoint, api, expected="new")

        self.assertEqual(answer["status"], 502)

    async def test_a_failed_rebuild_of_an_empty_can_do_list_is_refused(self) -> None:
        endpoint, api = self._endpoint(can_do_list={}, built=None, on="new")

        answer = await self._refused(endpoint, api, expected="new")

        self.assertEqual(answer["code"], CMS_UNAVAILABLE)

    async def test_a_can_do_list_with_content_on_the_new_quest_is_left_alone(
        self,
    ) -> None:
        endpoint, api = self._endpoint(on="new")

        status, body = await self._post(endpoint, expected="new")

        self.assertEqual(status, 200, body)
        self.assertNotIn("pangea.course_missions", [e["type"] for e in self._sent(api)])
        self.assertIsNone(body["can_do_list"])
        endpoint._course_missions.read.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_a_space_that_changed_during_the_read_is_refused(self) -> None:
        endpoint, api = self._endpoint()
        first = await api.get_room_state()
        moved = dict(first)
        moved[("pangea.course_plan", "")] = SimpleNamespace(
            content={"uuid": "teacher-pick", "l2": "es"}
        )
        api.get_room_state = AsyncMock(side_effect=[first, moved])

        with self.assertLogs(module.logger, level="WARNING"):
            status, body = await self._post(endpoint)

        self.assertEqual(status, 409, body)
        api.create_and_send_event_into_room.assert_not_awaited()

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

    async def test_a_refusal_after_the_can_do_list_landed_says_what_landed(
        self,
    ) -> None:
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


class _ReplacingHarness(_EndpointHarness):
    """A course on its quest whose named list the builder made wrongly."""

    _BUILT_LIST = {
        "version": 1,
        "revision": 1,
        "missions": [
            {"lo_id": "lo-a", "activity_ids": ["s1", "s2", "s3"]},
            {"lo_id": "lo-b", "activity_ids": ["b1"]},
        ],
    }
    _WRONG_LIST = {
        "version": 1,
        "revision": 1,
        "missions": [
            {"lo_id": "lo-a", "activity_ids": ["s1", "x1", "x2"]},
            {"lo_id": "lo-b", "activity_ids": ["b1", "b2"]},
        ],
    }

    def _replacing(
        self, copy_event_id: str = "$built"
    ) -> tuple[SetCoursePlan, MagicMock]:
        endpoint, api = self._endpoint(
            can_do_list=self._WRONG_LIST,
            built=self._BUILT_LIST,
            on="new",
            copy_event_id=copy_event_id,
        )
        endpoint._course_missions.quest_selections = AsyncMock(  # type: ignore[method-assign]
            return_value={"lo-a": ["s1", "s2", "s3"], "lo-b": []}
        )
        return endpoint, api


class TestReplaceCanDoList(_ReplacingHarness):
    """Rebuilding a course's list the server built wrongly (pangeachat/.github
    #832): only while the space still holds the exact list event named, so a
    list a teacher has published since is never overwritten."""

    async def test_the_named_list_is_replaced_one_revision_up(self) -> None:
        endpoint, api = self._replacing()

        status, body = await self._post(
            endpoint, expected="new", replace_copy_event_id="$built"
        )

        self.assertEqual(status, 200, body)
        sent = self._sent(api)
        self.assertEqual(sent[0]["type"], "pangea.course_missions")
        self.assertEqual(sent[0]["content"], {**self._BUILT_LIST, "revision": 2})
        self.assertEqual(sent[0]["sender"], "@teacher:test")
        self.assertNotIn("pangea.course_plan", [e["type"] for e in sent])
        self.assertEqual(
            body["can_do_list"],
            {
                "built": True,
                "previous_revision": 1,
                "revision": 2,
                "missions": 2,
                "problems": {},
                "replaced": "$built",
                "current_activities": 5,
                "rebuilt_activities": 4,
                # x1 and x2 under lo-a; lo-b selects nothing, so none of its own.
                "current_outside_selection": 2,
            },
        )

    async def test_a_list_published_since_is_refused_and_never_read(self) -> None:
        endpoint, api = self._replacing(copy_event_id="$teacher-publish")

        status, body = await self._post(
            endpoint, expected="new", replace_copy_event_id="$built"
        )

        self.assertEqual(status, 409, body)
        self.assertEqual(body["current_copy_event_id"], "$teacher-publish")
        api.create_and_send_event_into_room.assert_not_awaited()
        endpoint._course_missions.read.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_a_space_with_no_list_is_refused(self) -> None:
        endpoint, api = self._endpoint(can_do_list=None, on="new")

        status, body = await self._post(
            endpoint, expected="new", replace_copy_event_id="$built"
        )

        self.assertEqual(status, 409, body)
        self.assertIsNone(body["current_copy_event_id"])
        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_a_list_published_during_the_read_is_refused(self) -> None:
        endpoint, api = self._replacing()
        first = await api.get_room_state()
        published = dict(first)
        published[("pangea.course_missions", "")] = SimpleNamespace(
            content=self._WRONG_LIST, event_id="$teacher-publish"
        )
        api.get_room_state = AsyncMock(side_effect=[first, published])

        with self.assertLogs(module.logger, level="WARNING"):
            status, body = await self._post(
                endpoint, expected="new", replace_copy_event_id="$built"
            )

        self.assertEqual(status, 409, body)
        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_a_list_published_during_the_counts_read_is_refused(self) -> None:
        # The counts read the quest again; a teacher who publishes while it is
        # in flight still wins (Codex review of #377).
        endpoint, api = self._replacing()
        first = await api.get_room_state()
        published = dict(first)
        published[("pangea.course_missions", "")] = SimpleNamespace(
            content=self._WRONG_LIST, event_id="$teacher-publish"
        )

        async def selections(quest_id: str) -> Dict[str, List[str]]:
            api.get_room_state = AsyncMock(return_value=published)
            return {"lo-a": ["s1", "s2", "s3"], "lo-b": []}

        endpoint._course_missions.quest_selections = selections  # type: ignore[method-assign]

        with self.assertLogs(module.logger, level="WARNING"):
            status, body = await self._post(
                endpoint, expected="new", replace_copy_event_id="$built"
            )

        self.assertEqual(status, 409, body)
        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_a_dry_run_reports_the_counts_and_writes_nothing(self) -> None:
        endpoint, api = self._replacing()

        status, body = await self._post(
            endpoint, expected="new", replace_copy_event_id="$built", dry_run=True
        )

        self.assertEqual(status, 200, body)
        api.create_and_send_event_into_room.assert_not_awaited()
        self.assertEqual(body["can_do_list"]["rebuilt_activities"], 4)
        self.assertFalse(body["changed"])

    async def test_a_list_that_cannot_be_rebuilt_is_left_alone_and_refused(
        self,
    ) -> None:
        endpoint, api = self._replacing()
        endpoint._course_missions.read = AsyncMock(  # type: ignore[method-assign]
            side_effect=CourseMissionsError(CMS_UNAVAILABLE)
        )

        answer = await self._refused(
            endpoint, api, expected="new", replace_copy_event_id="$built"
        )

        self.assertEqual(answer["status"], 502)


class TestReplaceKeepingProgress(_ReplacingHarness):
    """A course whose quest trimmed its choices follows the quest, and keeps
    every id its list credits, its due dates and its own Can-Dos."""

    async def _post(self, endpoint, instructors=None, expected="old", **extra):  # type: ignore[override]
        # The script sends the course's own name and topic back: no rename.
        extra = {"title": "Old title", "description": "", **extra}
        return await super()._post(endpoint, instructors, expected, **extra)

    _TEACHER_LIST = {
        "version": 1,
        "revision": 5,
        "time_zone": "America/Chicago",
        "missions": [
            {
                "lo_id": "lo-a",
                "activity_ids": ["s1", "x1"],
                "due_at": "2026-12-01T00:00:00Z",
            }
        ],
    }

    async def test_old_ids_move_to_removed_one_revision_up(self) -> None:
        endpoint, api = self._replacing()

        status, body = await self._post(
            endpoint,
            expected="new",
            replace_copy_event_id="$built",
            keep_progress=True,
        )

        self.assertEqual(status, 200, body)
        (sent,) = self._sent(api)
        self.assertEqual(sent["type"], "pangea.course_missions")
        self.assertEqual(
            sent["content"],
            {
                "version": 1,
                "revision": 2,
                "missions": [
                    {
                        "lo_id": "lo-a",
                        "activity_ids": ["s1", "s2", "s3"],
                        "removed_activity_ids": ["x1", "x2"],
                    },
                    {
                        "lo_id": "lo-b",
                        "activity_ids": ["b1"],
                        "removed_activity_ids": ["b2"],
                    },
                ],
            },
        )
        merged = body["can_do_list"]
        self.assertEqual(merged["lost"], [])
        self.assertTrue(merged["nothing_lost"])
        self.assertEqual(
            [(e["lo_id"], e["moved_to_removed"]) for e in merged["entries"]],
            [("lo-a", 2), ("lo-b", 1)],
        )

    async def test_a_teachers_list_is_refused_before_any_read(self) -> None:
        endpoint, api = self._endpoint(
            can_do_list=self._TEACHER_LIST,
            built=self._BUILT_LIST,
            on="new",
            copy_event_id="$built",
        )

        extras: List[Dict[str, Any]] = [{}, {"keep_progress": True}]
        for extra in extras:
            with self.subTest(extra=extra):
                status, body = await self._post(
                    endpoint, expected="new", replace_copy_event_id="$built", **extra
                )
                self.assertEqual(status, 409, body)
                self.assertEqual(body["code"], "teacher_list")
        api.create_and_send_event_into_room.assert_not_awaited()
        endpoint._course_missions.read.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_a_teachers_list_named_explicitly_keeps_its_edits(self) -> None:
        endpoint, api = self._endpoint(
            can_do_list=self._TEACHER_LIST,
            built=self._BUILT_LIST,
            on="new",
            copy_event_id="$built",
        )
        endpoint._course_missions.quest_selections = AsyncMock(  # type: ignore[method-assign]
            return_value={"lo-a": ["s1", "s2", "s3"], "lo-b": []}
        )

        status, body = await self._post(
            endpoint,
            expected="new",
            replace_copy_event_id="$built",
            keep_progress=True,
            allow_teacher_list=True,
        )

        self.assertEqual(status, 200, body)
        (sent,) = self._sent(api)
        self.assertEqual(sent["content"]["time_zone"], "America/Chicago")
        self.assertEqual(sent["content"]["revision"], 6)
        self.assertEqual(
            sent["content"]["missions"][0],
            {
                "lo_id": "lo-a",
                "activity_ids": ["s1", "s2", "s3"],
                "removed_activity_ids": ["x1"],
                "due_at": "2026-12-01T00:00:00Z",
            },
        )
        self.assertTrue(body["can_do_list"]["nothing_lost"])

    async def test_a_merge_that_would_lose_anything_is_refused(self) -> None:
        endpoint, api = self._replacing()

        with patch.object(
            module,
            "merge_can_do_list",
            return_value=MergedCanDoList(
                {"version": 1, "revision": 1, "missions": []},
                [],
                ["lo-a: Can-Do dropped"],
            ),
        ), self.assertLogs(module.logger, level="ERROR"):
            status, body = await self._post(
                endpoint,
                expected="new",
                replace_copy_event_id="$built",
                keep_progress=True,
            )

        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "would_lose")
        self.assertEqual(body["lost"], ["lo-a: Can-Do dropped"])
        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_a_dry_run_reports_each_entry_and_writes_nothing(self) -> None:
        endpoint, api = self._replacing()

        status, body = await self._post(
            endpoint,
            expected="new",
            replace_copy_event_id="$built",
            keep_progress=True,
            dry_run=True,
        )

        self.assertEqual(status, 200, body)
        api.create_and_send_event_into_room.assert_not_awaited()
        self.assertEqual(len(body["can_do_list"]["entries"]), 2)
        self.assertTrue(body["can_do_list"]["nothing_lost"])


class TestRestoreCanDoList(_EndpointHarness):
    """Undo: a course's earlier list written back as a new revision, only
    while the course still holds the list the operator names."""

    async def _post(self, endpoint, instructors=None, expected="old", **extra):  # type: ignore[override]
        # The script sends the course's own name and topic back: no rename.
        extra = {"title": "Old title", "description": "", **extra}
        return await super()._post(endpoint, instructors, expected, **extra)

    _EARLIER = {
        "version": 1,
        "revision": 1,
        "missions": [{"lo_id": "lo-a", "activity_ids": ["x1", "x2"]}],
    }
    _NOW = {
        "version": 1,
        "revision": 2,
        "missions": [
            {
                "lo_id": "lo-a",
                "activity_ids": ["s1"],
                "removed_activity_ids": ["x1", "x2"],
            }
        ],
    }

    def _restoring(
        self, earlier: Any = None, copy_event_id: str = "$merged"
    ) -> tuple[SetCoursePlan, MagicMock]:
        endpoint, api = self._endpoint(
            can_do_list=self._NOW, on="new", copy_event_id=copy_event_id
        )
        if earlier is None:
            earlier = SimpleNamespace(
                event_id="$earlier",
                room_id="!space:test",
                type="pangea.course_missions",
                state_key="",
                content=self._EARLIER,
            )
        api._hs.get_datastores.return_value.main.get_event = AsyncMock(
            return_value=earlier
        )
        endpoint._store = api._hs.get_datastores.return_value.main
        return endpoint, api

    async def test_the_earlier_list_is_written_back_one_revision_up(self) -> None:
        endpoint, api = self._restoring()

        status, body = await self._post(
            endpoint,
            expected="new",
            replace_copy_event_id="$merged",
            restore_copy_event_id="$earlier",
        )

        self.assertEqual(status, 200, body)
        (sent,) = self._sent(api)
        self.assertEqual(sent["type"], "pangea.course_missions")
        self.assertEqual(sent["content"], {**self._EARLIER, "revision": 3})
        self.assertEqual(sent["sender"], "@teacher:test")
        self.assertEqual(body["can_do_list"]["restored"], "$earlier")
        endpoint._course_missions.read.assert_not_awaited()  # type: ignore[attr-defined]

    async def test_a_list_published_since_is_not_undone(self) -> None:
        endpoint, api = self._restoring(copy_event_id="$teacher-publish")

        status, body = await self._post(
            endpoint,
            expected="new",
            replace_copy_event_id="$merged",
            restore_copy_event_id="$earlier",
        )

        self.assertEqual(status, 409, body)
        self.assertEqual(body["current_copy_event_id"], "$teacher-publish")
        api.create_and_send_event_into_room.assert_not_awaited()

    async def test_only_a_can_do_list_of_the_same_room_is_restored(self) -> None:
        base: Dict[str, Any] = {
            "event_id": "$earlier",
            "room_id": "!space:test",
            "type": "pangea.course_missions",
            "state_key": "",
            "content": self._EARLIER,
        }
        bads: List[Optional[Dict[str, Any]]] = [
            None,
            {**base, "room_id": "!other:test"},
            {**base, "type": "m.room.name"},
            {**base, "state_key": "x"},
            {**base, "content": {}},
        ]
        for bad in bads:
            with self.subTest(bad=bad):
                earlier = None if bad is None else SimpleNamespace(**bad)
                endpoint, api = self._restoring()
                api._hs.get_datastores.return_value.main.get_event = AsyncMock(
                    return_value=earlier
                )
                status, body = await self._post(
                    endpoint,
                    expected="new",
                    replace_copy_event_id="$merged",
                    restore_copy_event_id="$earlier",
                )
                self.assertEqual(status, 422, body)
                api.create_and_send_event_into_room.assert_not_awaited()

    async def test_a_list_published_while_reading_the_earlier_one_wins(
        self,
    ) -> None:
        endpoint, api = self._restoring()
        first = await api.get_room_state()
        published = dict(first)
        published[("pangea.course_missions", "")] = SimpleNamespace(
            content=self._NOW, event_id="$teacher-publish"
        )
        api.get_room_state = AsyncMock(side_effect=[first, published])

        with self.assertLogs(module.logger, level="WARNING"):
            status, body = await self._post(
                endpoint,
                expected="new",
                replace_copy_event_id="$merged",
                restore_copy_event_id="$earlier",
            )

        self.assertEqual(status, 409, body)
        api.create_and_send_event_into_room.assert_not_awaited()


class TestReplaceRequest(unittest.TestCase):
    _VALID = {
        "room_id": "!space:test",
        "quest_id": "q",
        "expected_quest_id": "q",
        "title": "T",
        "description": "",
        "replace_copy_event_id": "$built",
    }

    def test_a_replacement_names_its_event_and_stays_on_the_quest(self) -> None:
        parsed, error = parse_request(self._VALID)
        assert parsed is not None, error
        self.assertEqual(parsed.replace_copy_event_id, "$built")
        parsed, _ = parse_request(
            {k: v for k, v in self._VALID.items() if k != "replace_copy_event_id"}
        )
        assert parsed is not None
        self.assertIsNone(parsed.replace_copy_event_id)
        for bad in (
            {**self._VALID, "quest_id": "other"},
            {**self._VALID, "replace_copy_event_id": ""},
            {**self._VALID, "replace_copy_event_id": 3},
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_request(bad)[0])

    def test_keeping_progress_allowing_and_restoring_go_with_a_replacement(
        self,
    ) -> None:
        parsed, error = parse_request(
            {**self._VALID, "keep_progress": True, "allow_teacher_list": True}
        )
        assert parsed is not None, error
        self.assertTrue(parsed.keep_progress)
        self.assertTrue(parsed.allow_teacher_list)
        parsed, error = parse_request({**self._VALID, "restore_copy_event_id": "$e"})
        assert parsed is not None, error
        self.assertEqual(parsed.restore_copy_event_id, "$e")
        plain = {k: v for k, v in self._VALID.items() if k != "replace_copy_event_id"}
        for bad in (
            {**plain, "keep_progress": True},
            {**plain, "allow_teacher_list": True},
            {**plain, "restore_copy_event_id": "$e"},
            {**self._VALID, "keep_progress": "yes"},
            {**self._VALID, "allow_teacher_list": 1},
            {**self._VALID, "restore_copy_event_id": ""},
            {**self._VALID, "restore_copy_event_id": "$e", "keep_progress": True},
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_request(bad)[0])


if __name__ == "__main__":
    unittest.main()
