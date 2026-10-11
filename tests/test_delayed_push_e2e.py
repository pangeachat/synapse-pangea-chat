"""Delayed push against a real Synapse: a held pusher, a ring, and the wake
path between them, none of which the unit tests' fake pusher can exercise —
the queued-ring lookup reads Synapse's own tables."""

import asyncio
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, List, Tuple
from urllib.parse import quote

import requests

from synapse_pangea_chat.delayed_push.delayed_push import (
    AUDITED_SYNAPSE_VERSION,
    CALL_RING_EVENT_TYPE,
)

from .base_e2e import BaseSynapseE2ETest

# Long enough that a push arriving inside a test phase can only be one the hold
# let through.
_HOLD_MS = 60_000
_QUIET_SECONDS = 3.0
_ARRIVAL_SECONDS = 10.0


class _RecordingSygnal:
    """A push gateway that records, per pushkey, the event id of every event
    push it gets."""

    def __init__(self) -> None:
        stub = self
        self.pushes: List[Tuple[str, str]] = []
        self._lock = threading.Lock()

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                notification = json.loads(body)["notification"]
                # Badge-only updates carry no event.
                if notification.get("event_id"):
                    with stub._lock:
                        for device in notification["devices"]:
                            stub.pushes.append(
                                (device["pushkey"], notification["event_id"])
                            )
                payload = b'{"rejected": []}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/_matrix/push/v1/notify"

    def received(self, pushkey: str) -> List[str]:
        with self._lock:
            return [event_id for key, event_id in self.pushes if key == pushkey]

    def wait_for(self, pushkey: str, count: int, timeout: float) -> List[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(self.received(pushkey)) < count:
            time.sleep(0.1)
        return self.received(pushkey)

    def start(self) -> "_RecordingSygnal":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _OpenWebTab:
    """Keeps a user online the way an open web tab does: by syncing."""

    def __init__(self, server_url: str, access_token: str) -> None:
        self._url = f"{server_url}/_matrix/client/v3/sync"
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        since = None
        while not self._stop.is_set():
            params = {"timeout": "2000", "set_presence": "online"}
            if since:
                params["since"] = since
            response = requests.get(
                self._url, params=params, headers=self._headers, timeout=10
            )
            response.raise_for_status()
            since = response.json()["next_batch"]

    def start(self) -> "_OpenWebTab":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)


class TestDelayedPushRingE2E(BaseSynapseE2ETest):
    sygnal: _RecordingSygnal

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.sygnal = _RecordingSygnal().start()
        cls.addClassCleanup(cls.sygnal.stop)

    def setUp(self) -> None:
        super().setUp()
        # The gateway serves every test of the class, and a hold an earlier
        # test left can release its push into a later one; each test reads
        # only the pushes to its own bob's phone.
        self.pushkey = f"bob-phone-{self.tag}"

    def _send(self, room_id: str, token: str, event_type: str, content: dict) -> str:
        response = requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/{quote(room_id)}/send/"
            f"{event_type}/{uuid.uuid4().hex}",
            json=content,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["event_id"]

    def _ring(self, room_id: str, token: str) -> str:
        # The client's ring content (CallNotification.toContent).
        return self._send(
            room_id,
            token,
            CALL_RING_EVENT_TYPE,
            {
                "application": {
                    "type": "m.call",
                    "notification_type": "ring",
                    "sender_ts": int(time.time() * 1000),
                    "lifetime": 30_000,
                    "m.call.intent": "audio",
                    "device_id": "CALLERDEVICE",
                },
                "m.text": [{"body": "Incoming call"}],
                "m.mentions": {"user_ids": [], "room": True},
            },
        )

    def _set_client_ring_push_rule(self, token: str) -> None:
        # The rule the client sets on login (pangea_push_rules_extension.dart);
        # Synapse ships none for MSC4075 rings.
        response = requests.put(
            f"{self.server_url}/_matrix/client/v3/pushrules/global/override/"
            f"{CALL_RING_EVENT_TYPE}",
            json={
                "actions": [
                    "notify",
                    {"set_tweak": "sound", "value": "ring"},
                    {"set_tweak": "highlight", "value": False},
                ],
                "conditions": [
                    {
                        "kind": "event_match",
                        "key": "type",
                        "pattern": CALL_RING_EVENT_TYPE,
                    },
                    {
                        "kind": "event_match",
                        "key": "content.application.notification_type",
                        "pattern": "ring",
                    },
                ],
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _set_pusher(self, token: str) -> None:
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/pushers/set",
            json={
                "kind": "http",
                "app_id": "com.talktolearn.chat",
                "app_display_name": "Pangea Chat",
                "device_display_name": "Test phone",
                "pushkey": self.pushkey,
                "lang": "en",
                "data": {"url": self.sygnal.url},
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)

    async def _start_synapse_with_delayed_push(self) -> None:
        await self.homeserver(
            module_config={
                "delayed_push": {
                    "enabled": True,
                    "delay_ms": _HOLD_MS,
                    "max_delay_ms": 10 * _HOLD_MS,
                    "require_synapse_version": AUDITED_SYNAPSE_VERSION,
                }
            },
            synapse_config_overrides={
                # Synapse refuses to push to loopback addresses by default.
                "ip_range_whitelist": ["127.0.0.1"],
                "rc_message": {"per_second": 1000, "burst_count": 1000},
            },
        )

    async def _alice_and_bob_in_rooms(
        self, room_count: int
    ) -> Tuple[str, str, str, List[str]]:
        """Returns (alice_token, bob_id, bob_token, room_ids)."""
        _, alice_token = await self.new_user("alice")
        bob_id, bob_token = await self.new_user("bob")
        room_ids = []
        for _ in range(room_count):
            room_id = await self.create_private_room(alice_token)
            self.assertTrue(
                await self.invite_user_to_room(room_id, bob_id, alice_token)
            )
            self.assertTrue(await self.accept_room_invitation(room_id, bob_token))
            room_ids.append(room_id)
        return alice_token, bob_id, bob_token, room_ids

    def _message(
        self, room_id: str, token: str, body: str, mentions: List[str] | None = None
    ) -> str:
        content: dict = {"msgtype": "m.text", "body": body}
        if mentions is not None:
            content["m.mentions"] = {"user_ids": mentions}
        return self._send(room_id, token, "m.room.message", content)

    def _read(self, room_id: str, event_id: str, token: str) -> None:
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/rooms/{quote(room_id)}/receipt/"
            f"m.read/{quote(event_id)}",
            json={},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)

    async def test_ring_releases_a_held_message_while_the_callee_is_online(self):
        await self._start_synapse_with_delayed_push()
        alice_token, _, bob_token, (room_id,) = await self._alice_and_bob_in_rooms(
            room_count=1
        )
        self._set_client_ring_push_rule(bob_token)
        self._set_pusher(bob_token)
        web_tab = _OpenWebTab(self.server_url, bob_token).start()
        try:
            await asyncio.sleep(1)

            # Bob is online on web, so a message to his phone is held. Proving
            # the hold is real here is what makes the ring's arrival meaningful.
            held_message = self._message(room_id, alice_token, "hi")
            await asyncio.sleep(_QUIET_SECONDS)
            self.assertEqual(
                self.sygnal.received(self.pushkey), [], "the message was not held"
            )

            # The ring releases the hold: the held message, then the ring.
            ring = self._ring(room_id, alice_token)
            self.assertEqual(
                self.sygnal.wait_for(self.pushkey, 2, _ARRIVAL_SECONDS),
                [held_message, ring],
            )

            # Past the ring, holding resumes.
            self._message(room_id, alice_token, "still there?")
            await asyncio.sleep(_QUIET_SECONDS)
            self.assertEqual(self.sygnal.received(self.pushkey), [held_message, ring])
        finally:
            web_tab.stop()

    async def test_ring_behind_mentions_read_on_web_still_sends(self):
        # Synapse limits each fetch to 20 rows before dropping read ones, and
        # keeps read mentions, so 20 of them behind the held message used to
        # empty every later fetch and stop all pushes to the phone.
        await self._start_synapse_with_delayed_push()
        (
            alice_token,
            bob_id,
            bob_token,
            (call_room, busy_room),
        ) = await self._alice_and_bob_in_rooms(room_count=2)
        self._set_client_ring_push_rule(bob_token)
        self._set_pusher(bob_token)
        web_tab = _OpenWebTab(self.server_url, bob_token).start()
        try:
            await asyncio.sleep(1)

            held_message = self._message(call_room, alice_token, "hi")
            mention = ""
            for number in range(20):
                mention = self._message(
                    busy_room, alice_token, f"{bob_id} #{number}", mentions=[bob_id]
                )
            self._read(busy_room, mention, bob_token)
            await asyncio.sleep(_QUIET_SECONDS)
            self.assertEqual(
                self.sygnal.received(self.pushkey), [], "the messages were not held"
            )

            ring = self._ring(call_room, alice_token)
            self.assertEqual(
                self.sygnal.wait_for(self.pushkey, 2, _ARRIVAL_SECONDS),
                [held_message, ring],
            )
        finally:
            web_tab.stop()
