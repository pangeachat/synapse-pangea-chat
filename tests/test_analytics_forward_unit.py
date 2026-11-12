"""The forwarder loop's delivery rules, over an in-memory store.

The store's SQL is exercised against a real homeserver in
``test_analytics_forward_e2e.py``; these tests pin what the LOOP does with it:
the cursor moves only on an acknowledged 200, a restart resumes from the
stored cursor, a profile is cleared only for the revision that was sent, and
the heartbeat reports profiles clean only once the startup re-send is in.
"""

import asyncio
import json
import os
import unittest
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from synapse_pangea_chat.analytics_forward.config import (
    AnalyticsForwardConfig,
    parse_analytics_forward_config,
)
from synapse_pangea_chat.analytics_forward.forwarder import (
    BUSY_PAUSE_SECONDS,
    MAX_BACKOFF_SECONDS,
    PROFILE_CLOCK_GRACE_MS,
    SAFETY_BATCH_MAX,
    DeliveryError,
    Forwarder,
)
from synapse_pangea_chat.analytics_forward.store import SAFETY_RECONCILE_MARGIN_MS
from synapse_pangea_chat.analytics_forward.wire import (
    BadAck,
    check_ack,
    check_safety_ack,
    incident_record,
    ingest_body,
    is_forwarded,
)
from synapse_pangea_chat.moderation.incidents import (
    Incident,
    IncidentStore,
    read_outbox,
    remark_outbox,
    settle_outbox,
)

from .moderation_doubles import DbPoolDouble, EventStoreDouble, HomeServerDouble

SECRET = "s" * 40


class _Meta:
    def __init__(self, pos: int) -> None:
        self.stream_ordering = pos


class FakeEvent:
    def __init__(
        self,
        pos: int,
        etype: str = "pangea.construct",
        state_key: Optional[str] = None,
        content: Optional[dict] = None,
        room_id: str = "!r:hs",
    ) -> None:
        self.internal_metadata = _Meta(pos)
        self.type = etype
        self._state_key = state_key
        self.content = content or {}
        self.room_id = room_id
        self.event_id = f"$e{pos}"
        self.sender = "@s:hs"
        self.origin_server_ts = 1000 + pos

    def is_state(self) -> bool:
        return self._state_key is not None

    def get_state_key(self) -> Optional[str]:
        return self._state_key


class FakeStore:
    """The ForwardStore contract, in memory, with its documented semantics."""

    def __init__(self, events: Sequence[FakeEvent], head: int) -> None:
        self.events = list(events)
        self.head_pos = head
        self.cursor: Optional[int] = None
        self.outbox: Dict[str, Dict[str, Any]] = {}
        self.profiled_users: List[str] = []
        self.profile_data: Dict[str, Any] = {}
        self.pending: List[Tuple[str, str]] = []
        self.undelivered_join = 0
        self.pending_rev = 0
        self.delivered_clean: Optional[int] = None
        #: The rooms the course filter passes; None passes every room.
        self.course_room_ids: Optional[Set[str]] = None
        self.filtered: List[Set[str]] = []
        # The safety record and its outbox on a real SQLite database, written
        # by the real IncidentStore and read by the same SQL the forwarder's
        # store runs (moderation/incidents.py).
        self.safety_pool = DbPoolDouble()
        events_store = EventStoreDouble()
        events_store.db_pool = self.safety_pool
        self.safety_hs = HomeServerDouble(events_store)
        self.incidents = IncidentStore(self.safety_hs)
        self.safety_watermark: Optional[int] = None
        self.reconcile_error: Optional[Exception] = None
        self.reconciles = 0

    def head(self) -> int:
        return self.head_pos

    async def load_cursor(self, initial: int) -> int:
        if self.cursor is None:
            self.cursor = initial
        return self.cursor

    async def read_events(self, after: int, upto: int, limit: int):
        if upto <= after:
            return [], after
        rows = [
            e
            for e in self.events
            if after < e.internal_metadata.stream_ordering <= upto
        ][:limit]
        reached = (
            rows[-1].internal_metadata.stream_ordering if len(rows) >= limit else upto
        )
        return rows, reached

    async def course_rooms(self, room_ids: Iterable[str]) -> Set[str]:
        asked = set(room_ids)
        self.filtered.append(asked)
        if self.course_room_ids is None:
            return asked
        return asked & self.course_room_ids

    async def advance(
        self,
        cursor: int,
        acked,
        clean_before: int = 0,
        *,
        incidents_accepted=(),
        incidents_parked=(),
        safety_watermark: Optional[int] = None,
    ) -> None:
        self.delivered_clean = max(self.delivered_clean or clean_before, clean_before)
        self.cursor = max(self.cursor or 0, cursor)
        for user_id, revision in acked:
            row = self.outbox[user_id]
            if row["revision"] == revision:
                row["dirty"] = False
                row["dirty_since"] = None
        await self.safety_pool.runInteraction(
            "settle",
            lambda txn: settle_outbox(txn, incidents_accepted, incidents_parked),
        )
        if safety_watermark is not None:
            self.safety_watermark = max(self.safety_watermark or 0, safety_watermark)

    async def dirty_outboxes(self, profile_limit: int, incident_limit: int):
        dirty, rest = await self.dirty_profiles(profile_limit)
        await self.incidents.ensure_table()
        entries, more = await self.safety_pool.runInteraction(
            "read", lambda txn: read_outbox(txn, incident_limit)
        )
        return dirty, rest, entries, more

    async def reconcile_incidents(self) -> None:
        self.reconciles += 1
        if self.reconcile_error is not None:
            raise self.reconcile_error
        await self.incidents.ensure_table()
        wm = self.safety_watermark
        since = None if wm is None else wm - SAFETY_RECONCILE_MARGIN_MS
        await self.safety_pool.runInteraction(
            "reconcile", lambda txn: remark_outbox(txn, since)
        )

    def outbox_row(self, incident_id: str) -> Optional[Tuple[int, bool, bool]]:
        row = self.safety_pool.connection.execute(
            "SELECT revision, dirty, parked FROM pangea_safety_incident_outbox "
            "WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
        return None if row is None else (int(row[0]), bool(row[1]), bool(row[2]))

    def write_unmarked(self, incident_id: str, updated_ms: int) -> None:
        """A write by code without the outbox (a rolled-back module)."""
        connection = self.safety_pool.connection
        updated = connection.execute(
            "UPDATE pangea_safety_incidents SET updated_ms = ? WHERE incident_id = ?",
            (updated_ms, incident_id),
        ).rowcount
        if not updated:
            connection.execute(
                "INSERT INTO pangea_safety_incidents (incident_id, source, action, "
                "room_id, course_ids, categories, self_harm, created_ms, "
                "updated_ms, text) VALUES (?, 'moderation', 'kept', '!dm:hs', "
                "'[\"!course:hs\"]', '[]', 0, ?, ?, 'unmarked text')",
                (incident_id, updated_ms, updated_ms),
            )
        connection.commit()

    def change_profile(self, user_id: str, now_ms: int) -> None:
        row = self.outbox.get(user_id)
        if row is None:
            self.outbox[user_id] = {"revision": 1, "dirty": True, "dirty_since": now_ms}
        else:
            row["revision"] += 1
            row["dirty"] = True
            if row["dirty_since"] is None:
                row["dirty_since"] = now_ms

    async def mark_all_for_resend(self) -> None:
        for row in self.outbox.values():
            row.update(revision=row["revision"] + 1, dirty=True, dirty_since=0)
        for user_id in self.profiled_users:
            self.outbox.setdefault(
                user_id, {"revision": 1, "dirty": True, "dirty_since": 0}
            )

    async def dirty_profiles(self, limit: int):
        dirty = sorted(
            (row["dirty_since"] or 0, uid, row["revision"])
            for uid, row in self.outbox.items()
            if row["dirty"]
        )
        rest = dirty[limit][0] if len(dirty) > limit else None
        return [(uid, rev) for _, uid, rev in dirty[:limit]], rest

    async def profiles(self, user_ids):
        return {u: self.profile_data[u] for u in user_ids if u in self.profile_data}

    async def pending_analytics_invites(self):
        return list(self.pending)

    async def load_pending_rev(self) -> int:
        return self.pending_rev

    async def save_pending_rev(self, rev: int) -> None:
        self.pending_rev = max(self.pending_rev, rev)

    async def undelivered_analytics_join(self, after: int) -> int:
        return self.undelivered_join if self.undelivered_join > after else 0


class FakeConsent:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []
        self.position = 0

    async def accept_if_consented(self, room_id: str, user_id: str):
        self.calls.append((room_id, user_id))
        return ("accept" if self.position else "not_invited"), self.position


class Ingest:
    """Answers like admin-dash-api: an echo of delivered_through, after commit."""

    def __init__(self) -> None:
        self.bodies: List[dict] = []
        self.fail_with: Optional[Exception] = None
        self.answer: Any = None
        self.on_post: Any = None
        #: Incident ids the ingest refuses, as the real one names a reject.
        self.reject_safety: Set[str] = set()
        #: Rejects the ingest cannot name (no usable incident_id).
        self.unnamed_safety = 0

    async def __call__(self, body: dict) -> Any:
        self.bodies.append(json.loads(json.dumps(body)))
        if self.on_post is not None:
            self.on_post(body)
        if self.fail_with is not None:
            raise self.fail_with
        if self.answer is not None:
            return self.answer
        sent = [i["incident_id"] for i in body.get("safety_incidents", [])]
        return {
            "delivered_through": body["delivered_through"],
            "rejected": 0,
            "safety_rejected": [i for i in sent if i in self.reject_safety],
            "safety_unnamed": self.unnamed_safety,
        }


class Clock:
    def __init__(self) -> None:
        self.ms = 1_000_000
        self.slept: List[float] = []

    def now(self) -> int:
        return self.ms

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.ms += int(seconds * 1000)


def _forwarder(
    store: FakeStore,
    ingest: Ingest,
    clock: Clock,
    consent: Optional[FakeConsent] = None,
    batch_max: int = 200,
) -> Forwarder:
    return Forwarder(
        store=store,
        post=ingest,
        consent=consent or FakeConsent(),
        clock_ms=clock.now,
        sleep=clock.sleep,
        run_in_background=lambda f: None,
        batch_max=batch_max,
        poll_seconds=5,
        start_position=0,
    )


def _events(*positions: int) -> List[FakeEvent]:
    return [FakeEvent(p) for p in positions]


def _delivered_positions(ingest: Ingest) -> List[int]:
    return [e["stream_pos"] for b in ingest.bodies for e in b["events"]]


class TestCourseRoomFilter(unittest.IsolatedAsyncioTestCase):
    """Only course spaces, rooms linked under one, and analytics rooms are
    forwarded; a DM's events never leave the homeserver."""

    async def test_a_dm_is_not_forwarded_a_course_room_is(self) -> None:
        dm, course = "!dm:hs", "!course:hs"
        store = FakeStore(
            [
                FakeEvent(2, "m.room.name", "", {"name": "secret"}, room_id=dm),
                FakeEvent(3, "m.room.member", "@b:hs", {"membership": "join"}, dm),
                FakeEvent(4, "m.room.name", "", {"name": "Class"}, room_id=course),
                FakeEvent(5, "m.room.member", "@b:hs", {"membership": "join"}, course),
            ],
            head=10,
        )
        store.course_room_ids = {course}
        ingest, consent = Ingest(), FakeConsent()
        fwd = _forwarder(store, ingest, Clock(), consent=consent)
        await fwd.start()
        await fwd.step()
        sent = [
            (e["room_id"], e["stream_pos"]) for b in ingest.bodies for e in b["events"]
        ]
        self.assertEqual(sent, [(course, 4), (course, 5)])
        self.assertEqual(store.filtered, [{dm, course}])
        # Delivery still reaches past the DM's events: they are never owed.
        self.assertEqual(store.cursor, 10)

    async def test_a_dm_invite_is_not_owed_a_consent_answer(self) -> None:
        dm = "!dm:hs"
        store = FakeStore(
            [FakeEvent(2, "m.room.member", "@t:hs", {"membership": "invite"}, dm)],
            head=5,
        )
        store.course_room_ids = set()
        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock())
        await fwd.start()
        await fwd.step()
        # An owed invite would be named in the batch until answered.
        self.assertEqual([b["pending_accepts"] for b in ingest.bodies], [[]])


class TestCursorOnlyOnAck(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = FakeStore(_events(3, 5, 8), head=10)
        self.ingest = Ingest()
        self.clock = Clock()
        self.fwd = _forwarder(self.store, self.ingest, self.clock)
        await self.fwd.start()

    async def _attempt(self) -> None:
        with self.assertRaises(DeliveryError):
            await self.fwd.step()
        self.assertEqual(self.store.cursor, 0)
        self.assertEqual(self.fwd.cursor, 0)

    async def test_http_error_holds_the_cursor(self) -> None:
        self.ingest.fail_with = DeliveryError("http", "HTTP 503")
        await self._attempt()

    async def test_transport_error_holds_the_cursor(self) -> None:
        self.ingest.fail_with = DeliveryError("http", "ConnectionRefusedError")
        await self._attempt()

    async def test_wrong_echo_holds_the_cursor(self) -> None:
        self.ingest.answer = {"delivered_through": 9}
        await self._attempt()

    async def test_non_object_answer_holds_the_cursor(self) -> None:
        self.ingest.answer = ["ok"]
        await self._attempt()

    async def test_acknowledged_batch_moves_the_cursor(self) -> None:
        self.assertFalse(await self.fwd.step())
        self.assertEqual(self.store.cursor, 10)
        self.assertEqual(_delivered_positions(self.ingest), [3, 5, 8])
        self.assertEqual(self.ingest.bodies[0]["delivered_through"], 10)

    async def test_retry_after_failure_resends_the_same_events(self) -> None:
        self.ingest.fail_with = DeliveryError("http", "HTTP 503")
        await self._attempt()
        self.ingest.fail_with = None
        await self.fwd.step()
        self.assertEqual(
            [e["stream_pos"] for e in self.ingest.bodies[-1]["events"]], [3, 5, 8]
        )
        self.assertEqual(self.store.cursor, 10)


class TestRestart(unittest.IsolatedAsyncioTestCase):
    async def test_restart_resumes_from_the_stored_cursor(self) -> None:
        store = FakeStore(_events(*range(1, 8)), head=7)
        clock = Clock()
        first = Ingest()
        fwd = _forwarder(store, first, clock, batch_max=3)
        await fwd.start()
        self.assertTrue(await fwd.step())  # 1..3 acknowledged
        first.fail_with = DeliveryError("http", "HTTP 503")
        with self.assertRaises(DeliveryError):
            await fwd.step()  # 4..6 sent, never acknowledged: the "crash"
        self.assertEqual(store.cursor, 3)

        second = Ingest()
        restarted = _forwarder(store, second, clock, batch_max=3)
        await restarted.start()
        self.assertEqual(restarted.cursor, 3)
        while await restarted.step():
            pass
        # Nothing at or below the cursor again; nothing above it lost.
        self.assertEqual(_delivered_positions(second), [4, 5, 6, 7])
        self.assertEqual(
            sorted(
                set(_delivered_positions(first)) | set(_delivered_positions(second))
            ),
            list(range(1, 8)),
        )
        self.assertEqual(store.cursor, 7)

    async def test_first_start_uses_the_start_position_once(self) -> None:
        store = FakeStore(_events(1, 2), head=2)
        fwd = Forwarder(
            store=store,
            post=Ingest(),
            consent=FakeConsent(),
            clock_ms=Clock().now,
            sleep=Clock().sleep,
            run_in_background=lambda f: None,
            batch_max=10,
            poll_seconds=5,
        )
        await fwd.start()
        self.assertEqual(fwd.cursor, 2)  # "current": the head
        store.head_pos = 9
        again = _forwarder(store, Ingest(), Clock())
        await again.start()
        self.assertEqual(again.cursor, 2)  # stored, not re-initialised


class TestBatchesAndHeartbeat(unittest.IsolatedAsyncioTestCase):
    async def test_full_batches_then_head(self) -> None:
        store = FakeStore(_events(2, 4, 6, 8, 9), head=12)
        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock(), batch_max=2)
        await fwd.start()
        steps = [await fwd.step() for _ in range(3)]
        self.assertEqual(steps, [True, True, False])
        self.assertEqual([b["delivered_through"] for b in ingest.bodies], [4, 8, 12])
        self.assertEqual(_delivered_positions(ingest), [2, 4, 6, 8, 9])

    async def test_idle_posts_only_a_heartbeat_every_poll(self) -> None:
        store = FakeStore([], head=0)
        ingest = Ingest()
        clock = Clock()
        fwd = _forwarder(store, ingest, clock)
        await fwd.start()
        await fwd.step()
        self.assertEqual(len(ingest.bodies), 1)  # first heartbeat at once
        store.head_pos = 40  # events of types that are not forwarded
        clock.ms += 4_000
        self.assertFalse(await fwd.step())
        self.assertEqual(len(ingest.bodies), 1)
        clock.ms += 1_000
        await fwd.step()
        self.assertEqual(len(ingest.bodies), 2)
        self.assertEqual(ingest.bodies[1]["events"], [])
        self.assertEqual(ingest.bodies[1]["delivered_through"], 40)
        self.assertEqual(store.cursor, 40)


class TestProfiles(unittest.IsolatedAsyncioTestCase):
    async def test_cleared_only_for_the_revision_sent(self) -> None:
        store = FakeStore([], head=0)
        store.profile_data = {"@u:hs": {"displayname": "One"}}
        ingest = Ingest()
        clock = Clock()
        fwd = _forwarder(store, ingest, clock)
        await fwd.start()
        await fwd.step()  # startup heartbeat, nothing dirty yet
        store.change_profile("@u:hs", clock.now())

        def change_while_in_flight(body: dict) -> None:
            store.profile_data["@u:hs"] = {"displayname": "Two"}
            store.change_profile("@u:hs", clock.now())

        ingest.on_post = change_while_in_flight
        await fwd.step()
        sent = ingest.bodies[-1]["profiles"]
        self.assertEqual([(p["user_id"], p["revision"]) for p in sent], [("@u:hs", 1)])
        self.assertTrue(store.outbox["@u:hs"]["dirty"])  # revision 2 is undelivered

        ingest.on_post = None
        await fwd.step()
        resent = ingest.bodies[-1]["profiles"]
        self.assertEqual(resent[0]["revision"], 2)
        self.assertEqual(resent[0]["profile"], {"displayname": "Two"})
        self.assertFalse(store.outbox["@u:hs"]["dirty"])

    async def test_failed_send_leaves_the_profile_dirty(self) -> None:
        store = FakeStore([], head=0)
        store.change_profile("@u:hs", 5)
        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock())
        await fwd.start()
        ingest.fail_with = DeliveryError("http", "HTTP 500")
        with self.assertRaises(DeliveryError):
            await fwd.step()
        self.assertTrue(store.outbox["@u:hs"]["dirty"])

    async def test_removed_profile_is_sent_as_none(self) -> None:
        store = FakeStore([], head=0)
        store.change_profile("@gone:hs", 5)
        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock())
        await fwd.start()
        await fwd.step()
        self.assertEqual(
            ingest.bodies[0]["profiles"],
            [{"user_id": "@gone:hs", "revision": 2, "profile": None}],
        )


class TestStartupResend(unittest.IsolatedAsyncioTestCase):
    async def test_clean_only_after_the_startup_resend(self) -> None:
        store = FakeStore([], head=0)
        # One user delivered before the restart, one profiled user never sent.
        store.outbox["@old:hs"] = {"revision": 4, "dirty": False, "dirty_since": None}
        store.profiled_users = ["@new:hs"]
        store.profile_data = {"@new:hs": {"pangea.analytics_profile": {}}}
        ingest = Ingest()
        clock = Clock()
        fwd = _forwarder(store, ingest, clock, batch_max=1)
        await fwd.start()

        barrier_time = clock.now() - 60_000  # any click before the restart
        self.assertTrue(await fwd.step())
        first = ingest.bodies[-1]
        self.assertLess(first["profiles_clean_before"], barrier_time)

        await fwd.step()
        second = ingest.bodies[-1]
        # The last re-send's own batch reports clean once it is acknowledged.
        self.assertGreater(second["profiles_clean_before"], barrier_time)
        sent = {
            p["user_id"]: p["revision"] for b in (first, second) for p in b["profiles"]
        }
        self.assertEqual(sent, {"@new:hs": 1, "@old:hs": 5})  # 5: newer than 4

        clock.ms += 5_000
        await fwd.step()  # heartbeat after both re-sends are acknowledged
        third = ingest.bodies[-1]
        self.assertEqual(third["profiles"], [])
        self.assertEqual(
            third["profiles_clean_before"], clock.now() - PROFILE_CLOCK_GRACE_MS
        )


class TestConsentFence(unittest.IsolatedAsyncioTestCase):
    """Consent joins run in their own worker. Delivery never waits on one; an
    invite is named in every batch until the worker has answered it, and a
    join's position is the fence on the batch after it."""

    INVITE = ("!a:hs", "@teacher:hs")
    OWED = [{"room_id": "!a:hs", "user_id": "@teacher:hs"}]

    def _invite(self, pos: int = 4) -> FakeEvent:
        return FakeEvent(
            pos, "m.room.member", "@teacher:hs", {"membership": "invite"}, "!a:hs"
        )

    async def test_invite_named_until_answered_then_fenced(self) -> None:
        store = FakeStore([self._invite()], head=4)
        consent = FakeConsent()
        consent.position = 7
        ingest = Ingest()
        clock = Clock()
        fwd = _forwarder(store, ingest, clock, consent=consent)
        await fwd.start()
        await fwd.step()
        self.assertEqual(store.cursor, 4)  # delivered without waiting
        self.assertEqual(ingest.bodies[-1]["pending_accepts"], self.OWED)
        self.assertIsNone(ingest.bodies[-1]["accept_join_pos"])

        await fwd.answer(*self.INVITE)
        self.assertEqual(consent.calls, [self.INVITE])
        await fwd.step()  # a fence is news: sent without waiting for the poll
        self.assertEqual(ingest.bodies[-1]["pending_accepts"], [])
        self.assertEqual(ingest.bodies[-1]["accept_join_pos"], 7)
        clock.ms += 5_000
        await fwd.step()
        self.assertIsNone(ingest.bodies[-1]["accept_join_pos"])

    async def test_fence_survives_a_failed_delivery(self) -> None:
        store = FakeStore([self._invite()], head=4)
        consent = FakeConsent()
        consent.position = 7
        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock(), consent=consent)
        await fwd.start()
        await fwd.step()
        await fwd.answer(*self.INVITE)
        ingest.fail_with = DeliveryError("http", "HTTP 503")
        with self.assertRaises(DeliveryError):
            await fwd.step()
        ingest.fail_with = None
        await fwd.step()
        self.assertEqual(ingest.bodies[-1]["accept_join_pos"], 7)

    async def test_hanging_join_never_holds_delivery(self) -> None:
        store = FakeStore([self._invite(), FakeEvent(6)], head=6)
        consent = FakeConsent()
        never: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()

        async def hangs(room_id: str, user_id: str) -> Any:
            return await never

        consent.accept_if_consented = hangs  # type: ignore[method-assign]
        ingest = Ingest()
        clock = Clock()
        fwd = _forwarder(store, ingest, clock, consent=consent)
        await fwd.start()
        # A bound, so a regression that waits on the join fails, not hangs.
        await asyncio.wait_for(fwd.step(), 2)
        worker = asyncio.ensure_future(fwd.answer(*self.INVITE))
        await asyncio.sleep(0)
        store.events.append(FakeEvent(8))
        store.head_pos = 8
        await asyncio.wait_for(fwd.step(), 2)
        self.assertEqual(store.cursor, 8)
        self.assertEqual(ingest.bodies[-1]["pending_accepts"], self.OWED)
        worker.cancel()

    async def test_hanging_join_never_holds_another_invite(self) -> None:
        other = FakeEvent(
            5, "m.room.member", "@other:hs", {"membership": "invite"}, "!b:hs"
        )
        store = FakeStore([self._invite(), other], head=5)
        never: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()

        class Consent:
            async def accept_if_consented(self, room_id: str, user_id: str) -> Any:
                if room_id == "!a:hs":
                    return await never
                return "accept", 9

        tasks: List[Any] = []
        ingest = Ingest()
        fwd = Forwarder(
            store=store,
            post=ingest,
            consent=Consent(),
            clock_ms=Clock().now,
            sleep=Clock().sleep,
            run_in_background=lambda f: (
                None if f == fwd.drain else tasks.append(asyncio.ensure_future(f()))
            ),
            batch_max=10,
            poll_seconds=5,
            start_position=0,
        )
        await fwd.start()
        await asyncio.wait_for(fwd.step(), 2)
        for _ in range(5):
            await asyncio.sleep(0)
        await asyncio.wait_for(fwd.step(), 2)
        self.assertEqual(ingest.bodies[-1]["pending_accepts"], self.OWED)
        self.assertEqual(ingest.bodies[-1]["accept_join_pos"], 9)
        for task in tasks:
            task.cancel()

    async def test_invite_seen_again_mid_proof_is_proven_again(self) -> None:
        """A replacement invite arriving while an older proof runs must not be
        released by that older proof's refusal."""
        store = FakeStore([self._invite(4)], head=4)
        first: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()
        calls: List[int] = []

        class Consent:
            async def accept_if_consented(self, room_id: str, user_id: str) -> Any:
                calls.append(1)
                if len(calls) == 1:
                    return await first
                return "accept", 9

        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock(), consent=Consent())  # type: ignore[arg-type]
        await fwd.start()
        await fwd.step()
        task = asyncio.ensure_future(fwd.answer(*self.INVITE))
        await asyncio.sleep(0)
        store.events.append(self._invite(6))  # revoked and re-sent meanwhile
        store.head_pos = 6
        await fwd.step()
        first.set_result(("inviter_or_creator_not_student", 0))
        await asyncio.wait_for(task, 2)
        self.assertEqual(len(calls), 2)
        await fwd.step()
        self.assertEqual(ingest.bodies[-1]["pending_accepts"], [])
        self.assertEqual(ingest.bodies[-1]["accept_join_pos"], 9)

    async def test_join_fence_kept_when_the_invite_is_proven_again(self) -> None:
        store = FakeStore([self._invite(4)], head=4)
        first: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()
        answers: List[Any] = []

        class Consent:
            async def accept_if_consented(self, room_id: str, user_id: str) -> Any:
                answers.append(1)
                if len(answers) == 1:
                    return await first
                return "not_invited", 0  # the teacher left meanwhile

        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock(), consent=Consent())  # type: ignore[arg-type]
        await fwd.start()
        await fwd.step()
        task = asyncio.ensure_future(fwd.answer(*self.INVITE))
        await asyncio.sleep(0)
        store.events.append(self._invite(6))
        store.head_pos = 6
        await fwd.step()
        first.set_result(("accept", 9))  # the join was made
        await asyncio.wait_for(task, 2)
        await fwd.step()
        self.assertEqual(ingest.bodies[-1]["accept_join_pos"], 9)

    async def test_failing_join_retried_once_per_poll(self) -> None:
        store = FakeStore([self._invite()], head=4)
        consent = FakeConsent()
        results: List[Any] = [ConnectionError("join failed"), ("accept", 9)]

        async def flaky(room_id: str, user_id: str) -> Any:
            consent.calls.append((room_id, user_id))
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        consent.accept_if_consented = flaky  # type: ignore[method-assign]
        clock = Clock()
        ingest = Ingest()
        fwd = _forwarder(store, ingest, clock, consent=consent)
        await fwd.start()
        await fwd.step()
        await fwd.answer(*self.INVITE)
        self.assertEqual(consent.calls, [self.INVITE, self.INVITE])
        self.assertEqual(clock.slept, [5.0])
        await fwd.step()
        self.assertEqual(ingest.bodies[-1]["pending_accepts"], [])
        self.assertEqual(ingest.bodies[-1]["accept_join_pos"], 9)

    async def test_pending_invites_are_owed_before_anything_is_sent(self) -> None:
        store = FakeStore([], head=0)
        store.pending = [self.INVITE]
        launched: List[Any] = []
        fwd = Forwarder(
            store=store,
            post=Ingest(),
            consent=FakeConsent(),
            clock_ms=Clock().now,
            sleep=Clock().sleep,
            run_in_background=launched.append,
            batch_max=10,
            poll_seconds=5,
        )
        ingest = Ingest()
        fwd._post = ingest
        await fwd.start()
        self.assertEqual(len(launched), 2)  # the invite's task, then delivery
        self.assertEqual(launched[1], fwd.drain)
        await fwd.step()
        self.assertEqual(ingest.bodies[0]["pending_accepts"], self.OWED)

    async def test_failed_startup_scan_fails_the_start(self) -> None:
        store = FakeStore([], head=0)

        async def unreadable() -> Any:
            raise ConnectionError("db down")

        store.pending_analytics_invites = unreadable  # type: ignore[method-assign]
        launched: List[Any] = []
        fwd = Forwarder(
            store=store,
            post=Ingest(),
            consent=FakeConsent(),
            clock_ms=Clock().now,
            sleep=Clock().sleep,
            run_in_background=launched.append,
            batch_max=10,
            poll_seconds=5,
        )
        with self.assertRaises(ConnectionError):
            await fwd.start()
        fwd.wake()
        self.assertIsNone(fwd.cursor)
        self.assertEqual(launched, [])  # nothing is sent until a scan succeeds

    async def test_failed_daily_scan_keeps_what_is_owed(self) -> None:
        store = FakeStore([self._invite()], head=4)
        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock())
        await fwd.start()

        async def unreadable() -> Any:
            raise ConnectionError("db down")

        store.pending_analytics_invites = unreadable  # type: ignore[method-assign]
        await fwd.daily_scan()
        await fwd.step()
        self.assertEqual(ingest.bodies[-1]["pending_accepts"], self.OWED)

    async def test_join_and_knock_are_not_owed(self) -> None:
        events = [
            FakeEvent(1, "m.room.member", "@t:hs", {"membership": "join"}),
            FakeEvent(2, "m.room.member", "@t:hs", {"membership": "knock"}),
        ]
        store = FakeStore(events, head=2)
        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock())
        await fwd.start()
        await fwd.step()
        self.assertEqual(ingest.bodies[-1]["pending_accepts"], [])

    async def test_pending_accepts_rev_grows_with_every_change(self) -> None:
        store = FakeStore([self._invite()], head=4)
        consent = FakeConsent()
        consent.position = 7
        ingest = Ingest()
        clock = Clock()
        fwd = _forwarder(store, ingest, clock, consent=consent)
        await fwd.start()
        await fwd.step()  # the invite is owed: a new set
        owed_rev = ingest.bodies[-1]["pending_accepts_rev"]
        await fwd.answer(*self.INVITE)
        await fwd.step()  # answered: a new set again
        answered_rev = ingest.bodies[-1]["pending_accepts_rev"]
        self.assertGreater(answered_rev, owed_rev)
        clock.ms += 5_000
        await fwd.step()  # nothing changed: same rev
        self.assertEqual(ingest.bodies[-1]["pending_accepts_rev"], answered_rev)

    async def test_pending_accepts_rev_outgrows_a_crash(self) -> None:
        """A rev sent but never acknowledged before a crash may still land at
        the ingest; every rev after the restart must be newer."""
        store = FakeStore([self._invite()], head=4)
        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock())
        await fwd.start()
        ingest.fail_with = DeliveryError("http", "timeout")
        with self.assertRaises(DeliveryError):
            await fwd.step()
        sent = ingest.bodies[-1]["pending_accepts_rev"]

        after = Ingest()
        restarted = _forwarder(store, after, Clock())
        await restarted.start()
        await restarted.step()
        self.assertGreater(after.bodies[-1]["pending_accepts_rev"], sent)

    async def test_restart_with_an_empty_set_still_sends_a_newer_rev(self) -> None:
        """Delivered as owed, then a crash before the answer: after the
        restart nothing is owed, and that empty set must replace the old."""
        store = FakeStore([self._invite()], head=4)
        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock())
        await fwd.start()
        await fwd.step()
        owed = ingest.bodies[-1]
        self.assertEqual(owed["pending_accepts"], self.OWED)

        after = Ingest()
        restarted = _forwarder(store, after, Clock())
        await restarted.start()
        await restarted.step()
        self.assertEqual(after.bodies[-1]["pending_accepts"], [])
        self.assertGreater(
            after.bodies[-1]["pending_accepts_rev"], owed["pending_accepts_rev"]
        )

    async def test_restart_fences_an_undelivered_analytics_join(self) -> None:
        """A join made before a crash, whose fence was never delivered, is
        fenced again from the stream on start."""
        store = FakeStore([], head=20)
        store.cursor = 10
        store.undelivered_join = 15
        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock())
        await fwd.start()
        await fwd.step()
        self.assertEqual(ingest.bodies[0]["accept_join_pos"], 15)


class TestDrain(unittest.IsolatedAsyncioTestCase):
    async def test_backoff_then_success(self) -> None:
        store = FakeStore(_events(1), head=1)
        ingest = Ingest()
        clock = Clock()
        fwd = _forwarder(store, ingest, clock)
        await fwd.start()
        failures = [DeliveryError("http", "HTTP 503")] * 3

        async def flaky(body: dict) -> Any:
            if failures:
                ingest.bodies.append(body)
                raise failures.pop()
            return await Ingest()(body)

        fwd._post = flaky
        await fwd.drain()
        self.assertEqual(clock.slept, [1.0, 2.0, 4.0, BUSY_PAUSE_SECONDS])
        self.assertEqual(store.cursor, 1)

    def test_backoff_is_capped_through_a_long_outage(self) -> None:
        fwd = _forwarder(FakeStore([], 0), Ingest(), Clock())
        delays = [fwd._backoff(DeliveryError("http", "HTTP 503")) for _ in range(2000)]
        self.assertEqual(delays[:7], [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0])
        self.assertEqual(delays[-1], MAX_BACKOFF_SECONDS)

    async def test_catch_up_is_paced(self) -> None:
        store = FakeStore(_events(1, 2, 3, 4, 5), head=5)
        clock = Clock()
        fwd = _forwarder(store, Ingest(), clock, batch_max=2)
        await fwd.start()
        await fwd.drain()
        self.assertEqual(store.cursor, 5)
        # Every read is followed by the pause, the last one included.
        self.assertEqual(clock.slept, [BUSY_PAUSE_SECONDS] * 3)

    async def test_separate_wakes_are_paced_too(self) -> None:
        store = FakeStore([], head=0)
        clock = Clock()
        fwd = _forwarder(store, Ingest(), clock)
        await fwd.start()
        for _ in range(5):
            fwd.wake()
            await fwd.drain()
        self.assertEqual(clock.slept, [BUSY_PAUSE_SECONDS] * 5)

    async def test_auth_failure_waits_the_longest(self) -> None:
        store = FakeStore(_events(1), head=1)
        ingest = Ingest()
        clock = Clock()
        fwd = _forwarder(store, ingest, clock)
        await fwd.start()
        calls = []

        async def refused_once(body: dict) -> Any:
            calls.append(body)
            if len(calls) == 1:
                raise DeliveryError("auth", "HTTP 401")
            return {"delivered_through": body["delivered_through"]}

        fwd._post = refused_once
        await fwd.drain()
        self.assertEqual(clock.slept, [MAX_BACKOFF_SECONDS, BUSY_PAUSE_SECONDS])
        self.assertEqual(store.cursor, 1)

    async def test_wake_before_start_is_ignored(self) -> None:
        launched: List[Any] = []
        fwd = Forwarder(
            store=FakeStore([], 0),
            post=Ingest(),
            consent=FakeConsent(),
            clock_ms=Clock().now,
            sleep=Clock().sleep,
            run_in_background=launched.append,
            batch_max=1,
            poll_seconds=5,
        )
        fwd.wake()
        self.assertEqual(launched, [])


class TestWire(unittest.TestCase):
    def test_check_ack(self) -> None:
        self.assertEqual(check_ack({"delivered_through": 5}, 5), 0)
        self.assertEqual(check_ack({"delivered_through": 5, "rejected": 2}, 5), 2)
        answers: List[Any] = [
            None,
            [],
            {"delivered_through": 4},
            {"delivered_through": True},
        ]
        for answer in answers:
            with self.assertRaises(BadAck):
                check_ack(answer, 5)
        with self.assertRaises(BadAck):
            check_ack({"delivered_through": 5, "rejected": -1}, 5)

    def test_is_forwarded(self) -> None:
        self.assertTrue(is_forwarded("pangea.construct", None))
        self.assertFalse(is_forwarded("pangea.construct", ""))
        self.assertTrue(is_forwarded("m.room.member", "@u:hs"))
        self.assertFalse(is_forwarded("m.room.member", None))
        self.assertFalse(is_forwarded("m.room.message", None))

    def test_forwarded_types_match_the_contract(self) -> None:
        from synapse_pangea_chat.analytics_forward.wire import (
            CONSTRUCT_TYPE,
            STATE_TYPES,
        )

        path = os.path.join(
            os.path.dirname(__file__),
            "fixtures",
            "analytics_forward",
            "ingest_contract.json",
        )
        with open(path, encoding="utf-8") as f:
            types = json.load(f)["forwarded_types"]
        self.assertEqual(types["timeline"], [CONSTRUCT_TYPE])
        self.assertEqual(types["state"], sorted(STATE_TYPES))
        snapshot = json.load(open(path, encoding="utf-8"))["analytics_state_endpoint"]
        self.assertEqual(
            snapshot["response_example"]["forwarded_types"], sorted(STATE_TYPES)
        )
        # The Announcements chat is marked by its alias.
        self.assertTrue(is_forwarded("m.room.canonical_alias", ""))
        # A student's progress in a course, keyed by the course room id.
        self.assertTrue(is_forwarded("pangea.course_progress", "!course:hs"))
        self.assertFalse(is_forwarded("pangea.course_progress", None))

    def _contract(self) -> Dict[str, Any]:
        path = os.path.join(
            os.path.dirname(__file__),
            "fixtures",
            "analytics_forward",
            "ingest_contract.json",
        )
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_contract_fixture_matches_the_body(self) -> None:
        contract = self._contract()
        request_schema = contract["request_schema"]
        example = contract["request_example"]
        body = ingest_body(
            events=example["events"],
            profiles=example["profiles"],
            delivered_through=example["delivered_through"],
            profiles_clean_before=example["profiles_clean_before"],
            accept_join_pos=example["accept_join_pos"],
            pending_accepts=example["pending_accepts"],
            pending_accepts_rev=example["pending_accepts_rev"],
        )
        self.assertEqual(body, example)
        self.assertEqual(set(body), set(request_schema["required"]))
        self.assertLessEqual(set(body), set(request_schema["properties"]))
        event_schema = request_schema["properties"]["events"]["items"]
        for event in example["events"]:
            self.assertEqual(set(event), set(event_schema["required"]))
        profile_schema = request_schema["properties"]["profiles"]["items"]
        for profile in example["profiles"]:
            self.assertEqual(set(profile), set(profile_schema["required"]))
        answer = contract["response_example"]
        self.assertEqual(
            check_ack(answer, example["delivered_through"]), answer["rejected"]
        )

    def test_safety_contract_round_trips(self) -> None:
        """admin-dash-api's safety example (admin-dash#300) is exactly what the
        forwarder builds from the incident it describes."""
        contract = self._contract()
        schema = contract["request_schema"]
        example = contract["safety_request_example"]
        (wire_incident,) = example["safety_incidents"]
        fields = {
            k: v
            for k, v in wire_incident.items()
            if k not in ("course_ids", "revision")
        }
        fields["categories"] = tuple(fields["categories"])
        incident = Incident(course_ids=tuple(wire_incident["course_ids"]), **fields)
        body = ingest_body(
            events=example["events"],
            profiles=example["profiles"],
            delivered_through=example["delivered_through"],
            profiles_clean_before=example["profiles_clean_before"],
            accept_join_pos=example["accept_join_pos"],
            pending_accepts=example["pending_accepts"],
            pending_accepts_rev=example["pending_accepts_rev"],
            safety_incidents=[incident_record(incident, wire_incident["revision"])],
            safety_order=(example["safety_order"][0], example["safety_order"][1]),
            safety_backlog=example["safety_backlog"],
        )
        self.assertEqual(body, example)
        self.assertEqual(set(body), set(schema["properties"]))
        item_schema = schema["properties"]["safety_incidents"]["items"]
        self.assertLessEqual(set(item_schema["required"]), set(wire_incident))
        self.assertEqual(set(wire_incident), set(item_schema["properties"]))
        self.assertGreaterEqual(
            schema["properties"]["safety_incidents"]["maxItems"], SAFETY_BATCH_MAX
        )
        answer = contract["safety_response_example"]
        self.assertEqual(check_ack(answer, example["delivered_through"]), 0)
        self.assertEqual(
            check_safety_ack(answer, [wire_incident["incident_id"]]), set()
        )

    def test_check_safety_ack(self) -> None:
        both = {"safety_rejected": ["a", "z"], "safety_unnamed": 0}
        # Named rejects park; ids the batch did not carry are ignored.
        self.assertEqual(check_safety_ack(both, ["a", "b"]), {"a"})
        # An unnamed reject could be any of them: every sent incident parks.
        self.assertEqual(
            check_safety_ack({"safety_rejected": [], "safety_unnamed": 1}, ["a", "b"]),
            {"a", "b"},
        )
        # No incidents sent: an ingest that names none is fine.
        self.assertEqual(check_safety_ack({"delivered_through": 1}, []), set())
        bad: List[Any] = [
            {"delivered_through": 1},  # incidents sent, none accounted for
            {"safety_rejected": []},  # safety_unnamed missing
            {"safety_unnamed": 0},  # safety_rejected missing
            {"safety_rejected": "a", "safety_unnamed": 0},
            {"safety_rejected": [1], "safety_unnamed": 0},
            {"safety_rejected": [], "safety_unnamed": -1},
            {"safety_rejected": [], "safety_unnamed": True},
            {"safety_rejected": [], "safety_unnamed": "0"},
            None,
        ]
        for answer in bad:
            with self.assertRaises(BadAck, msg=answer):
                check_safety_ack(answer, ["a"])
        # A present but malformed field is refused even with nothing sent.
        with self.assertRaises(BadAck):
            check_safety_ack({"safety_rejected": [], "safety_unnamed": -1}, [])

    def test_a_non_ascii_id_is_echoed_back_exactly(self) -> None:
        """The answer is ASCII-escaped JSON; the parsed id is the one sent."""
        sent = "report:\u00e9t\u00e9-\U0001f600"
        answer = json.loads(
            json.dumps(
                {"safety_rejected": [sent], "safety_unnamed": 0}, ensure_ascii=True
            )
        )
        self.assertEqual(check_safety_ack(answer, [sent]), {sent})


class TestConfig(unittest.TestCase):
    def test_not_configured(self) -> None:
        self.assertIsNone(parse_analytics_forward_config(None))
        self.assertIsNone(parse_analytics_forward_config({}))

    def test_configured(self) -> None:
        parsed = parse_analytics_forward_config(
            {"url": "https://api.example/x", "secret": SECRET}
        )
        self.assertEqual(
            parsed,
            AnalyticsForwardConfig(url="https://api.example/x", secret=SECRET),
        )
        self.assertNotIn(SECRET, repr(parsed))

    def test_rejections(self) -> None:
        base = {"url": "https://api.example/x", "secret": SECRET}
        bad: List[Any] = [
            "x",
            {"secret": SECRET},
            {"url": "https://api.example/x"},
            {**base, "enabled": True},
            {**base, "url": "ftp://api.example/x"},
            {**base, "url": "https://user:pw@api.example/x"},
            # The secret never travels in plaintext beyond this machine.
            {**base, "url": "http://api.example/x"},
            {**base, "url": "http://10.0.0.5:8000/x"},
            {**base, "secret": "short"},
            {**base, "batch_max": 0},
            {**base, "batch_max": True},
            {**base, "poll_seconds": 61},
            {**base, "start_position": -1},
            {**base, "start_position": "latest"},
            {**base, "unknown": 1},
        ]
        for raw in bad:
            with self.assertRaises(ValueError, msg=repr(raw)):
                parse_analytics_forward_config(raw)

    def test_plain_http_only_to_this_machine(self) -> None:
        for url in (
            "http://127.0.0.1:8000/x",
            "http://localhost:8000/x",
            "http://[::1]:8000/x",
        ):
            parsed = parse_analytics_forward_config({"url": url, "secret": SECRET})
            assert parsed is not None, url
            self.assertEqual(parsed.url, url)

    def test_start_position(self) -> None:
        parsed = parse_analytics_forward_config(
            {
                "url": "http://127.0.0.1:1/x",
                "secret": SECRET,
                "start_position": 1234,
            }
        )
        assert parsed is not None
        self.assertEqual(parsed.start_position, 1234)


if __name__ == "__main__":
    unittest.main()


class _Transport:
    def __init__(self) -> None:
        self.aborted = False

    def stopProducing(self) -> None:  # noqa: N802
        pass

    def abortConnection(self) -> None:  # noqa: N802
        self.aborted = True


class _Response:
    """An HTTP response that delivers ``body`` (or never ends, when None)."""

    phrase = b"OK"

    def __init__(self, code: int, body: Optional[bytes]) -> None:
        self.code = code
        self.length = len(body) if body is not None else 10
        self._body = body
        self.transport = _Transport()

    def deliverBody(self, protocol: Any) -> None:  # noqa: N802
        from twisted.python.failure import Failure
        from twisted.web.client import ResponseDone

        protocol.makeConnection(self.transport)
        if self._body is None:
            return  # the body never ends
        protocol.dataReceived(self._body)
        protocol.connectionLost(Failure(ResponseDone()))


class _Agent:
    def __init__(self, outcomes: List[Any]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def request(self, method: bytes, uri: bytes, headers: Any, body: Any) -> Any:
        from twisted.internet import defer

        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            return defer.fail(outcome)
        return defer.succeed(outcome)


class TestIngestTransport(unittest.TestCase):
    """The POST to the ingest: its own connection each time, a stale one
    retried at once, and a hard cap on the whole exchange."""

    def _post(self, agent: Any) -> Any:
        from synapse.util.clock import Clock as SynapseClock
        from twisted.internet.testing import MemoryReactorClock

        from synapse_pangea_chat.analytics_forward.forwarder import make_http_poster

        reactor = MemoryReactorClock()
        post = make_http_poster(
            agent, "http://ingest/x", SECRET, SynapseClock(reactor, "hs")  # type: ignore[arg-type]
        )
        return reactor, post

    def _run(self, post: Any) -> Tuple[List[Any], List[Any]]:
        from twisted.internet import defer

        async def call() -> Any:
            return await post({"delivered_through": 1})

        results: List[Any] = []
        failures: List[Any] = []
        result: "defer.Deferred[Any]" = defer.ensureDeferred(call())
        result.addCallbacks(results.append, failures.append)
        return results, failures

    def test_a_connection_lost_under_the_request_is_retried_at_once(self) -> None:
        from twisted.web._newclient import ResponseNeverReceived

        agent = _Agent(
            [ResponseNeverReceived([]), _Response(200, b'{"delivered_through": 1}')]
        )
        _, post = self._post(agent)
        results, failures = self._run(post)
        self.assertEqual(failures, [])
        self.assertEqual(results, [{"delivered_through": 1}])
        self.assertEqual(agent.calls, 2)

    def test_only_one_immediate_retry(self) -> None:
        agent = _Agent([ConnectionRefusedError(), ConnectionRefusedError()])
        _, post = self._post(agent)
        _, failures = self._run(post)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0].value, DeliveryError)
        self.assertEqual(agent.calls, 2)

    def test_an_http_error_is_not_retried_here(self) -> None:
        agent = _Agent([_Response(503, b"{}")])
        _, post = self._post(agent)
        _, failures = self._run(post)
        self.assertEqual(agent.calls, 1)
        self.assertEqual(failures[0].value.reason, "http")

    def test_an_oversized_answer_is_refused_unread(self) -> None:
        from synapse_pangea_chat.analytics_forward.forwarder import MAX_ANSWER_BYTES

        response = _Response(200, b" " * (MAX_ANSWER_BYTES + 1))
        agent = _Agent([response])
        _, post = self._post(agent)
        _, failures = self._run(post)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].value.reason, "bad_ack")
        self.assertEqual(agent.calls, 1)  # an answer came back: not re-sent
        self.assertTrue(response.transport.aborted)

    def test_an_answer_at_the_cap_is_read(self) -> None:
        from synapse_pangea_chat.analytics_forward.forwarder import MAX_ANSWER_BYTES

        answer = b'{"delivered_through": 1}'
        padded = answer + b" " * (MAX_ANSWER_BYTES - len(answer))
        _, post = self._post(_Agent([_Response(200, padded)]))
        results, failures = self._run(post)
        self.assertEqual(failures, [])
        self.assertEqual(results, [{"delivered_through": 1}])

    def test_a_stalled_body_times_out(self) -> None:
        from synapse_pangea_chat.analytics_forward.forwarder import (
            REQUEST_TIMEOUT_SECONDS,
        )

        stalled = [_Response(200, None), _Response(200, None)]
        reactor, post = self._post(_Agent(list(stalled)))
        _, failures = self._run(post)
        reactor.advance(REQUEST_TIMEOUT_SECONDS / 2 + 0.5)
        # The stalled body's connection is closed at its bound, never left open.
        self.assertTrue(stalled[0].transport.aborted)
        reactor.advance(REQUEST_TIMEOUT_SECONDS / 2 - 1)
        self.assertEqual(failures, [])  # the re-send is still within its bound
        reactor.advance(1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0].value, DeliveryError)
        self.assertTrue(stalled[1].transport.aborted)

    def test_the_cap_cancels_everything_and_starts_nothing_more(self) -> None:
        """Each attempt is bounded and cancelled at its bound; the re-send is
        the last, so nothing is left open, nothing keeps a timer.

        A cancelled real request fails with ResponseNeverReceived wrapping
        CancelledError, not a bare CancelledError; the fake does the same."""
        from twisted.internet import defer
        from twisted.python.failure import Failure
        from twisted.web._newclient import ResponseNeverReceived

        from synapse_pangea_chat.analytics_forward.forwarder import (
            REQUEST_TIMEOUT_SECONDS,
        )

        cancelled: List[int] = []

        def like_twisted(d: "defer.Deferred[Any]") -> None:
            cancelled.append(1)
            d.errback(ResponseNeverReceived([Failure(defer.CancelledError())]))

        class Hanging:
            calls = 0

            def request(self, *args: Any) -> Any:
                self.calls += 1
                return defer.Deferred(like_twisted)

        agent = Hanging()
        reactor, post = self._post(agent)
        _, failures = self._run(post)
        reactor.advance(REQUEST_TIMEOUT_SECONDS / 2 + 0.5)  # the first is cut
        self.assertEqual(failures, [])
        reactor.advance(REQUEST_TIMEOUT_SECONDS / 2)  # and the re-send
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0].value, DeliveryError)
        self.assertEqual(agent.calls, 2)  # one re-send, then nothing more
        self.assertEqual(cancelled, [1, 1])
        self.assertEqual(reactor.getDelayedCalls(), [])
        reactor.advance(REQUEST_TIMEOUT_SECONDS * 3)
        self.assertEqual(agent.calls, 2)

    def test_the_resend_is_bounded_by_the_same_cap(self) -> None:
        from twisted.internet import defer
        from twisted.web._newclient import ResponseNeverReceived

        from synapse_pangea_chat.analytics_forward.forwarder import (
            REQUEST_TIMEOUT_SECONDS,
        )

        cancelled: List[int] = []

        class FailsThenHangs:
            calls = 0

            def request(self, *args: Any) -> Any:
                self.calls += 1
                if self.calls == 1:
                    return defer.fail(ResponseNeverReceived([]))
                return defer.Deferred(lambda d: cancelled.append(1))

        agent = FailsThenHangs()
        reactor, post = self._post(agent)
        _, failures = self._run(post)
        reactor.advance(REQUEST_TIMEOUT_SECONDS / 2 + 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(agent.calls, 2)
        self.assertEqual(cancelled, [1])
        self.assertEqual(reactor.getDelayedCalls(), [])

    def test_the_agent_keeps_no_idle_connection(self) -> None:
        from twisted.internet.testing import MemoryReactorClock

        from synapse_pangea_chat.analytics_forward.forwarder import (
            CONNECT_TIMEOUT_SECONDS,
            make_ingest_agent,
        )

        agent = make_ingest_agent(MemoryReactorClock())
        self.assertFalse(agent._pool.persistent)
        self.assertLessEqual(CONNECT_TIMEOUT_SECONDS, 10)
        self.assertEqual(
            agent._endpointFactory._connectTimeout, CONNECT_TIMEOUT_SECONDS
        )


class TestRefreshBarrier(unittest.IsolatedAsyncioTestCase):
    """What the head endpoint asks a Refresh to wait for: only what is not
    delivered yet, so an idle forwarder costs the Refresh no wait at all."""

    def test_nothing_dirty_is_already_met(self) -> None:
        from synapse_pangea_chat.analytics_forward.forwarder import refresh_barrier

        self.assertEqual(refresh_barrier(42, False, 5_000, 9_000), (42, 5_000))

    def test_nothing_delivered_yet_waits_for_a_batch(self) -> None:
        from synapse_pangea_chat.analytics_forward.forwarder import refresh_barrier

        self.assertEqual(
            refresh_barrier(42, False, None, 9_000),
            (42, 9_000 - PROFILE_CLOCK_GRACE_MS),
        )

    async def _met(self, ingest: Ingest, barrier_time: int) -> bool:
        return max(b["profiles_clean_before"] for b in ingest.bodies) >= barrier_time

    async def test_every_dirty_profile_delivered_before_it_is_met(self) -> None:
        from synapse_pangea_chat.analytics_forward.forwarder import refresh_barrier

        store = FakeStore([], head=0)
        clock = Clock()
        ingest = Ingest()
        fwd = _forwarder(store, ingest, clock, batch_max=1)
        await fwd.start()
        await fwd.step()
        clock.ms += 1  # the changes come after that heartbeat
        store.change_profile("@a:hs", clock.now())
        store.change_profile("@b:hs", clock.now())
        _, barrier_time = refresh_barrier(0, True, None, clock.now())
        clock.ms += 10
        await fwd.step()  # @a only
        self.assertFalse(await self._met(ingest, barrier_time))
        clock.ms += 10
        await fwd.step()  # @b: now everything dirty at the read is delivered
        self.assertTrue(await self._met(ingest, barrier_time))

    async def test_a_delivery_in_the_same_millisecond_meets_it(self) -> None:
        from synapse_pangea_chat.analytics_forward.forwarder import refresh_barrier

        store = FakeStore([], head=0)
        clock = Clock()
        ingest = Ingest()
        fwd = _forwarder(store, ingest, clock)
        await fwd.start()
        await fwd.step()
        clock.ms += 1
        store.change_profile("@a:hs", clock.now())
        _, barrier_time = refresh_barrier(0, True, None, clock.now())
        await fwd.step()  # the same millisecond as the head read
        self.assertTrue(await self._met(ingest, barrier_time))

    async def test_a_head_read_during_a_delivery_gets_its_own_batch(self) -> None:
        """A head that saw a change while its batch was in flight is met by
        the next batch, even when that one has nothing left to carry."""
        from synapse_pangea_chat.analytics_forward.forwarder import refresh_barrier

        store = FakeStore([], head=0)
        clock = Clock()
        ingest = Ingest()
        fwd = _forwarder(store, ingest, clock)
        await fwd.start()
        await fwd.step()
        clock.ms += 1
        store.change_profile("@a:hs", clock.now())
        barrier: List[int] = []

        def head_read_mid_flight(body: dict) -> None:
            clock.ms += 10
            barrier.append(refresh_barrier(0, True, None, clock.now())[1])
            fwd.wake(send=True)

        ingest.on_post = head_read_mid_flight
        await fwd.step()  # delivers @a; the head read happens in flight
        ingest.on_post = None
        clock.ms += 1
        await fwd.step()  # nothing dirty, no heartbeat due: still sent
        self.assertTrue(await self._met(ingest, barrier[0]))

    async def test_a_requested_send_survives_a_failed_delivery(self) -> None:
        store = FakeStore([], head=0)
        clock = Clock()
        ingest = Ingest()
        fwd = _forwarder(store, ingest, clock)
        await fwd.start()
        await fwd.step()
        fwd.wake(send=True)
        ingest.fail_with = DeliveryError("http", "HTTP 503")
        with self.assertRaises(DeliveryError):
            await fwd.step()
        ingest.fail_with = None
        sent = len(ingest.bodies)
        await fwd.step()  # no heartbeat due, nothing to carry: still sent
        self.assertEqual(len(ingest.bodies), sent + 1)

    async def test_head_requests_a_send_only_when_something_is_undelivered(
        self,
    ) -> None:
        from synapse_pangea_chat.analytics_forward.resources import ForwardHead

        class Store:
            dirty = True

            def head(self) -> int:
                return 10

            async def forwarded_head(self, upto: int) -> int:
                return 8

            async def any_dirty(self) -> bool:
                return self.dirty

            async def delivered_clean_before(self) -> Optional[int]:
                return 5

        store = Store()
        wakes: List[bool] = []
        head = ForwardHead(SECRET, store, lambda: 1_000, wakes.append)
        status, body = await head._head(None)  # type: ignore[arg-type]
        self.assertEqual(status, 200)
        self.assertEqual(body["barrier_pos"], 8)
        store.dirty = False
        await head._head(None)  # type: ignore[arg-type]
        self.assertEqual(wakes, [True, False])

    async def test_a_restart_waits_for_the_resend(self) -> None:
        """A value acknowledged before the restart must not meet a barrier
        read while the startup re-send is undelivered."""
        from synapse_pangea_chat.analytics_forward.forwarder import refresh_barrier

        store = FakeStore([], head=0)
        store.profiled_users = ["@u:hs"]
        clock = Clock()
        before_restart = clock.now() - 60_000 - PROFILE_CLOCK_GRACE_MS
        ingest = Ingest()
        fwd = _forwarder(store, ingest, clock)
        await fwd.start()  # marks @u dirty since always
        _, barrier_time = refresh_barrier(0, True, before_restart, clock.now())
        self.assertGreater(barrier_time, before_restart)
        clock.ms += 10
        await fwd.step()
        self.assertTrue(await self._met(ingest, barrier_time))


class TestCleanBeforeCountsTheBatch(unittest.IsolatedAsyncioTestCase):
    async def test_a_batch_delivering_the_only_change_reports_clean(self) -> None:
        """Once acknowledged, the batch's own profiles are delivered, so its
        clean_before need not wait for the next batch (a Refresh would)."""
        store = FakeStore([], head=0)
        clock = Clock()
        ingest = Ingest()
        fwd = _forwarder(store, ingest, clock)
        await fwd.start()
        await fwd.step()
        store.change_profile("@u:hs", clock.now() - 10)
        clock.ms += 5_000
        await fwd.step()
        body = ingest.bodies[-1]
        self.assertEqual([p["user_id"] for p in body["profiles"]], ["@u:hs"])
        self.assertEqual(
            body["profiles_clean_before"], clock.now() - PROFILE_CLOCK_GRACE_MS
        )

    async def test_an_undelivered_change_still_holds_it_back(self) -> None:
        store = FakeStore([], head=0)
        clock = Clock()
        ingest = Ingest()
        fwd = _forwarder(store, ingest, clock, batch_max=1)
        await fwd.start()
        await fwd.step()
        store.change_profile("@a:hs", 100)
        store.change_profile("@b:hs", 200)
        await fwd.step()  # sends @a only
        self.assertEqual(
            ingest.bodies[-1]["profiles_clean_before"], 199 - PROFILE_CLOCK_GRACE_MS
        )


class TestDeliveredCleanIsStored(unittest.IsolatedAsyncioTestCase):
    async def test_ack_stores_profiles_clean_before(self) -> None:
        store = FakeStore([], head=0)
        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock())
        await fwd.start()
        await fwd.step()
        self.assertEqual(
            store.delivered_clean, ingest.bodies[-1]["profiles_clean_before"]
        )
        ingest.fail_with = DeliveryError("http", "HTTP 503")
        before = store.delivered_clean
        fwd._last_post_ms = None
        with self.assertRaises(DeliveryError):
            await fwd.step()
        self.assertEqual(store.delivered_clean, before)  # only on an ack


class TestSnapshotDeclaresForwardedTypes(unittest.IsolatedAsyncioTestCase):
    """Every analytics_state answer names the state types this module
    forwards, so the API trusts an absent type (no Announcements chat) only
    from a module that forwards it."""

    async def _snapshot(self, body: dict) -> Any:
        from unittest import mock

        from synapse_pangea_chat.analytics_forward.resources import AnalyticsState

        class Store:
            async def course_rooms(self, room_ids: Any) -> Set[str]:
                return set(room_ids)

            async def bump_revisions(self, user_ids: Any) -> Dict[str, int]:
                return {u: 1 for u in user_ids}

            async def profiles(self, user_ids: Any) -> Dict[str, Any]:
                return {}

        class State:
            async def get_current_state(self, room_id: str, state_filter: Any) -> Any:
                return {}

        resource = AnalyticsState(SECRET, Store(), State())
        with mock.patch(
            "synapse_pangea_chat.analytics_forward.resources.extract_body_json",
            new=mock.AsyncMock(return_value=body),
        ):
            return await resource._snapshot(None)  # type: ignore[arg-type]

    async def test_declared_on_every_answer(self) -> None:
        from synapse_pangea_chat.analytics_forward.wire import STATE_TYPES

        bodies: List[Dict[str, Any]] = [
            {"room_ids": ["!r:hs"], "user_ids": []},
            {"room_ids": [], "user_ids": ["@u:hs"]},  # a profile-only round
            {},
        ]
        for body in bodies:
            status, answer = await self._snapshot(body)
            self.assertEqual(status, 200)
            self.assertEqual(answer["forwarded_types"], sorted(STATE_TYPES))
            self.assertIn("m.room.canonical_alias", answer["forwarded_types"])


# ---------------------------------------------------------------------------
# The safety push (admin-dash#300)
# ---------------------------------------------------------------------------

COURSE = "!course:hs"
DM = "!dm:hs"
SAFETY_KEYS = {"safety_incidents", "safety_backlog", "safety_order"}


def _safety_incident(incident_id: str = "mod:$dm", **overrides: Any) -> Incident:
    fields: Dict[str, Any] = {
        "incident_id": incident_id,
        "source": "moderation",
        "action": "kept",
        "outcome": None,
        "subject_id": "@learner:hs",
        "reporter_id": None,
        "room_id": DM,
        "event_id": "$dm",
        "course_ids": (COURSE,),
        "categories": ("harassment",),
        "self_harm": False,
        "rule": None,
        "top_score": 0.4,
        "text": "a direct message",
        "reason": None,
        "created_ms": 5_000,
        "updated_ms": 5_000,
    }
    fields.update(overrides)
    return Incident(**fields)


def _sent_incidents(body: dict) -> List[str]:
    return [i["incident_id"] for i in body.get("safety_incidents", [])]


class TestSafetyPush(unittest.IsolatedAsyncioTestCase):
    async def _started(
        self, store: Optional[FakeStore] = None
    ) -> Tuple[FakeStore, Ingest, Clock, Forwarder]:
        store = store or FakeStore([], head=0)
        ingest = Ingest()
        clock = Clock()
        fwd = _forwarder(store, ingest, clock)
        await fwd.start()
        return store, ingest, clock, fwd

    async def test_a_dm_incident_rides_the_next_batch_whole(self) -> None:
        store, ingest, _clock, fwd = await self._started()
        await store.incidents.insert(_safety_incident())
        await fwd.step()
        body = ingest.bodies[-1]
        (record,) = body["safety_incidents"]
        self.assertEqual(record["text"], "a direct message")
        self.assertEqual(record["room_id"], DM)
        self.assertEqual(record["course_ids"], [COURSE])
        self.assertEqual(record["revision"], 1)
        self.assertEqual(store.outbox_row("mod:$dm"), (1, False, False))
        self.assertEqual(store.safety_watermark, 5_000)
        _clock.ms += fwd.poll_ms
        await fwd.step()  # the next heartbeat: nothing left to carry
        self.assertEqual(len(ingest.bodies), 2)
        self.assertEqual(_sent_incidents(ingest.bodies[-1]), [])

    async def test_a_failed_delivery_keeps_the_mark(self) -> None:
        store, ingest, _clock, fwd = await self._started()
        await store.incidents.insert(_safety_incident())
        ingest.fail_with = DeliveryError("http", "HTTP 500")
        with self.assertRaises(DeliveryError):
            await fwd.step()
        self.assertEqual(store.outbox_row("mod:$dm"), (1, True, False))
        self.assertIsNone(store.safety_watermark)
        ingest.fail_with = None
        await fwd.step()
        self.assertEqual(_sent_incidents(ingest.bodies[-1]), ["mod:$dm"])
        self.assertEqual(store.outbox_row("mod:$dm"), (1, False, False))

    async def test_an_ack_that_does_not_account_for_incidents_moves_nothing(
        self,
    ) -> None:
        store = FakeStore(_events(3), head=3)
        store, ingest, _clock, fwd = await self._started(store)
        await store.incidents.insert(_safety_incident())
        ingest.answer = {"delivered_through": 3, "rejected": 0}
        with self.assertRaises(DeliveryError) as raised:
            await fwd.step()
        self.assertEqual(raised.exception.reason, "bad_ack")
        self.assertEqual(store.cursor, 0)
        self.assertEqual(store.outbox_row("mod:$dm"), (1, True, False))

    async def test_a_write_during_the_send_is_sent_again(self) -> None:
        store, ingest, _clock, fwd = await self._started()
        await store.incidents.insert(_safety_incident())
        writes: List[Any] = []

        def verdict_in_flight(body: dict) -> None:
            if body.get("safety_incidents"):
                writes.append(
                    store.incidents.upsert_verdict(
                        _safety_incident(action="redacted", top_score=0.9)
                    )
                )

        ingest.on_post = verdict_in_flight
        await fwd.step()
        ingest.on_post = None
        # The in-flight write lands before the ack is settled.
        await asyncio.gather(*writes)
        self.assertEqual(store.outbox_row("mod:$dm"), (2, True, False))
        await fwd.step()
        (record,) = ingest.bodies[-1]["safety_incidents"]
        self.assertEqual((record["revision"], record["action"]), (2, "redacted"))
        self.assertEqual(store.outbox_row("mod:$dm"), (2, False, False))

    async def test_a_refused_incident_is_parked_until_reconciliation(self) -> None:
        store, ingest, _clock, fwd = await self._started()
        await store.incidents.insert(_safety_incident("mod:$ok", updated_ms=6_000))
        await store.incidents.insert(_safety_incident("mod:$bad", updated_ms=9_000))
        ingest.reject_safety = {"mod:$bad"}
        await fwd.step()
        self.assertEqual(
            sorted(_sent_incidents(ingest.bodies[-1])), ["mod:$bad", "mod:$ok"]
        )
        self.assertEqual(store.outbox_row("mod:$ok"), (1, False, False))
        self.assertEqual(store.outbox_row("mod:$bad"), (1, False, True))
        # The watermark counts only what was stored.
        self.assertEqual(store.safety_watermark, 6_000)
        self.assertFalse(ingest.bodies[-1]["safety_backlog"])

        posted = len(ingest.bodies)
        _clock.ms += fwd.poll_ms
        await fwd.step()  # not re-sent every step
        self.assertEqual(len(ingest.bodies), posted + 1)
        self.assertEqual(_sent_incidents(ingest.bodies[-1]), [])

        ingest.reject_safety = set()
        await fwd.daily_scan()
        await fwd.step()
        # The parked one goes again (the stored one too, being inside the
        # margin below the watermark; a re-send is a no-op at the ingest).
        self.assertIn("mod:$bad", _sent_incidents(ingest.bodies[-1]))
        self.assertEqual(store.outbox_row("mod:$bad"), (2, False, False))
        self.assertEqual(store.safety_watermark, 9_000)

    async def test_an_unnamed_reject_parks_the_whole_batch(self) -> None:
        store, ingest, _clock, fwd = await self._started()
        await store.incidents.insert(_safety_incident("mod:$a", updated_ms=6_000))
        await store.incidents.insert(_safety_incident("mod:$b", updated_ms=7_000))
        ingest.unnamed_safety = 1
        await fwd.step()
        self.assertEqual(store.outbox_row("mod:$a"), (1, False, True))
        self.assertEqual(store.outbox_row("mod:$b"), (1, False, True))
        self.assertIsNone(store.safety_watermark, "a parked incident moved it")
        ingest.unnamed_safety = 0
        await fwd.daily_scan()
        await fwd.step()
        self.assertEqual(store.outbox_row("mod:$a"), (2, False, False))
        self.assertEqual(store.outbox_row("mod:$b"), (2, False, False))
        self.assertEqual(store.safety_watermark, 7_000)

    async def test_a_parked_incident_is_resent_as_it_is_now(self) -> None:
        """A reject closes on adapi only when an accept at or after the refused
        updated_ms lands, so a re-send carries the CURRENT row."""
        store, ingest, _clock, fwd = await self._started()
        await store.incidents.insert(_safety_incident(updated_ms=6_000))
        ingest.reject_safety = {"mod:$dm"}
        await fwd.step()
        self.assertEqual(store.outbox_row("mod:$dm"), (1, False, True))
        # Changed while parked: the write itself re-marks it, current row.
        store.safety_hs.clock.advance(10_000)
        await store.incidents.upsert_verdict(
            _safety_incident(action="redacted", top_score=0.95)
        )
        ingest.reject_safety = set()
        await fwd.step()
        (record,) = ingest.bodies[-1]["safety_incidents"]
        current = await store.incidents.get("mod:$dm")
        assert current is not None
        self.assertEqual(record["updated_ms"], current.updated_ms)
        self.assertGreater(record["updated_ms"], 6_000)
        self.assertEqual(record["action"], "redacted")
        self.assertEqual(store.outbox_row("mod:$dm"), (2, False, False))

    async def test_an_ack_missing_safety_unnamed_moves_nothing(self) -> None:
        store, ingest, _clock, fwd = await self._started()
        await store.incidents.insert(_safety_incident())
        ingest.answer = {"delivered_through": 0, "rejected": 0, "safety_rejected": []}
        with self.assertRaises(DeliveryError) as raised:
            await fwd.step()
        self.assertEqual(raised.exception.reason, "bad_ack")
        self.assertEqual(store.outbox_row("mod:$dm"), (1, True, False))

    async def test_twenty_per_batch_and_backlog_until_the_last(self) -> None:
        self.assertEqual(SAFETY_BATCH_MAX, 20)  # the spec's bound on one POST
        store, ingest, _clock, fwd = await self._started()
        total = SAFETY_BATCH_MAX + 5
        for n in range(total):
            await store.incidents.insert(
                _safety_incident(f"mod:${n:03d}", updated_ms=10_000 + n)
            )
        self.assertTrue(await fwd.step(), "more incidents waiting is more to do")
        first = ingest.bodies[-1]
        self.assertEqual(len(first["safety_incidents"]), SAFETY_BATCH_MAX)
        self.assertTrue(first["safety_backlog"])
        self.assertFalse(await fwd.step())
        second = ingest.bodies[-1]
        self.assertEqual(len(second["safety_incidents"]), 5)
        self.assertFalse(second["safety_backlog"])
        sent = _sent_incidents(first) + _sent_incidents(second)
        self.assertEqual(sent, [f"mod:${n:03d}" for n in range(total)])

    async def test_a_heartbeat_carries_only_the_order_and_backlog(self) -> None:
        store = FakeStore(_events(2, 4), head=5)
        store, ingest, _clock, fwd = await self._started(store)
        await fwd.step()
        body = ingest.bodies[-1]
        self.assertNotIn("safety_incidents", body)
        self.assertIs(body["safety_backlog"], False)
        self.assertEqual(len(body["safety_order"]), 2)
        # Everything else is exactly the analytics batch it always was.
        analytics = {k: v for k, v in body.items() if k not in SAFETY_KEYS}
        expected = ingest_body(
            events=analytics["events"],
            profiles=analytics["profiles"],
            delivered_through=5,
            profiles_clean_before=analytics["profiles_clean_before"],
            accept_join_pos=None,
            pending_accepts=[],
            pending_accepts_rev=analytics["pending_accepts_rev"],
        )
        self.assertEqual(analytics, expected)
        self.assertEqual(_delivered_positions(ingest), [2, 4])
        self.assertEqual(store.cursor, 5)

    async def test_the_order_only_grows_within_and_across_processes(self) -> None:
        store, ingest, clock, fwd = await self._started()
        await fwd.step()
        await store.incidents.insert(_safety_incident())
        await fwd.step()  # same millisecond as the heartbeat before it
        clock.ms -= 10_000  # and a clock stepped back
        await store.incidents.upsert_verdict(_safety_incident(top_score=0.9))
        await fwd.step()
        orders = [tuple(b["safety_order"]) for b in ingest.bodies]
        self.assertEqual(len(orders), 3)
        self.assertEqual(orders, sorted(set(orders)), orders)

        restarted = _forwarder(store, ingest, Clock())
        restarted_clock_low = orders[-1][1] - 1  # a new process, an older clock
        restarted._clock_ms = lambda: restarted_clock_low
        await restarted.start()
        await restarted.step()
        self.assertGreater(tuple(ingest.bodies[-1]["safety_order"]), orders[-1])

    async def test_first_start_backfills_every_incident(self) -> None:
        store = FakeStore([], head=0)
        await store.incidents.ensure_table()
        # History written before the outbox existed: no marks at all.
        store.write_unmarked("mod:$history1", 100)
        store.write_unmarked("report:history2", 200)
        store, ingest, _clock, fwd = await self._started(store)
        await fwd.step()
        self.assertEqual(
            sorted(_sent_incidents(ingest.bodies[-1])),
            ["mod:$history1", "report:history2"],
        )

    async def test_a_restart_heals_a_rollback_window(self) -> None:
        store, ingest, _clock, fwd = await self._started()
        await store.incidents.insert(_safety_incident("mod:$a", updated_ms=1_000_000))
        await store.incidents.insert(_safety_incident("mod:$long_ago", updated_ms=1))
        await fwd.step()
        self.assertEqual(store.safety_watermark, 1_000_000)

        # The module is rolled back: the old code writes without marks.
        store.write_unmarked("mod:$a", 2_000_000)  # an update to a sent one
        store.write_unmarked("mod:$during", 1_500_000)  # a new one
        # Committed late by another worker: nine minutes older than the
        # watermark, inside the spec's ten-minute margin.
        self.assertEqual(SAFETY_RECONCILE_MARGIN_MS, 10 * 60 * 1000)
        store.write_unmarked("mod:$late", 1_000_000 - 9 * 60 * 1000)
        # Outside the margin: delivered long ago and never touched again.
        self.assertEqual(store.outbox_row("mod:$long_ago"), (1, False, False))

        redeployed = _forwarder(store, ingest, Clock())
        await redeployed.start()
        await redeployed.step()
        self.assertEqual(
            sorted(_sent_incidents(ingest.bodies[-1])),
            ["mod:$a", "mod:$during", "mod:$late"],
        )
        self.assertEqual(store.outbox_row("mod:$long_ago"), (1, False, False))
        self.assertEqual(store.safety_watermark, 2_000_000)

    async def test_a_failed_reconciliation_never_holds_the_stream(self) -> None:
        store = FakeStore(_events(2), head=2)
        store.reconcile_error = RuntimeError("database hiccup")
        with self.assertLogs(
            "synapse.modules.synapse_pangea_chat.analytics_forward", "ERROR"
        ):
            store, ingest, clock, fwd = await self._started(store)
        await store.incidents.insert(_safety_incident())
        await fwd.step()
        body = ingest.bodies[-1]
        self.assertEqual(_delivered_positions(ingest), [2])
        self.assertEqual(_sent_incidents(body), ["mod:$dm"])  # marks still flow
        self.assertTrue(body["safety_backlog"], "a missed reconciliation said clear")
        attempts = store.reconciles

        await fwd.step()  # within the poll: not retried on every step
        self.assertEqual(store.reconciles, attempts)

        store.reconcile_error = None
        clock.ms += fwd.poll_ms
        await fwd.step()
        self.assertEqual(store.reconciles, attempts + 1)
        self.assertFalse(ingest.bodies[-1]["safety_backlog"])

    async def test_a_mark_without_its_row_is_cleared_unsent(self) -> None:
        store, ingest, _clock, fwd = await self._started()
        await store.incidents.insert(_safety_incident())
        store.safety_pool.connection.execute("DELETE FROM pangea_safety_incidents")
        store.safety_pool.connection.commit()
        await fwd.step()
        self.assertEqual(_sent_incidents(ingest.bodies[-1]), [])
        self.assertEqual(store.outbox_row("mod:$dm"), (1, False, False))
        self.assertFalse(ingest.bodies[-1]["safety_backlog"])
        self.assertIsNone(store.safety_watermark)
