import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from synapse_pangea_chat.notice_delivery.eligibility import NoticeEligibility
from synapse_pangea_chat.notice_delivery.request import NoticeRequest
from tests import test_notice_schedule
from tests.test_notice_schedule import scheduled_body


class TestEligibility(unittest.IsolatedAsyncioTestCase):
    def setup_gate(self, conditions):
        handler, api = test_notice_schedule.TestScheduledDelivery().handler()
        body = scheduled_body()
        body["eligibility"] = conditions
        gate = NoticeEligibility(api, handler._delivery_log)
        gate._query = AsyncMock(return_value=[])
        return gate, body, NoticeRequest.parse(body), api

    async def test_no_conditions_do_not_read_data(self):
        gate, body, req, _ = self.setup_gate({})
        self.assertIsNone(await gate.check(body, req, "42"))
        gate._query.assert_not_awaited()

    async def test_returned_recipient_is_suppressed(self):
        gate, body, req, api = self.setup_gate({"recipient_not_returned": True})
        api._hs.get_presence_handler.return_value.current_state_for_user.return_value = SimpleNamespace(
            last_active_ts=0
        )
        gate._query.return_value = [(1,)]
        self.assertEqual(await gate.check(body, req, "42"), "recipient_returned")

    async def test_failed_evidence_suppresses_and_logs(self):
        gate, body, req, _ = self.setup_gate({"activity_not_started": True})
        gate._query.side_effect = RuntimeError("statement timeout")
        with self.assertLogs(
            "synapse_pangea_chat.notice_delivery.eligibility", level="ERROR"
        ):
            self.assertEqual(
                await gate.check(body, req, "42"), "eligibility_unavailable"
            )

    async def test_room_cap_stops_before_state_query(self):
        gate, body, req, _ = self.setup_gate({"activity_not_started": True})
        gate._query.return_value = [(f"!{i}:test",) for i in range(257)]
        self.assertEqual(
            await gate.check(body, req, "42"), "eligibility_evidence_limit"
        )
        self.assertEqual(gate._query.await_count, 1)

    async def test_prior_contact_is_scoped_and_excludes_own_reservation(self):
        gate, body, req, _ = self.setup_gate({"min_contact_spacing_ms": 28800000})
        gate.log._request = AsyncMock(return_value=(200, {"docs": [{"id": "other"}]}))
        self.assertEqual(await gate.check(body, req, "42"), "contact_spacing")
        query = gate.log._request.await_args.args[1]
        self.assertIn("limit=1", query)
        self.assertIn("not_equals%5D=42", query)

    def test_unknown_or_wrong_types_rejected(self):
        for conditions in (
            {"typo": True},
            {"recipient_not_returned": "yes"},
            {"min_contact_spacing_ms": True},
            {"min_contact_spacing_ms": -1},
        ):
            with self.subTest(conditions=conditions), self.assertRaises(ValueError):
                NoticeRequest.parse({**scheduled_body(), "eligibility": conditions})

    async def test_suppressed_delivery_has_log_but_no_event_or_email(self):
        handler, api = test_notice_schedule.TestScheduledDelivery().handler()
        body = scheduled_body()
        body["eligibility"] = {"recipient_not_returned": True}
        api._hs.get_presence_handler.return_value.current_state_for_user.return_value = SimpleNamespace(
            last_active_ts=9999999999999
        )
        result = await handler._deliver_scheduled(body)
        self.assertEqual(result["reason"], "recipient_returned")
        handler._delivery_log.finish.assert_awaited_once()
        api.create_and_send_event_into_room.assert_not_awaited()
        api._hs.get_send_email_handler.return_value.send_email.assert_not_awaited()

    async def test_activity_progress_is_specific_to_recipient_and_activity(self):
        for actor, activity, expected in (
            ("@alice:my.domain.name", "activity-1", "activity_already_started"),
            ("@other:test", "activity-1", None),
            ("@alice:my.domain.name", "different", None),
        ):
            gate, body, req, _ = self.setup_gate({"activity_not_started": True})
            gate._query.side_effect = [
                [("!session:test",)],
                [
                    (
                        "!session:test",
                        "pangea.activity_plan",
                        json.dumps({"content": {"activity_id": activity}}),
                    ),
                    (
                        "!session:test",
                        "pangea.activity_roles",
                        json.dumps(
                            {
                                "content": {
                                    "roles": {
                                        "one": {
                                            "user_id": actor,
                                            "finished_at": "2026-10-01T00:00:00Z",
                                        }
                                    }
                                }
                            }
                        ),
                    ),
                ],
            ]
            self.assertEqual(await gate.check(body, req, "42"), expected)

    async def test_session_full_ended_and_inaccessible(self):
        for membership, capacity, finished, expected in (
            ("join", 2, False, None),
            ("join", 1, False, "session_full"),
            ("join", 2, True, "session_ended"),
            ("ban", 2, False, "session_inaccessible"),
        ):
            gate, body, req, api = self.setup_gate({})
            body.update(
                session_room_id="!session:test", eligibility={"session_available": True}
            )
            roles = {
                "roles": {
                    "one": {
                        "user_id": req.user_id,
                        "finished_at": "2026-10-01" if finished else None,
                    }
                }
            }

            def event(content):
                return SimpleNamespace(content=content)

            state = {
                ("pangea.activity_plan", ""): event(
                    {
                        "activity_id": "activity-1",
                        "roles": {str(n): {} for n in range(capacity)},
                    }
                ),
                ("pangea.activity_roles", ""): event(roles),
                ("m.room.member", req.user_id): event({"membership": membership}),
            }
            api.get_room_state = AsyncMock(return_value=state)
            self.assertEqual(await gate.check(body, req, "42"), expected)

    async def test_session_reference_reads_its_pinned_plan(self):
        gate, body, req, api = self.setup_gate({})
        body.update(
            session_room_id="!session:test", eligibility={"session_available": True}
        )
        api.get_room_state = AsyncMock(
            return_value={
                ("pangea.activity_plan", ""): SimpleNamespace(
                    content={"activity_id": "activity-1", "version_id": "version-1"}
                ),
                ("m.room.member", req.user_id): SimpleNamespace(
                    content={"membership": "join"}
                ),
            }
        )
        gate.log._request = AsyncMock(
            return_value=(
                200,
                {
                    "version": {
                        "res": {
                            "plan": {
                                "activity_id": "activity-1",
                                "roles": [{"role_id": "one"}, {"role_id": "two"}],
                            }
                        }
                    }
                },
            )
        )
        self.assertIsNone(await gate.check(body, req, "42"))
        gate.log._request.assert_awaited_once_with(
            "GET", "/version-1", collection="activities-v2/versions"
        )

    async def test_saved_completion_survives_missing_current_role(self):
        gate, body, req, _ = self.setup_gate({"activity_not_started": True})
        gate._query.side_effect = [
            [("!analytics:test",)],
            [
                (
                    "!analytics:test",
                    "m.room.create",
                    json.dumps({"sender": req.user_id, "content": {}}),
                ),
                (
                    "!analytics:test",
                    "pangea.activity_room_ids",
                    json.dumps({"content": {"room_ids": ["!old:test"]}}),
                ),
            ],
            [("!old:test", json.dumps({"content": {"activity_id": "activity-1"}}))],
        ]
        self.assertEqual(
            await gate.check(body, req, "42"), "activity_already_completed"
        )
