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
    SetCoursePlan,
    parse_request,
    plan_update,
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


class TestParseRequest(unittest.TestCase):
    _VALID = {
        "room_id": "!space:pangea.chat",
        "quest_id": "new",
        "expected_quest_id": "old",
    }

    def test_valid_body(self) -> None:
        parsed, error = parse_request({**self._VALID, "dry_run": True})

        self.assertIsNone(error)
        self.assertEqual(parsed, ("!space:pangea.chat", "new", "old", True))

    def test_dry_run_defaults_to_false(self) -> None:
        parsed, error = parse_request(self._VALID)

        self.assertIsNone(error)
        assert parsed is not None
        self.assertFalse(parsed[3])

    def test_invalid_bodies_are_refused(self) -> None:
        bad = [
            "not an object",
            {**self._VALID, "room_id": "#alias:pangea.chat"},
            {**self._VALID, "room_id": 5},
            {**self._VALID, "quest_id": ""},
            {**self._VALID, "quest_id": "x" * 256},
            {k: v for k, v in self._VALID.items() if k != "expected_quest_id"},
            {**self._VALID, "dry_run": "yes"},
        ]
        for body in bad:
            with self.subTest(body=body):
                parsed, error = parse_request(body)
                self.assertIsNone(parsed)
                self.assertIsInstance(error, str)


class TestEndpointFailures(unittest.IsolatedAsyncioTestCase):
    """A refused write must not read as the operator's own auth failure."""

    _BODY = {"room_id": "!space:test", "quest_id": "new", "expected_quest_id": "old"}

    def _endpoint(self) -> tuple[SetCoursePlan, MagicMock]:
        api = MagicMock()
        api._hs.get_auth.return_value.get_user_by_req = AsyncMock(
            return_value=SimpleNamespace(
                user=SimpleNamespace(to_string=lambda: "@op:test")
            )
        )
        api.is_user_admin = AsyncMock(return_value=True)
        plan = SimpleNamespace(content={"uuid": "old", "l2": "es"})
        api.get_room_state = AsyncMock(return_value={("pangea.course_plan", ""): plan})
        api.create_and_send_event_into_room = AsyncMock()
        config = SimpleNamespace(course_plan_state_event_type=None)
        return SetCoursePlan(api, config), api  # type: ignore[arg-type]

    async def _post(self, endpoint: SetCoursePlan) -> tuple[int, Dict[str, Any]]:
        with patch.object(
            module, "extract_body_json", AsyncMock(return_value=self._BODY)
        ), patch.object(
            module, "select_state_sender", AsyncMock(return_value="@teacher:test")
        ), patch.object(
            module, "respond_with_json"
        ) as respond:
            await endpoint._async_render_POST(MagicMock())
        return respond.call_args.args[1], respond.call_args.args[2]

    async def test_a_write_the_room_refuses_is_a_logged_conflict(self) -> None:
        endpoint, api = self._endpoint()
        api.create_and_send_event_into_room.side_effect = AuthError(
            403, "User @teacher:test not in room !space:test"
        )

        with self.assertLogs(module.logger, level="ERROR") as logs:
            status, body = await self._post(endpoint)

        self.assertEqual(status, 409)
        self.assertEqual(body["errcode"], "M_FORBIDDEN")
        self.assertIn("@teacher:test", logs.output[0])

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
