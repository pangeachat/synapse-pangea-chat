"""The forwarder loop's delivery rules, over an in-memory store.

The store's SQL is exercised against a real homeserver in
``test_analytics_forward_e2e.py``; these tests pin what the LOOP does with it:
the cursor moves only on an acknowledged 200, a restart resumes from the
stored cursor, a profile is cleared only for the revision that was sent, and
the heartbeat reports profiles clean only once the startup re-send is in.
"""

import json
import os
import unittest
from typing import Any, Dict, List, Optional, Sequence, Tuple

from synapse_pangea_chat.analytics_forward.config import (
    AnalyticsForwardConfig,
    parse_analytics_forward_config,
)
from synapse_pangea_chat.analytics_forward.forwarder import (
    MAX_BACKOFF_SECONDS,
    PROFILE_CLOCK_GRACE_MS,
    DeliveryError,
    Forwarder,
)
from synapse_pangea_chat.analytics_forward.wire import (
    BadAck,
    check_ack,
    ingest_body,
    is_forwarded,
)

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

    async def advance(self, cursor: int, acked) -> None:
        self.cursor = max(self.cursor or 0, cursor)
        for user_id, revision in acked:
            row = self.outbox[user_id]
            if row["revision"] == revision:
                row["dirty"] = False
                row["dirty_since"] = None

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
        oldest = dirty[0][0] if dirty else None
        return [(uid, rev) for _, uid, rev in dirty[:limit]], oldest

    async def profiles(self, user_ids):
        return {u: self.profile_data[u] for u in user_ids if u in self.profile_data}

    async def pending_analytics_invites(self):
        return list(self.pending)


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

    async def __call__(self, body: dict) -> Any:
        self.bodies.append(json.loads(json.dumps(body)))
        if self.on_post is not None:
            self.on_post(body)
        if self.fail_with is not None:
            raise self.fail_with
        if self.answer is not None:
            return self.answer
        return {"delivered_through": body["delivered_through"], "rejected": 0}


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
        self.assertLess(second["profiles_clean_before"], barrier_time)
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
    async def test_invite_batch_carries_the_join_position(self) -> None:
        invite = FakeEvent(
            4, "m.room.member", "@teacher:hs", {"membership": "invite"}, "!a:hs"
        )
        store = FakeStore([invite], head=4)
        consent = FakeConsent()
        consent.position = 7
        ingest = Ingest()
        clock = Clock()
        fwd = _forwarder(store, ingest, clock, consent=consent)
        await fwd.start()
        ingest.fail_with = DeliveryError("http", "HTTP 503")
        with self.assertRaises(DeliveryError):
            await fwd.step()
        self.assertEqual(consent.calls, [("!a:hs", "@teacher:hs")])
        self.assertEqual(ingest.bodies[-1]["accept_join_pos"], 7)
        ingest.fail_with = None
        consent.position = 0  # the retry finds the teacher already joined
        await fwd.step()
        self.assertEqual(ingest.bodies[-1]["accept_join_pos"], 7)
        clock.ms += 5_000
        await fwd.step()
        self.assertEqual(len(ingest.bodies), 3)
        self.assertIsNone(ingest.bodies[-1]["accept_join_pos"])

    async def test_join_error_holds_the_invite_batch(self) -> None:
        invite = FakeEvent(
            4, "m.room.member", "@teacher:hs", {"membership": "invite"}, "!a:hs"
        )
        store = FakeStore([invite], head=4)
        consent = FakeConsent()
        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock(), consent=consent)
        await fwd.start()

        async def broken(room_id: str, user_id: str) -> Any:
            raise ConnectionError("join failed")

        consent.accept_if_consented = broken  # type: ignore[method-assign]
        with self.assertRaises(ConnectionError):
            await fwd.step()
        self.assertEqual(ingest.bodies, [])  # never sent without its join
        self.assertEqual(store.cursor, 0)
        consent.accept_if_consented = FakeConsent().accept_if_consented  # type: ignore[method-assign]
        await fwd.step()
        self.assertEqual(store.cursor, 4)

    async def test_pending_scan_error_does_not_stop_the_start(self) -> None:
        store = FakeStore([], head=0)
        store.pending = [("!a:hs", "@t:hs")]
        consent = FakeConsent()

        async def broken(room_id: str, user_id: str) -> Any:
            raise ConnectionError("join failed")

        consent.accept_if_consented = broken  # type: ignore[method-assign]
        fwd = _forwarder(store, Ingest(), Clock(), consent=consent)
        await fwd.start()
        self.assertEqual(fwd.cursor, 0)

    async def test_pending_invites_are_scanned_at_start(self) -> None:
        store = FakeStore([], head=0)
        store.pending = [("!a:hs", "@t:hs")]
        consent = FakeConsent()
        consent.position = 3
        ingest = Ingest()
        fwd = _forwarder(store, ingest, Clock(), consent=consent)
        await fwd.start()
        self.assertEqual(consent.calls, [("!a:hs", "@t:hs")])
        await fwd.step()
        self.assertEqual(ingest.bodies[0]["accept_join_pos"], 3)

    async def test_join_and_knock_are_not_consent_checked(self) -> None:
        events = [
            FakeEvent(1, "m.room.member", "@t:hs", {"membership": "join"}),
            FakeEvent(2, "m.room.member", "@t:hs", {"membership": "knock"}),
        ]
        store = FakeStore(events, head=2)
        consent = FakeConsent()
        fwd = _forwarder(store, Ingest(), Clock(), consent=consent)
        await fwd.start()
        await fwd.step()
        self.assertEqual(consent.calls, [])


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
        self.assertEqual(clock.slept, [1.0, 2.0, 4.0])
        self.assertEqual(store.cursor, 1)

    def test_backoff_is_capped_through_a_long_outage(self) -> None:
        fwd = _forwarder(FakeStore([], 0), Ingest(), Clock())
        delays = [fwd._backoff(DeliveryError("http", "HTTP 503")) for _ in range(2000)]
        self.assertEqual(delays[:7], [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0])
        self.assertEqual(delays[-1], MAX_BACKOFF_SECONDS)

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
        self.assertEqual(clock.slept, [MAX_BACKOFF_SECONDS])
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

    def test_contract_fixture_matches_the_body(self) -> None:
        path = os.path.join(
            os.path.dirname(__file__),
            "fixtures",
            "analytics_forward",
            "ingest_contract.json",
        )
        with open(path, encoding="utf-8") as f:
            contract = json.load(f)
        request_schema = contract["request_schema"]
        example = contract["request_example"]
        body = ingest_body(
            events=example["events"],
            profiles=example["profiles"],
            delivered_through=example["delivered_through"],
            profiles_clean_before=example["profiles_clean_before"],
            accept_join_pos=example["accept_join_pos"],
        )
        self.assertEqual(body, example)
        self.assertEqual(set(body), set(request_schema["required"]))
        self.assertEqual(set(body), set(request_schema["properties"]))
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


class TestRequestTimeout(unittest.TestCase):
    """A response whose body never ends must not stall the loop."""

    def test_a_stalled_body_times_out(self) -> None:
        from synapse.util.clock import Clock as SynapseClock
        from twisted.internet import defer
        from twisted.internet.testing import MemoryReactorClock

        from synapse_pangea_chat.analytics_forward.forwarder import (
            REQUEST_TIMEOUT_SECONDS,
            make_http_poster,
        )

        class Transport:
            def stopProducing(self) -> None:  # noqa: N802
                pass

            def abortConnection(self) -> None:  # noqa: N802
                pass

        class StalledResponse:
            code = 200
            phrase = b"OK"
            length = 10

            def deliverBody(self, protocol: Any) -> None:  # noqa: N802
                protocol.makeConnection(Transport())  # the body never ends

        class Client:
            async def request(self, *args: Any, **kwargs: Any) -> Any:
                return StalledResponse()

        reactor = MemoryReactorClock()
        post = make_http_poster(
            Client(), "http://ingest/x", SECRET, SynapseClock(reactor, "hs")  # type: ignore[arg-type]
        )

        async def call() -> Any:
            return await post({"delivered_through": 1})

        result: "defer.Deferred[Any]" = defer.ensureDeferred(call())
        failures: List[Any] = []
        result.addErrback(failures.append)
        reactor.advance(REQUEST_TIMEOUT_SECONDS - 1)
        self.assertEqual(failures, [])
        reactor.advance(2)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0].value, DeliveryError)
