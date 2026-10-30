import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

from synapse_pangea_chat.notice_delivery.eligibility import NoticeEligibility
from synapse_pangea_chat.notice_delivery.request import NoticeRequest
from tests import test_notice_schedule
from tests.test_notice_schedule import scheduled_body

# What a current client writes into `pangea.activity_plan`: a reference whose
# version_id is a content signature, not a CMS version id.
REFERENCE = {
    "activity_id": "activity-1",
    "version_id": "0123456789abcdef0123456789abcdef",
    "source_course_id": "course-1",
}
CANONICAL_QUERY = "?" + urlencode(
    {
        "where[res.plan.activity_id][equals]": "activity-1",
        "where[req.source_request_hash][exists]": "false",
        "limit": 2,
        "depth": 0,
    }
)


def canonical_row(roles):
    plan = {
        "activity_id": "activity-1",
        "roles": [{"role_id": n} for n in range(roles)],
    }
    return 200, {"docs": [{"res": {"plan": plan}}]}


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

    async def test_inactive_recipient_uses_one_indexed_activity_read(self):
        gate, body, req, api = self.setup_gate({"recipient_not_returned": True})
        api._hs.get_presence_handler.return_value.current_state_for_user.return_value = SimpleNamespace(
            last_active_ts=0
        )
        self.assertIsNone(await gate.check(body, req, "42"))
        gate._query.assert_awaited_once()
        self.assertIn(
            "FROM user_ips WHERE user_id = ? AND last_seen > ? LIMIT 1",
            gate._query.await_args.args[0],
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

    async def test_session_reference_reads_the_canonical_plan(self):
        gate, body, req, api = self.setup_gate({})
        body.update(
            session_room_id="!session:test", eligibility={"session_available": True}
        )
        api.get_room_state = AsyncMock(
            return_value={
                ("pangea.activity_plan", ""): SimpleNamespace(content=REFERENCE),
                ("m.room.member", req.user_id): SimpleNamespace(
                    content={"membership": "join"}
                ),
            }
        )
        gate.log._request = AsyncMock(return_value=canonical_row(2))
        self.assertIsNone(await gate.check(body, req, "42"))
        gate.log._request.assert_awaited_once_with(
            "GET", CANONICAL_QUERY, collection="activities-v2"
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


BOT = "@bot:test"
LEARNER = "LEARNER"  # the recipient, filled in by the fixture


class TestBotFillsOpenSeat(unittest.IsolatedAsyncioTestCase):
    """The open-session notice goes out only while pangea-bot's sweep would
    take the session's last seat at the hour."""

    def gate(self, members, *, capacity=2, extra=None, roles=None, readers=()):
        """Every joined human holds a role unless `roles` says otherwise;
        readers are joined without one, as a course admin
        course_session_admins joins is."""
        gate, body, req, api = TestEligibility().setup_gate({})
        body.update(
            sender_id=BOT,
            session_room_id="!session:test",
            eligibility={"bot_fills_open_seat": True},
        )
        if roles is None:
            roles = {
                str(n): {"user_id": user}
                for n, (user, membership) in enumerate(members.items())
                if membership == "join" and user != BOT
            }
        roles = {
            key: {**role, "user_id": req.user_id}
            if role.get("user_id") == LEARNER
            else role
            for key, role in roles.items()
        }
        members = {**members, **{reader: "join" for reader in readers}}
        state = {
            ("pangea.activity_plan", ""): SimpleNamespace(
                content={
                    "activity_id": "activity-1",
                    "roles": {str(n): {} for n in range(capacity)},
                }
            ),
            ("pangea.activity_roles", ""): SimpleNamespace(
                content={"roles": roles or {}}
            ),
            **(extra or {}),
        }
        for user, membership in members.items():
            key = ("m.room.member", req.user_id if user == LEARNER else user)
            state[key] = SimpleNamespace(content={"membership": membership})
        api.get_room_state = AsyncMock(return_value=state)
        return gate, body, req

    async def test_only_the_last_open_seat_with_learner_and_bot_in(self):
        friend = "@friend:test"
        for members, capacity, expected in (
            ({LEARNER: "join", BOT: "join"}, 2, None),
            ({LEARNER: "join", BOT: "join", friend: "invite"}, 3, None),
            ({LEARNER: "join", BOT: "join", friend: "invite"}, 2, "session_full"),
            ({LEARNER: "join", BOT: "join"}, 3, "seats_open"),
            ({LEARNER: "leave", BOT: "join", friend: "join"}, 3, "recipient_left"),
            ({LEARNER: "join", BOT: "leave"}, 2, "bot_not_in_session"),
        ):
            gate, body, req = self.gate(members, capacity=capacity)
            with self.subTest(members=members, capacity=capacity):
                self.assertEqual(await gate.check(body, req, "42"), expected)

    async def test_a_member_without_a_role_holds_no_seat(self):
        # course_session_admins joins a course's admins to every new session.
        admins = ("@teacher:test", "@coteacher:test")
        gate, body, req = self.gate({LEARNER: "join", BOT: "join"}, readers=admins)
        self.assertIsNone(await gate.check(body, req, "42"))

    async def test_a_role_left_behind_by_someone_who_left_holds_no_seat(self):
        roles = {"0": {"user_id": "@peer:test"}, "1": {"user_id": LEARNER}}
        gate, body, req = self.gate(
            {LEARNER: "join", BOT: "join", "@peer:test": "leave"}, roles=roles
        )
        self.assertIsNone(await gate.check(body, req, "42"))

    async def test_bot_chosen_or_session_ended_sends_nothing(self):
        in_room = {LEARNER: "join", BOT: "join"}
        marker = {("pangea.bot_participant", ""): SimpleNamespace(content={})}
        summary = {("pangea.activity_summary", ""): SimpleNamespace(content={})}
        finished = {"one": {"user_id": "@x:test", "finished_at": "2026-10-01"}}
        for extra, roles, expected in (
            (marker, None, "bot_already_chosen"),
            (summary, None, "session_ended"),
            ({}, finished, "session_ended"),
        ):
            gate, body, req = self.gate(in_room, extra=extra, roles=roles)
            with self.subTest(expected=expected):
                self.assertEqual(await gate.check(body, req, "42"), expected)

    async def test_reference_only_plan_is_sized_from_the_canonical_row(self):
        plan = {("pangea.activity_plan", ""): SimpleNamespace(content=REFERENCE)}
        for roles, expected in ((2, None), (3, "seats_open")):
            gate, body, req = self.gate({LEARNER: "join", BOT: "join"}, extra=plan)
            gate.log._request = AsyncMock(return_value=canonical_row(roles))
            with self.subTest(roles=roles):
                self.assertEqual(await gate.check(body, req, "42"), expected)
                gate.log._request.assert_awaited_once_with(
                    "GET", CANONICAL_QUERY, collection="activities-v2"
                )

    def test_condition_requires_the_session(self):
        body = {**scheduled_body(), "eligibility": {"bot_fills_open_seat": True}}
        with self.assertRaises(ValueError):
            NoticeRequest.parse(body)
