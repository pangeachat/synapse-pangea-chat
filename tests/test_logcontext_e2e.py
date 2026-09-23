"""Regression test: nothing in this module may leak its logcontext.

Handlers launched with a bare ``defer.ensureDeferred`` leave the request's
logcontext set on the reactor. Synapse logs "Expected logging context ... was
lost" for every such request, and since 1.159 a hardened ``clock.py`` asserts
on the leaked context: any ``looping_call`` that fires in the leaked window
stops for good ("Looping call died"), and any one-shot ``call_later`` that
fires in it never runs. Resources must launch handlers via
``synapse.logging.context.run_in_background`` instead.

Three tests, because there are three shapes of the same defect. The first
covers the HTTP resources, which is where commit ``33f7ead`` found it. The
second covers the module's own outbound HTTP calls: a raw Twisted ``Agent``
Deferred awaited without ``make_deferred_yieldable``, after an earlier
Synapse-aware await has resumed the coroutine from the reactor, hands the
request's context back to the reactor for as long as the remote takes to
answer. That is where production found it again (#214). The
third covers Tier-2 moderation, which has the harder version of the problem: a
producer running inside the notifier hands work to a pool of long-lived
consumers across a reactor boundary, and every handoff - the wakeup, the
request deferred, the timeout cancellation, the drain - is a place a context
can be handed to the wrong owner. It asserts that moderation ACTUALLY RAN,
because "enough traffic and no leak strings in the log" passes just as well
with dead workers.
"""

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import requests

from .base_e2e import BaseSynapseE2ETest
from .mock_moderation_server import FLAG_MARKER, MockModerationServer

# `Background process re-entered without a proc` is added to the two markers
# commit 33f7ead started with. It is emitted by
# `synapse.metrics.background_process_metrics` when a background process is
# resumed on a context it does not own, which is the specific failure a pool
# of long-lived workers being woken from the reactor can cause, and which the
# original two markers do not cover.
#
# `Re-starting finished log context` is deliberately NOT here, and the reason
# is measured rather than assumed: on Synapse 1.159, with this module's config
# empty and both tiers off, ordinary room creation and message sends emit it
# 28 times from Synapse's own `events_worker` fetch and `handle_new_client_event`
# paths. It is upstream behaviour, under whatever context happens to be
# current - a client request's, or ours. Asserting on it would make this gate
# fail against stock Synapse, which is a broken gate rather than a strict one.
# Re-measure before adding it; do not add it because it looks like it belongs.
#
# `leaked their logcontext to us` is the tail every 1.159 `clock.py` sentinel
# assert shares - `looping_call`, `call_later` and `add_system_event_trigger`.
# Only the first also logs "Looping call died"; a one-shot `call_later` that
# fires in a leaked window just raises that assert into the reactor and never
# runs, so without this marker a lost one-shot timer passes the gate.
LEAK_MARKERS = (
    "Expected logging context",
    "Looping call died",
    "Background process re-entered without a proc",
    "leaked their logcontext to us",
)


class TestLogcontextLeak(BaseSynapseE2ETest):
    async def test_module_endpoints_do_not_leak_logcontext(self):
        postgres = synapse_dir = config_path = None
        server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse()

            await self.register_user(
                config_path, synapse_dir, "leakuser", "leakpass", admin=False
            )
            _, access_token = await self.login_user("leakuser", "leakpass")
            headers = {"Authorization": f"Bearer {access_token}"}

            # One GET and one POST module endpoint, plus an auth-rejected
            # request — pre-fix, all three leaked (the leak sits in the shared
            # render plumbing, not any one handler).
            requests.get(
                f"{self.server_url}/_synapse/client/pangea/v1/public_courses",
                headers=headers,
            )
            requests.post(
                f"{self.server_url}/_synapse/client/pangea/v1/knock_with_code",
                json={"access_code": "no-such-code"},
                headers=headers,
            )
            requests.get(
                f"{self.server_url}/_synapse/client/pangea/v1/public_courses",
            )

            # Let the server flush its log output through the reader threads.
            await asyncio.sleep(2)

            leaked = [
                line
                for line in self.server_stdout_lines + self.server_stderr_lines
                if any(marker in line for marker in LEAK_MARKERS)
            ]
            self.assertEqual(
                leaked,
                [],
                "module requests leaked their logcontext to the reactor:\n"
                + "\n".join(leaked),
            )
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )


class _SlowSygnal:
    """A Sygnal stand-in that holds each notify for a while before answering.

    The hold is the point: a leak in an outbound call lasts exactly as long as
    the call is awaited, so an instant answer leaves a window too short for
    any `looping_call` to fire in, and the test passes against the defect.
    """

    def __init__(self, hold_seconds: float) -> None:
        stub = self
        self.hold_seconds = hold_seconds
        self.notifies = 0

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                time.sleep(stub.hold_seconds)
                stub.notifies += 1
                payload = b'{"rejected": []}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/_matrix/push/v1/notify"

    def start(self) -> "_SlowSygnal":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class TestOutboundCallLogcontext(BaseSynapseE2ETest):
    """The module's own outbound HTTP calls, not just its request plumbing.

    `send_push` is where production found it (SYNAPSE-BT, #214): the handler
    was launched correctly, but it then awaited a raw Twisted `Agent` Deferred,
    which leaves the request's context on the reactor until Sygnal answers.
    """

    async def test_send_push_to_sygnal_does_not_leak_logcontext(self) -> None:
        postgres = synapse_dir = config_path = None
        server_process = stdout_thread = stderr_thread = None
        sygnal = _SlowSygnal(hold_seconds=1.5).start()
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config={"send_push_sygnal_url": sygnal.url}
            )

            await self.register_user(
                config_path, synapse_dir, "alice", "pw", admin=False
            )
            await self.register_user(
                config_path, synapse_dir, "admin", "pw", admin=True
            )
            _, alice_token = await self.login_user("alice", "pw")
            _, admin_token = await self.login_user("admin", "pw")

            pusher = requests.post(
                f"{self.server_url}/_matrix/client/v3/pushers/set",
                json={
                    "kind": "http",
                    "app_id": "com.talktolearn.chat",
                    "app_display_name": "Pangea Chat",
                    "device_display_name": "Test iPhone",
                    "pushkey": "pushkey-1",
                    "lang": "en",
                    "data": {"url": sygnal.url},
                },
                headers={"Authorization": f"Bearer {alice_token}"},
            )
            self.assertEqual(pusher.status_code, 200, pusher.text)

            # Several held pushes, so the leaked windows add up to longer than
            # Synapse's 5-second timers (client IPs, typing timeouts), which
            # therefore fire inside one if the leak is there.
            for _ in range(6):
                response = requests.post(
                    f"{self.server_url}/_synapse/client/pangea/v1/send_push",
                    json={"user_id": "@alice:my.domain.name", "body": "Test"},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                self.assertEqual(response.status_code, 200, response.text)

            # The outbound call ACTUALLY RAN; a push that never reached Sygnal
            # has no leak window to test.
            self.assertEqual(sygnal.notifies, 6)

            await asyncio.sleep(2)
            leaked = [
                line
                for line in self.server_stdout_lines + self.server_stderr_lines
                if any(marker in line for marker in LEAK_MARKERS)
            ]
            self.assertEqual(
                leaked,
                [],
                "send_push leaked its logcontext while awaiting Sygnal:\n"
                + "\n".join(leaked),
            )
        finally:
            sygnal.stop()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )


class TestModerationLogcontext(BaseSynapseE2ETest):
    """Tier 2's producer, worker pool, timeout and drain, under real traffic.

    Everything here runs against a real homeserver on the real reactor. A
    leaked context does not fail an assertion of its own - it is reported by
    Synapse, in the server's log, which is the only place the 1.159 `clock.py`
    assertion is visible at all.
    """

    def _send(self, room_id: str, token: str, body: str, txn: str) -> Any:
        url = (
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}"
            f"/send/m.room.message/{txn}"
        )
        return requests.put(
            url,
            json={"msgtype": "m.text", "body": body},
            headers={"Authorization": f"Bearer {token}"},
        )

    def _event(self, room_id: str, event_id: str, token: str) -> Optional[Dict]:
        url = f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/event/{event_id}"
        response = requests.get(url, headers={"Authorization": f"Bearer {token}"})
        if response.status_code != 200:
            return None
        return response.json()

    def _wait_for_redaction(
        self, room_id: str, event_id: str, token: str, timeout_s: float = 30.0
    ) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            event = self._event(room_id, event_id, token)
            if event is not None and not event.get("content"):
                return
            time.sleep(0.5)
        self.fail(f"event {event_id} was never redacted")

    def _leaks(self) -> List[str]:
        return [
            line
            for line in self.server_stdout_lines + self.server_stderr_lines
            if any(marker in line for marker in LEAK_MARKERS)
        ]

    async def test_tier2_traffic_and_shutdown_do_not_leak_logcontext(self) -> None:
        postgres = synapse_dir = config_path = None
        server_process = stdout_thread = stderr_thread = None
        mock_moderation = MockModerationServer().start()
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config={
                    "moderation": {
                        "tier1_enabled": True,
                        "tier2_enabled": True,
                        "choreo_base_url": mock_moderation.base_url,
                        "choreo_access_token": "syt_mock_service_token",
                        # More messages than workers, so the queue is really
                        # used and a worker really parks and is really woken -
                        # a pool big enough to never park would test nothing.
                        "tier2_workers": 2,
                        "tier2_queue_size": 8,
                        # Short, so the supervisor fires several times during
                        # the run rather than never.
                        "tier2_supervisor_interval_seconds": 1.0,
                    },
                },
                # Synapse's own per-user message limiter, relaxed so the
                # burst below reaches the moderation queue instead of being
                # turned away at the door. It is the moderation queue's
                # backpressure this test is about, not Synapse's.
                synapse_config_overrides={
                    "rc_message": {"per_second": 1000, "burst_count": 1000}
                },
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="leaklearner",
                password="p4ssword!",
                admin=False,
            )
            _user_id, token = await self.login_user("leaklearner", "p4ssword!")
            room_id = await self.create_private_room(token)

            # Enough traffic to fill the queue, park workers, wake them, and
            # overflow - all four are paths with their own handoff.
            flagged_event_id = None
            for index in range(25):
                body = (
                    "buenos dias, como estas" if index % 5 else f"awful {FLAG_MARKER}"
                )
                response = self._send(room_id, token, body, f"txn-leak-{index}")
                self.assertEqual(response.status_code, 200, response.text)
                if index % 5 == 0:
                    flagged_event_id = response.json()["event_id"]

            assert flagged_event_id is not None
            # Moderation ACTUALLY RAN. Without this the leak assertion below
            # passes just as well against a pool of dead workers.
            self._wait_for_redaction(room_id, flagged_event_id, token)
            self.assertTrue(
                mock_moderation.seen_texts,
                "the moderation endpoint was never called",
            )
            flagged_count = len(mock_moderation.seen_texts)

            await asyncio.sleep(2)
            self.assertEqual(
                self._leaks(),
                [],
                "tier 2 traffic leaked a logcontext:\n" + "\n".join(self._leaks()),
            )

            # Now put work GENUINELY in flight and stop the server underneath
            # it. Without this the mock answers instantly, the pool is idle by
            # the time the server stops, and the drain - the thing this half of
            # the test claims to cover - never runs at all.
            mock_moderation.hold_responses_for(20.0)
            for index in range(20):
                response = self._send(
                    room_id, token, f"awful {FLAG_MARKER}", f"txn-drain-{index}"
                )
                self.assertEqual(response.status_code, 200, response.text)
            await asyncio.sleep(1)
            self.assertGreater(
                len(mock_moderation.seen_texts),
                flagged_count,
                "no check was in flight, so the drain is not being exercised",
            )

            # Shutdown wakes parked workers, fires the drain's waiter and
            # cancels its deadline, which is three more handoffs - and this is
            # the window in which the 1.159 clock refuses new calls and
            # cancels the ones it is tracking, so it is where a supervisor or
            # a drain that scheduled work unconditionally would produce
            # "Looping call died".
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
            )
            server_process = stdout_thread = stderr_thread = None
            self.assertEqual(
                self._leaks(),
                [],
                "shutdown leaked a logcontext:\n" + "\n".join(self._leaks()),
            )
            # And the drain RAN. Leak strings are an absence assertion: they
            # are satisfied just as well by a shutdown handler that was never
            # registered, or one that returned immediately - in which case
            # this half of the test proves nothing about the code it names.
            # The dispatcher says what it did on the way down, so that line is
            # the evidence.
            log = "\n".join(self.server_stdout_lines + self.server_stderr_lines)
            self.assertIn(
                "tier2 moderation shutting down with",
                log,
                "the shutdown handler never ran, so the drain is untested",
            )
            # And it RAN TO THE END. The line above proves only that the
            # handler was entered; a `shutdown` that returned immediately -
            # registering no waiter, arming no deadline, abandoning nothing -
            # would satisfy it while removing everything the drain does.
            self.assertIn(
                "tier2 moderation drain finished",
                log,
                "the drain was entered but never completed",
            )
        finally:
            mock_moderation.stop()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
