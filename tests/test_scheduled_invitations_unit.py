"""Scheduled class invitations: send_at gating, no double send, revocation,
and the one sender, over the real store on an in-memory database. The real
email through a real Synapse is ``test_scheduled_invitations_e2e.py``."""

from __future__ import annotations

import sqlite3
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from twisted.internet.error import ConnectionLost, ConnectionRefusedError
from twisted.mail.smtp import SMTPDeliveryError, SMTPTimeoutError

from synapse_pangea_chat.student_invitations.api import MAX_SEND
from synapse_pangea_chat.student_invitations.claim import now_ms
from synapse_pangea_chat.student_invitations.scheduled_send import (
    BATCH,
    MAX_SEND_AHEAD_MS,
    RETRY_WINDOW_MS,
    TICK_SECONDS,
    ScheduledInvites,
)
from synapse_pangea_chat.student_invitations.store import (
    SCHEMA,
    migrate,
)
from tests.test_seat_invitations_unit import (
    INVITED,
    ROOM,
    STUDENT,
    TEACHER,
    Harness,
    _Base,
)

DAY_MS = 24 * 60 * 60 * 1000
SECOND = "second@school.example"
THIRD = "third@school.example"


class _Scheduled(_Base):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.now = now_ms()
        self.sender = ScheduledInvites(
            self.h.api,
            self.h.store,
            self.h.rooms,
            self.h.mailer,
            now=lambda: self.now,
        )
        self.h.rooms.codes[ROOM] = "cl4sscd"

    async def schedule(
        self, *ids: str, at: Optional[int] = None, expected: int = 0
    ) -> Dict[str, Any]:
        status, body = await self.h.handlers.schedule(
            TEACHER,
            {
                "room_id": ROOM,
                "send_at_ms": self.now + DAY_MS if at is None else at,
                "items": [
                    {"invitation_id": i, "expected_send_count": expected} for i in ids
                ],
            },
        )
        self.assertEqual(status, 200, body)
        return body

    async def send_now(self, ident: str, expected: int = 0) -> Dict[str, Any]:
        status, body = await self.h.handlers.send(
            TEACHER,
            {
                "room_id": ROOM,
                "items": [{"invitation_id": ident, "expected_send_count": expected}],
            },
        )
        self.assertEqual(status, 200, body)
        return body

    def sent_ids(self) -> List[str]:
        return [ident for ident, _ in self.h.mailer.sent]


class TestScheduling(_Scheduled):
    async def test_a_scheduled_row_is_not_emailed_until_its_time(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        body = await self.schedule(ident)
        self.assertEqual(body["results"][0]["outcome"], "scheduled")
        self.assertEqual(
            (await self.h.listing())[ident]["send_at_ms"], self.now + DAY_MS
        )

        await self.sender.run_due()
        self.assertEqual(self.h.mailer.sent, [])

        self.now += DAY_MS - 1
        await self.sender.run_due()
        self.assertEqual(self.h.mailer.sent, [])

        self.now += 1
        await self.sender.run_due()
        self.assertEqual(self.h.mailer.sent, [(ident, "cl4sscd")])
        row = await self.h.row(ident)
        self.assertEqual(row["send_count"], 1)
        self.assertEqual(row["last_sent_at_ms"], self.now)
        self.assertIsNone(row["send_at_ms"])

    async def test_the_send_route_will_not_email_a_scheduled_row_early(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.schedule(ident)
        body = await self.send_now(ident)
        self.assertEqual(
            body["results"],
            [{"invitation_id": ident, "outcome": "skipped", "reason": "scheduled"}],
        )
        self.assertEqual(self.h.mailer.sent, [])
        self.assertEqual((await self.h.row(ident))["send_count"], 0)

    async def test_a_row_with_no_send_at_sends_now_and_the_loop_leaves_it(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        body = await self.send_now(ident)
        self.assertEqual(body["results"][0]["outcome"], "sent")
        await self.sender.run_due()
        self.assertEqual(self.sent_ids(), [ident])
        (added,) = await self.h.add(SECOND)
        await self.sender.run_due()
        self.assertEqual(self.sent_ids(), [ident])
        self.assertIsNone(
            (await self.h.listing())[added["invitation_id"]]["send_at_ms"]
        )

    async def test_a_second_schedule_call_changes_the_day_and_records_it_once(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.schedule(ident)
        await self.schedule(ident)
        await self.schedule(ident, at=self.now + 2 * DAY_MS)
        self.assertEqual((await self.h.row(ident))["send_at_ms"], self.now + 2 * DAY_MS)
        scheduled = [
            e for e in await self.h.events() if e["action"] == "invite_scheduled"
        ]
        self.assertEqual(len(scheduled), 2)
        self.assertEqual({e["actor"] for e in scheduled}, {TEACHER})

    async def test_schedule_is_compare_and_set_on_the_send_count(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.send_now(ident)
        body = await self.schedule(ident)
        self.assertEqual(
            body["results"],
            [{"invitation_id": ident, "outcome": "skipped", "reason": "stale"}],
        )
        self.assertIsNone((await self.h.row(ident))["send_at_ms"])
        at = self.now + DAY_MS
        await self.schedule(ident, at=at, expected=1)
        body = await self.schedule(ident, at=at, expected=0)
        self.assertEqual(body["results"][0]["reason"], "stale")

    async def test_send_at_is_validated_on_the_server(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        items = [{"invitation_id": ident, "expected_send_count": 0}]
        for bad in (
            None,
            "tomorrow",
            True,
            1.5,
            self.now - 1,
            0,
            now_ms() + MAX_SEND_AHEAD_MS + DAY_MS,
        ):
            status, body = await self.h.handlers.schedule(
                TEACHER, {"room_id": ROOM, "send_at_ms": bad, "items": items}
            )
            self.assertEqual(status, 400, bad)
            self.assertEqual(body["errcode"], "ORG.PANGEA.INVALID_SEND_AT")
        self.assertIsNone((await self.h.row(ident))["send_at_ms"])

    async def test_schedule_is_for_course_admins_and_needs_a_class_code(self):
        (row,) = await self.h.add(INVITED)
        items = [
            {"invitation_id": row["invitation_id"], "expected_send_count": 0},
        ]
        args = {"room_id": ROOM, "send_at_ms": self.now + DAY_MS, "items": items}
        status, _ = await self.h.handlers.schedule(STUDENT, args)
        self.assertEqual(status, 403)
        del self.h.rooms.codes[ROOM]
        status, body = await self.h.handlers.schedule(TEACHER, args)
        self.assertEqual(status, 400)
        self.assertEqual(body["errcode"], "ORG.PANGEA.NO_JOIN_CODE")

    async def test_schedule_takes_at_most_one_send_batch(self):
        items = [
            {"invitation_id": str(i), "expected_send_count": 0}
            for i in range(MAX_SEND + 1)
        ]
        status, _ = await self.h.handlers.schedule(
            TEACHER,
            {"room_id": ROOM, "send_at_ms": self.now + DAY_MS, "items": items},
        )
        self.assertEqual(status, 400)


class TestNeverEmailed(_Scheduled):
    async def test_a_revoked_row_is_never_emailed(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.schedule(ident)
        status, _ = await self.h.handlers.revoke(
            TEACHER, {"room_id": ROOM, "invitation_id": ident}
        )
        self.assertEqual(status, 200)
        self.now += DAY_MS
        await self.sender.run_due()
        self.assertEqual(self.h.mailer.sent, [])

    async def test_a_revoked_row_does_not_hold_up_the_rows_behind_it(self):
        rows = await self.h.add(INVITED, SECOND)
        first, second = [r["invitation_id"] for r in rows]
        await self.schedule(first, at=self.now + DAY_MS)
        await self.schedule(second, at=self.now + DAY_MS + 1)
        await self.h.handlers.revoke(TEACHER, {"room_id": ROOM, "invitation_id": first})
        self.now += DAY_MS + 1
        await self.sender.run_due()
        self.assertEqual(self.sent_ids(), [second])

    async def test_a_row_revoked_while_the_tick_reads_the_course_is_not_emailed(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.schedule(ident)
        self.now += DAY_MS
        working = self.h.rooms.access_code

        async def revoked_meanwhile(room_id: str) -> Optional[str]:
            await self.h.handlers.revoke(
                TEACHER, {"room_id": ROOM, "invitation_id": ident}
            )
            return await working(room_id)

        self.h.rooms.access_code = revoked_meanwhile
        await self.sender.run_due()
        self.assertEqual(self.h.mailer.sent, [])
        self.assertEqual((await self.h.row(ident))["send_count"], 0)

    async def test_a_claimed_row_is_never_emailed(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.schedule(ident)
        self.h.verify(STUDENT, "student@school.example")
        status, body = await self.h.open(STUDENT, ident)
        self.assertEqual((status, body["result"]), (200, "claimed"))
        self.now += DAY_MS
        await self.sender.run_due()
        self.assertEqual(self.h.mailer.sent, [])
        self.assertIsNone((await self.h.listing())[ident]["send_at_ms"])

    async def test_a_revoked_then_readded_row_waits_for_a_new_decision(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.schedule(ident)
        await self.h.handlers.revoke(TEACHER, {"room_id": ROOM, "invitation_id": ident})
        await self.h.add(INVITED)
        self.assertIsNone((await self.h.row(ident))["send_at_ms"])
        self.now += DAY_MS
        await self.sender.run_due()
        self.assertEqual(self.h.mailer.sent, [])


class TestNoDoubleSend(_Scheduled):
    async def test_two_senders_on_one_due_row_send_once(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.schedule(ident)
        self.now += DAY_MS
        other = ScheduledInvites(
            self.h.api,
            self.h.store,
            self.h.rooms,
            self.h.mailer,
            now=lambda: self.now,
        )
        await self.sender.run_due()
        await other.run_due()
        self.assertEqual(self.sent_ids(), [ident])

    async def test_a_second_claim_after_the_first_finds_nothing(self):
        (row,) = await self.h.add(INVITED)
        await self.schedule(row["invitation_id"])
        self.now += DAY_MS
        due = await self.h.store.next_due(self.now)
        self.assertTrue(await self.h.store.claim_due(due, self.now))
        self.assertFalse(await self.h.store.claim_due(due, self.now))
        self.assertIsNone(await self.h.store.next_due(self.now))

    async def test_a_claimed_row_lost_mid_send_is_not_sent_again(self):
        # The process died after the claim: the row is not due again.
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.schedule(ident)
        self.now += DAY_MS
        due = await self.h.store.next_due(self.now)
        await self.h.store.claim_due(due, self.now)
        await self.sender.run_due()
        self.assertEqual(self.h.mailer.sent, [])

    async def test_the_tick_does_not_overlap_itself(self):
        rows = await self.h.add(INVITED, SECOND)
        await self.schedule(*[r["invitation_id"] for r in rows])
        self.now += DAY_MS
        depth: List[int] = [0]
        deepest: List[int] = [0]
        original = self.h.mailer.send_invite

        async def nested(row: Dict[str, Any], code: str) -> None:
            depth[0] += 1
            deepest[0] = max(deepest[0], depth[0])
            await self.sender.run_due()
            await original(row, code)
            depth[0] -= 1

        self.h.mailer.send_invite = nested
        await self.sender.run_due()
        self.assertEqual(deepest[0], 1)
        self.assertEqual(
            sorted(self.sent_ids()), sorted(r["invitation_id"] for r in rows)
        )

    async def test_the_ledger_records_the_send_as_the_system_once(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.schedule(ident)
        self.now += DAY_MS
        await self.sender.run_due()
        await self.sender.run_due()
        sent = [e for e in await self.h.events() if e["action"] == "invite_sent"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["actor"], "system")


class TestBounds(_Scheduled):
    async def test_a_tick_sends_a_fixed_batch_oldest_first_and_leaves_the_rest(self):
        emails = [f"s{i}@school.example" for i in range(BATCH + 3)]
        rows = await self.h.add(*emails)
        await self.schedule(*[r["invitation_id"] for r in rows[:MAX_SEND]])
        self.now += DAY_MS
        await self.sender.run_due()
        self.assertEqual(len(self.h.mailer.sent), BATCH)
        await self.sender.run_due()
        self.assertEqual(len(self.h.mailer.sent), BATCH + 3)
        self.assertEqual(
            len(set(self.sent_ids())), BATCH + 3, "one email each, none twice"
        )

    def test_the_tick_is_about_a_minute_and_the_batch_is_small(self):
        self.assertEqual(TICK_SECONDS, 60)
        self.assertLessEqual(BATCH, MAX_SEND)


class TestFailures(_Scheduled):
    async def test_a_mail_service_failure_keeps_the_row_due_and_stops_the_tick(self):
        rows = await self.h.add(INVITED, SECOND)
        ids = [r["invitation_id"] for r in rows]
        await self.schedule(*ids)
        self.now += DAY_MS
        self.h.mailer.fail_with = ConnectionRefusedError()
        await self.sender.run_due()
        for ident in ids:
            row = await self.h.row(ident)
            self.assertEqual(row["send_count"], 0)
            self.assertIsNone(row["last_sent_at_ms"])
            self.assertEqual(row["send_at_ms"], self.now)

        self.h.mailer.fail_with = None
        await self.sender.run_due()
        self.assertEqual(sorted(self.sent_ids()), sorted(ids))

    async def test_a_time_set_during_a_failed_send_stands(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.schedule(ident)
        self.now += DAY_MS
        later = self.now + DAY_MS

        async def rescheduled_then_fails(*_: Any) -> None:
            await self.schedule(ident, at=later, expected=1)
            raise ConnectionRefusedError()

        self.h.mailer.send_invite = rescheduled_then_fails
        await self.sender.run_due()
        after = await self.h.row(ident)
        self.assertEqual(after["send_at_ms"], later)
        self.assertEqual(after["send_count"], 0)

    async def test_a_try_later_answer_keeps_the_row_due(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.schedule(ident)
        self.now += DAY_MS
        self.h.mailer.fail_with = SMTPDeliveryError(451, b"try later")
        await self.sender.run_due()
        self.assertEqual((await self.h.row(ident))["send_at_ms"], self.now)

    async def test_an_uncertain_delivery_is_never_sent_again_on_its_own(self):
        for uncertain in (
            ConnectionLost(),
            SMTPTimeoutError(-1, b"timeout"),
            RuntimeError("template"),
        ):
            self.h = Harness()
            self.h.rooms.codes[ROOM] = "cl4sscd"
            self.sender = ScheduledInvites(
                self.h.api,
                self.h.store,
                self.h.rooms,
                self.h.mailer,
                now=lambda: self.now,
            )
            (row,) = await self.h.add(INVITED)
            ident = row["invitation_id"]
            await self.schedule(ident)
            self.now += DAY_MS
            self.h.mailer.fail_with = uncertain
            logs = self.capture_logs()
            await self.sender.run_due()
            self.h.mailer.fail_with = None
            await self.sender.run_due()
            self.assertEqual(self.h.mailer.sent, [], repr(uncertain))
            after = await self.h.row(ident)
            self.assertIsNone(after["send_at_ms"])
            self.assertEqual(after["send_count"], 0)
            self.assertIn(ident, logs.text())

    async def test_a_row_failing_past_the_retry_window_stops_and_logs(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.schedule(ident)
        self.now += DAY_MS + RETRY_WINDOW_MS + 1
        self.h.mailer.fail_with = ConnectionRefusedError()
        logs = self.capture_logs()
        await self.sender.run_due()
        after = await self.h.row(ident)
        self.assertIsNone(after["send_at_ms"])
        self.assertEqual(after["send_count"], 0)
        self.assertIn(ident, logs.text())

    async def test_an_address_the_mail_server_rejects_is_not_retried(self):
        rows = await self.h.add(INVITED, SECOND)
        ids = [r["invitation_id"] for r in rows]
        await self.schedule(*ids)
        self.now += DAY_MS
        rejected = SMTPDeliveryError(550, b"no such user")
        calls: List[str] = []
        original = self.h.mailer.send_invite

        async def reject_first(row: Dict[str, Any], code: str) -> None:
            calls.append(row["id"])
            if len(calls) == 1:
                raise rejected
            await original(row, code)

        self.h.mailer.send_invite = reject_first
        logs = self.capture_logs()
        await self.sender.run_due()
        await self.sender.run_due()
        self.assertEqual(len(calls), 2, "the rejected row was not tried again")
        first = await self.h.row(calls[0])
        self.assertEqual(first["send_count"], 0)
        self.assertIsNone(first["send_at_ms"])
        self.assertEqual((await self.h.row(calls[1]))["send_count"], 1)
        self.assertIn(calls[0], logs.text())
        self.assertNotIn("school.example", logs.text())

    async def test_a_course_with_no_class_code_logs_it_once_and_stops_trying(self):
        rows = await self.h.add(INVITED, SECOND, THIRD)
        ids = [r["invitation_id"] for r in rows]
        await self.schedule(*ids)
        self.now += DAY_MS
        del self.h.rooms.codes[ROOM]
        logs = self.capture_logs()
        await self.sender.run_due()
        await self.sender.run_due()
        self.assertEqual(self.h.mailer.sent, [])
        for ident in ids:
            row = await self.h.row(ident)
            self.assertEqual(row["send_count"], 0)
            self.assertIsNone(row["send_at_ms"])
        self.assertEqual(logs.text().count(ROOM), 1)
        self.assertIn("count=3", logs.text())
        self.assertNotIn("school.example", logs.text())

    async def test_a_failed_read_of_the_course_is_not_taken_for_a_lost_course(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.schedule(ident)
        self.now += DAY_MS

        async def broken(_: str) -> Optional[str]:
            raise OSError("database went away")

        working = self.h.rooms.access_code
        self.h.rooms.access_code = broken
        await self.sender.run_due()
        after = await self.h.row(ident)
        self.assertEqual(after["send_count"], 0)
        self.assertEqual(after["send_at_ms"], self.now)
        self.h.rooms.access_code = working
        await self.sender.run_due()
        self.assertEqual(self.sent_ids(), [ident])

    async def test_a_failed_ledger_write_never_resends(self):
        (row,) = await self.h.add(INVITED)
        ident = row["invitation_id"]
        await self.schedule(ident)
        self.now += DAY_MS

        async def broken(*_: Any) -> None:
            raise OSError("ledger write failed")

        self.h.store.record_sent = broken
        await self.sender.run_due()
        await self.sender.run_due()
        self.assertEqual(self.sent_ids(), [ident])
        self.assertEqual((await self.h.row(ident))["send_count"], 1)


class TestOneSender(unittest.TestCase):
    def sender(self, writer: bool) -> Any:
        h = Harness()
        h.api._hs.get_instance_name.return_value = "master" if writer else "worker1"
        h.api._hs.config.worker.writers.push_rules = ["master"]
        h.api._hs.get_clock.return_value = MagicMock()
        sender = ScheduledInvites(h.api, h.store, h.rooms, h.mailer)
        return sender, h.api._hs.get_clock.return_value

    def test_the_loop_starts_on_one_process_only(self):
        sender, clock = self.sender(writer=True)
        sender.start()
        clock.looping_call.assert_called_once()
        sender, clock = self.sender(writer=False)
        sender.start()
        clock.looping_call.assert_not_called()


class TestMigration(unittest.TestCase):
    """The additive step, against an in-memory database of the earlier
    shape; ``test_scheduled_invitations_e2e`` runs it on Postgres."""

    def test_the_column_and_due_index_are_added_in_place_and_idempotently(self):
        db = sqlite3.connect(":memory:")
        for sql in SCHEMA:
            db.execute(sql)
        db.execute("ALTER TABLE pangea_seat_invitation DROP COLUMN send_at_ms")
        db.execute(
            "INSERT INTO pangea_seat_invitation (id, course_room_id, email_key,"
            " state, source, invited_by, created_at_ms)"
            " VALUES ('a', 'r', 'k', 'invited', 'manual', 't', 1)"
        )

        class Txn:
            def __init__(self) -> None:
                self.cursor = db.cursor()

            def execute(self, sql: str, args: Any = ()) -> None:
                self.cursor.execute(sql, args)

            def fetchall(self) -> List[Any]:
                return self.cursor.fetchall()

        migrate(Txn())
        migrate(Txn())
        columns = [
            r[1] for r in db.execute("PRAGMA table_info(pangea_seat_invitation)")
        ]
        self.assertIn("send_at_ms", columns)
        self.assertEqual(
            db.execute("SELECT id, send_at_ms FROM pangea_seat_invitation").fetchall(),
            [("a", None)],
        )
        indexes = [
            r[1] for r in db.execute("PRAGMA index_list(pangea_seat_invitation)")
        ]
        self.assertIn("pangea_seat_invitation_due", indexes)
