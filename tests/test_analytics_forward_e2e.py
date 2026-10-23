"""The analytics forwarder against a real homeserver and a stand-in ingest.

What these prove end to end, on Synapse's own tables:

* the stored cursor never passes an event the ingest has not acknowledged;
* a crashed homeserver resumes from the stored cursor: nothing acknowledged is
  re-sent, nothing unacknowledged is lost;
* a profile is cleared only for the revision that was sent;
* after a restart every profile is re-sent before the heartbeat reports
  profiles clean, and an invite left pending is accepted at startup;
* a consented analytics invite is accepted, and the join's position fences the
  batch that carries the invite; a co-teacher's invite is not;
* the head and snapshot endpoints answer only with the shared secret.
"""

import asyncio
import subprocess
import sys
import threading
import time
import uuid
from typing import IO, Any, Callable, Dict, List, Optional, Tuple, Union
from urllib.parse import quote

import psycopg2
import requests

from .base_e2e import BaseSynapseE2ETest
from .mock_analytics_ingest import MockIngest

SECRET = "analytics-forward-test-secret-0123456789"
PROFILE_FIELD = "pangea.analytics_profile"


class TestAnalyticsForwardE2E(BaseSynapseE2ETest):
    ingest: MockIngest

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.ingest = MockIngest(SECRET)
        cls.ingest.start()
        cls.addClassCleanup(cls.ingest.stop)

    def _module_config(self) -> Dict[str, Any]:
        return {
            "analytics_forward": {
                "url": self.ingest.url,
                "secret": SECRET,
                "poll_seconds": 1,
                "batch_max": 50,
            }
        }

    async def _start(self) -> None:
        self.ingest.set_status(200)
        self.ingest.set_on_post(None)
        await self.homeserver(module_config=self._module_config())

    # -- helpers ------------------------------------------------------------

    def _h(self, token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _q(self, value: str) -> str:
        return quote(value, safe="")

    def _sql(self, query: str, args: Tuple[Any, ...] = ()) -> List[Tuple[Any, ...]]:
        conn = psycopg2.connect(self.database_url)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(query, args)
                return list(cur.fetchall())
        finally:
            conn.close()

    def _cursor(self) -> int:
        rows = self._sql("SELECT stream_pos FROM pangea_analytics_forward_cursor")
        return int(rows[0][0]) if rows else -1

    def _pos(self, event_id: str) -> int:
        return int(
            self._sql(
                "SELECT stream_ordering FROM events WHERE event_id = %s", (event_id,)
            )[0][0]
        )

    async def _until(
        self, condition: Callable[[], Any], what: str, timeout: float = 30.0
    ) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            await asyncio.sleep(0.2)
        self.fail(f"timed out waiting for {what}")

    def _create_room(self, token: str, **body: Any) -> str:
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json=body,
            headers=self._h(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["room_id"]

    def _send(self, token: str, room_id: str, etype: str, content: dict) -> str:
        resp = requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/{self._q(room_id)}/send/"
            f"{etype}/{uuid.uuid4().hex}",
            json=content,
            headers=self._h(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["event_id"]

    def _room_action(self, token: str, room_id: str, action: str, **body: Any) -> str:
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/rooms/{self._q(room_id)}/{action}",
            json=body,
            headers=self._h(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.text

    def _join(self, token: str, room_id: str) -> None:
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/join/{self._q(room_id)}",
            json={},
            headers=self._h(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def _set_profile_field(
        self, user_id: str, token: str, field: str, value: Any
    ) -> None:
        resp = requests.put(
            f"{self.server_url}/_matrix/client/v3/profile/{self._q(user_id)}/{field}",
            json={field: value},
            headers=self._h(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def _membership(self, room_id: str, user_id: str) -> Optional[str]:
        rows = self._sql(
            "SELECT membership FROM local_current_membership "
            "WHERE room_id = %s AND user_id = %s",
            (room_id, user_id),
        )
        return rows[0][0] if rows else None

    def _acked_with(self, event_id: str) -> List[dict]:
        return [
            body
            for body in self.ingest.acknowledged()
            if any(e["event_id"] == event_id for e in body["events"])
        ]

    def _attempted_with(self, event_id: str) -> List[Tuple[int, dict]]:
        return [
            (status, body)
            for status, body in self.ingest.requests()
            if any(e["event_id"] == event_id for e in body["events"])
        ]

    def _outbox(self, user_id: str) -> Optional[Tuple[int, bool]]:
        rows = self._sql(
            "SELECT revision, dirty FROM pangea_analytics_profile_outbox "
            "WHERE user_id = %s",
            (user_id,),
        )
        return (int(rows[0][0]), bool(rows[0][1])) if rows else None

    async def _crash_and_restart(self) -> None:
        """SIGKILL the homeserver and start it again on the same database."""
        shared = type(self).__dict__["_shared_homeserver"]
        shared.server_process.kill()
        shared.server_process.wait(timeout=10)
        shared.stdout_thread.join(timeout=5)
        shared.stderr_thread.join(timeout=5)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "synapse.app.homeserver",
                "--config-path",
                shared.config_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=shared.synapse_dir,
            text=True,
        )

        def pump(pipe: Union[IO[str], None], sink: List[str]) -> None:
            if pipe is None:
                return
            for line in iter(pipe.readline, ""):
                sink.append(line.rstrip("\n"))
            pipe.close()

        shared.stdout_thread = threading.Thread(
            target=pump, args=(process.stdout, shared.stdout_lines), daemon=True
        )
        shared.stderr_thread = threading.Thread(
            target=pump, args=(process.stderr, shared.stderr_lines), daemon=True
        )
        shared.stdout_thread.start()
        shared.stderr_thread.start()
        shared.server_process = process

        def healthy() -> bool:
            try:
                return requests.get(f"{self.server_url}/health", timeout=2).ok
            except requests.exceptions.ConnectionError:
                return False

        await self._until(healthy, "the restarted homeserver")

    def _course(
        self, teacher_token: str, teacher: str, co_admin: str, language: str
    ) -> str:
        return self._create_room(
            teacher_token,
            preset="public_chat",
            creation_content={"type": "m.space"},
            initial_state=[
                {
                    "type": "pangea.course_plan",
                    "state_key": "",
                    "content": {"uuid": "plan", "l2": language},
                }
            ],
            power_level_content_override={"users": {teacher: 100, co_admin: 100}},
        )

    def _analytics_room(self, student_token: str) -> str:
        return self._create_room(
            student_token,
            preset="private_chat",
            creation_content={"type": "p.analytics", "lang_code": "es"},
        )

    def _point_profile(self, student: str, token: str, room_id: str) -> None:
        self._set_profile_field(
            student,
            token,
            PROFILE_FIELD,
            {"analytics": {"es": {"analytics_room_id": room_id}}},
        )

    def _assert_no_forwarder_errors(self) -> None:
        failures = [
            line
            for line in self.server_log()
            if "analytics forwarder: step failed" in line
            or "analytics_forward: start failed" in line
            or "analytics_forward endpoint failed" in line
            or "analytics_forward: profile outbox write failed" in line
            or "consented-invite join failed" in line
            or ("pangea_analytics_forward" in line and "threw an exception" in line)
        ]
        self.assertEqual(failures, [])

    # -- tests --------------------------------------------------------------

    async def test_cursor_moves_only_after_an_acknowledged_batch(self) -> None:
        await self._start()
        _, token = await self.new_user("student")
        room = self._create_room(token, preset="private_chat")
        self.ingest.set_status(503)
        construct = self._send(token, room, "pangea.construct", {"uses": [1]})
        message = self._send(
            token, room, "m.room.message", {"msgtype": "m.text", "body": "hi"}
        )
        pos = self._pos(construct)

        await self._until(lambda: self._attempted_with(construct), "a delivery attempt")
        await asyncio.sleep(1.5)  # retries keep failing
        self.assertLess(self._cursor(), pos)
        self.assertEqual(self._acked_with(construct), [])

        self.ingest.set_status(200)
        await self._until(lambda: self._acked_with(construct), "the acknowledged batch")
        await self._until(lambda: self._cursor() >= pos, "the cursor")
        sent = self._acked_with(construct)[0]["events"]
        record = next(e for e in sent if e["event_id"] == construct)
        self.assertEqual(record["stream_pos"], pos)
        self.assertEqual(record["type"], "pangea.construct")
        self.assertIsNone(record["state_key"])
        self.assertEqual(record["content"], {"uses": [1]})
        # A type the dashboard does not read is never sent.
        self.assertEqual(self._attempted_with(message), [])
        self._assert_no_forwarder_errors()

    async def test_profile_cleared_only_for_the_revision_sent(self) -> None:
        await self._start()
        user, token = await self.new_user("profiled")
        fired: List[int] = []

        def change_during_delivery(body: dict) -> None:
            mine = [p for p in body["profiles"] if p["user_id"] == user]
            if mine and not fired:
                fired.append(mine[0]["revision"])
                resp = requests.put(
                    f"{self.server_url}/_matrix/client/v3/profile/{self._q(user)}/displayname",
                    json={"displayname": "Second"},
                    headers=self._h(token),
                )
                assert resp.status_code == 200, resp.text

        self.ingest.set_on_post(change_during_delivery)
        self._set_profile_field(user, token, PROFILE_FIELD, {"analytics": {}})
        await self._until(lambda: fired, "the first delivery")
        first_revision = fired[0]

        def resent() -> List[dict]:
            return [
                p
                for body in self.ingest.acknowledged()
                for p in body["profiles"]
                if p["user_id"] == user and p["revision"] > first_revision
            ]

        later = await self._until(resent, "the re-send of the newer revision")
        self.assertEqual(later[-1]["profile"]["displayname"], "Second")
        await self._until(
            lambda: self._outbox(user) == (later[-1]["revision"], False),
            "the outbox to clear at the newer revision",
        )
        self._assert_no_forwarder_errors()

    async def test_consented_invite_is_accepted_and_fenced(self) -> None:
        await self._start()
        teacher, t_token = await self.new_user("teacher")
        coteacher, c_token = await self.new_user("coteacher")
        student, s_token = await self.new_user("student")
        course = self._course(t_token, teacher, coteacher, "es")
        self._join(s_token, course)
        self._join(c_token, course)
        analytics = self._analytics_room(s_token)
        self._point_profile(student, s_token, analytics)

        self._room_action(s_token, analytics, "invite", user_id=teacher)
        await self._until(
            lambda: self._membership(analytics, teacher) == "join", "the consent join"
        )
        join_pos = int(
            self._sql(
                "SELECT e.stream_ordering FROM local_current_membership m "
                "JOIN events e ON e.event_id = m.event_id "
                "WHERE m.room_id = %s AND m.user_id = %s",
                (analytics, teacher),
            )[0][0]
        )
        invite_event = self._sql(
            "SELECT event_id FROM events WHERE room_id = %s "
            "AND type = 'm.room.member' AND state_key = %s "
            "ORDER BY stream_ordering LIMIT 1",
            (analytics, teacher),
        )[0][0]
        # Every POST that carried the invite, acknowledged or not, was sent
        # after the join and carries its position.
        carrying = await self._until(
            lambda: self._acked_with(invite_event), "the batch carrying the invite"
        )
        for _status, body in self._attempted_with(invite_event):
            self.assertGreaterEqual(body["accept_join_pos"] or 0, join_pos)
        self.assertGreater(join_pos, self._pos(invite_event))
        self.assertTrue(carrying)

        # Fact 3: a teacher the student approved inviting a colleague is not
        # the student's consent.
        self._room_action(t_token, analytics, "invite", user_id=coteacher)
        invite_pos = self._pos(
            self._sql(
                "SELECT event_id FROM local_current_membership "
                "WHERE room_id = %s AND user_id = %s",
                (analytics, coteacher),
            )[0][0]
        )
        await self._until(
            lambda: self._cursor() >= invite_pos, "delivery of the invite"
        )
        self.assertEqual(self._membership(analytics, coteacher), "invite")
        self._assert_no_forwarder_errors()

    async def test_head_and_state_need_the_secret(self) -> None:
        await self._start()
        student, s_token = await self.new_user("snap")
        analytics = self._analytics_room(s_token)
        head_url = f"{self.server_url}/_synapse/client/pangea/v1/analytics_forward/head"
        state_url = f"{self.server_url}/_synapse/client/pangea/v1/analytics_state"
        self.assertEqual(requests.get(head_url).status_code, 401)
        self.assertEqual(
            requests.get(head_url, headers=self._h("wrong" * 10)).status_code, 401
        )
        self.assertEqual(
            requests.post(state_url, json={}, headers=self._h(s_token)).status_code,
            401,
        )

        head = requests.get(head_url, headers=self._h(SECRET))
        self.assertEqual(head.status_code, 200, head.text)
        self.assertGreater(head.json()["stream_pos"], 0)
        self.assertGreater(head.json()["module_time_ms"], 1_600_000_000_000)

        before = self._outbox(student)
        snap = requests.post(
            state_url,
            json={"room_ids": [analytics, "!unknown:localhost"], "user_ids": [student]},
            headers=self._h(SECRET),
        )
        self.assertEqual(snap.status_code, 200, snap.text)
        rooms = snap.json()["rooms"]
        self.assertIsNone(rooms["!unknown:localhost"])
        types = {e["type"] for e in rooms[analytics]["events"]}
        self.assertIn("m.room.create", types)
        self.assertIn("m.room.member", types)
        self.assertTrue(all(e["stream_pos"] > 0 for e in rooms[analytics]["events"]))
        [profile] = snap.json()["profiles"]
        after = self._outbox(student)
        assert after is not None
        self.assertEqual(profile["revision"], after[0])
        self.assertGreater(after[0], before[0] if before else 0)

        bad = requests.post(state_url, json={"room_ids": "x"}, headers=self._h(SECRET))
        self.assertEqual(bad.status_code, 400)
        self._assert_no_forwarder_errors()

    async def test_crash_resumes_from_the_cursor(self) -> None:
        await self._start()
        teacher, t_token = await self.new_user("rteacher")
        student, s_token = await self.new_user("rstudent")
        course = self._course(t_token, teacher, teacher, "es")
        self._join(s_token, course)
        analytics = self._analytics_room(s_token)

        first = self._send(s_token, analytics, "pangea.construct", {"n": 1})
        await self._until(lambda: self._acked_with(first), "the first batch")

        # An invite the stream refuses and delivers (the profile does not name
        # the room yet), then the profile: only the startup scan accepts it now.
        self._room_action(s_token, analytics, "invite", user_id=teacher)
        invite_pos = self._pos(
            self._sql(
                "SELECT event_id FROM local_current_membership "
                "WHERE room_id = %s AND user_id = %s",
                (analytics, teacher),
            )[0][0]
        )
        await self._until(lambda: self._cursor() >= invite_pos, "the refused invite")
        self._point_profile(student, s_token, analytics)

        self.ingest.set_status(503)
        second = self._send(s_token, analytics, "pangea.construct", {"n": 2})
        await self._until(lambda: self._attempted_with(second), "an attempt")
        self.assertEqual(self._membership(analytics, teacher), "invite")
        before_crash = len(self.ingest.requests())

        await self._crash_and_restart()
        self.ingest.set_status(200)
        await self._until(lambda: self._acked_with(second), "the batch after restart")
        await self._until(lambda: self._cursor() >= self._pos(second), "the cursor")

        after = [b for s, b in self.ingest.requests()[before_crash:] if s == 200]
        delivered = [e["event_id"] for b in after for e in b["events"]]
        self.assertNotIn(first, delivered)  # acknowledged before: not re-sent
        self.assertEqual(delivered.count(second), 1)

        # Every profile re-sent first; clean only afterwards.
        self.assertLessEqual(after[0]["profiles_clean_before"], 0)
        resent = {p["user_id"] for b in after for p in b["profiles"]}
        self.assertIn(student, resent)
        await self._until(
            lambda: [
                b
                for b in self.ingest.acknowledged()[-3:]
                if b["profiles_clean_before"] > 1_600_000_000_000
            ],
            "a clean heartbeat",
        )

        # The invite left pending is accepted by the startup scan.
        await self._until(
            lambda: self._membership(analytics, teacher) == "join",
            "the startup consent join",
        )
        self._assert_no_forwarder_errors()
