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


class _ForwardHelpers(BaseSynapseE2ETest):
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
        await self.homeserver(
            module_config=self._module_config(),
            # Room statistics can be switched off; nothing here may rely on them.
            synapse_config_overrides={"stats": {"enabled": False}},
        )

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
        rows = self._sql(
            "SELECT stream_pos FROM pangea_analytics_forward_cursor "
            "WHERE name = 'analytics_forward'"
        )
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

    def _answered(self, room_id: str, user_id: str) -> bool:
        """The consent worker has answered this invite: an acknowledged batch
        named it as owed, and a later one no longer does."""
        owed = {"room_id": room_id, "user_id": user_id}
        named = False
        for body in self.ingest.acknowledged():
            if owed in body["pending_accepts"]:
                named = True
            elif named:
                return True
        return False

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
            # Logcontext rules: the loop's DB work must stay in its own context.
            or ("pangea_analytics" in line and "from sentinel context" in line)
            or "Expected logging context pangea_analytics_forward" in line
        ]
        self.assertEqual(failures, [])


class TestAnalyticsForwardE2E(_ForwardHelpers):
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
        # Every POST that carried the invite either names it as owed or, if
        # the join was already made, carries the join's position; and a later
        # acknowledged batch carries the fence with the invite no longer owed.
        owed = {"room_id": analytics, "user_id": teacher}
        await self._until(
            lambda: self._acked_with(invite_event), "the batch carrying the invite"
        )
        for _status, body in self._attempted_with(invite_event):
            self.assertTrue(
                owed in body["pending_accepts"]
                or (body["accept_join_pos"] or 0) >= join_pos
            )
        await self._until(
            lambda: any(
                (b["accept_join_pos"] or 0) >= join_pos
                and owed not in b["pending_accepts"]
                for b in self.ingest.acknowledged()
            ),
            "the fence after the join",
        )
        self.assertGreater(join_pos, self._pos(invite_event))

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
        await self._until(
            lambda: self._answered(analytics, coteacher), "the consent answer"
        )
        self.assertEqual(self._membership(analytics, coteacher), "invite")
        self._assert_no_forwarder_errors()

    async def test_only_the_course_language_room_is_accepted(self) -> None:
        await self._start()
        teacher, t_token = await self.new_user("lteacher")
        student, s_token = await self.new_user("lstudent")
        course = self._course(t_token, teacher, teacher, "es")
        self._join(s_token, course)
        spanish = self._analytics_room(s_token)
        french = self._create_room(
            s_token,
            preset="private_chat",
            creation_content={"type": "p.analytics", "lang_code": "fr"},
        )
        self._set_profile_field(
            student,
            s_token,
            PROFILE_FIELD,
            {
                "analytics": {
                    "es": {"analytics_room_id": spanish},
                    "fr": {"analytics_room_id": french},
                }
            },
        )
        self._room_action(s_token, french, "invite", user_id=teacher)
        french_invite = self._pos(
            self._sql(
                "SELECT event_id FROM local_current_membership "
                "WHERE room_id = %s AND user_id = %s",
                (french, teacher),
            )[0][0]
        )
        await self._until(lambda: self._cursor() >= french_invite, "the invite")
        await self._until(lambda: self._answered(french, teacher), "the answer")
        self.assertEqual(self._membership(french, teacher), "invite")

        self._room_action(s_token, spanish, "invite", user_id=teacher)
        await self._until(
            lambda: self._membership(spanish, teacher) == "join", "the consent join"
        )
        self.assertEqual(self._membership(french, teacher), "invite")
        self._assert_no_forwarder_errors()

    async def test_canonical_alias_forwarded_live_and_in_the_snapshot(self) -> None:
        await self._start()
        teacher, token = await self.new_user("aliasteacher")
        course = self._course(token, teacher, teacher, "es")
        chat = self._create_room(
            token,
            preset="public_chat",
            room_alias_name=f"announcementsChat{self.tag}",
        )
        linked = requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/{self._q(course)}"
            f"/state/m.space.child/{self._q(chat)}",
            json={"via": ["localhost"]},
            headers=self._h(token),
        )
        self.assertEqual(linked.status_code, 200, linked.text)
        alias = self._sql(
            "SELECT event_id FROM current_state_events "
            "WHERE room_id = %s AND type = 'm.room.canonical_alias'",
            (chat,),
        )
        self.assertTrue(alias)
        await self._until(lambda: self._acked_with(alias[0][0]), "the alias, live")
        snap = requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/analytics_state",
            json={"room_ids": [chat], "user_ids": []},
            headers=self._h(SECRET),
        ).json()
        self.assertIn("m.room.canonical_alias", snap["forwarded_types"])
        records = [
            e
            for e in snap["rooms"][chat]["events"]
            if e["type"] == "m.room.canonical_alias"
        ]
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["content"]["alias"].startswith("#announcementsChat"))
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
        rev_before = max(b["pending_accepts_rev"] for _, b in self.ingest.requests())

        await self._crash_and_restart()

        def resend_marked() -> bool:
            rows = self._sql(
                "SELECT dirty, dirty_since FROM pangea_analytics_profile_outbox "
                "WHERE user_id = %s",
                (student,),
            )
            return bool(rows) and rows[0] == (True, 0)

        # Marked for re-send as dirty since always, so a heartbeat built while
        # the re-send is undelivered reports nothing clean.
        await self._until(resend_marked, "the startup re-send mark")
        self.ingest.set_status(200)
        await self._until(lambda: self._acked_with(second), "the batch after restart")
        await self._until(lambda: self._cursor() >= self._pos(second), "the cursor")

        after = [b for s, b in self.ingest.requests()[before_crash:] if s == 200]
        self.assertGreater(after[0]["pending_accepts_rev"], rev_before)
        delivered = [e["event_id"] for b in after for e in b["events"]]
        self.assertNotIn(first, delivered)  # acknowledged before: not re-sent
        self.assertEqual(delivered.count(second), 1)

        # Every profile re-sent first; clean only in or after the batch that
        # completes the re-send (its own profiles are delivered on its ack).
        resending = [
            i
            for i, b in enumerate(after)
            if any(p["user_id"] == student for p in b["profiles"])
        ]
        self.assertTrue(resending)
        for b in after[: resending[0]]:
            self.assertLessEqual(b["profiles_clean_before"], 0)
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

    async def test_restart_fences_a_join_left_undelivered(self) -> None:
        await self._start()
        teacher, t_token = await self.new_user("fteacher")
        student, s_token = await self.new_user("fstudent")
        room = self._analytics_room(s_token)  # in no profile: never consented
        self._room_action(s_token, room, "invite", user_id=teacher)
        invite_pos = self._pos(
            self._sql(
                "SELECT event_id FROM local_current_membership "
                "WHERE room_id = %s AND user_id = %s",
                (room, teacher),
            )[0][0]
        )
        await self._until(lambda: self._cursor() >= invite_pos, "the invite")

        self.ingest.set_status(503)
        self._join(t_token, room)  # a join whose delivery has not happened
        join_pos = self._pos(
            self._sql(
                "SELECT event_id FROM local_current_membership "
                "WHERE room_id = %s AND user_id = %s",
                (room, teacher),
            )[0][0]
        )
        await self._until(
            lambda: any(
                e["stream_pos"] == join_pos
                for _, b in self.ingest.requests()
                for e in b["events"]
            ),
            "an attempt carrying the join",
        )
        before_crash = len(self.ingest.requests())
        await self._crash_and_restart()
        self.ingest.set_status(200)
        await self._until(
            lambda: [s for s, _ in self.ingest.requests()[before_crash:] if s == 200],
            "a delivery after restart",
        )
        first = next(b for s, b in self.ingest.requests()[before_crash:] if s == 200)
        self.assertGreaterEqual(first["accept_join_pos"] or 0, join_pos)
        self._assert_no_forwarder_errors()


class TestAnalyticsForwardBarrierE2E(_ForwardHelpers):
    """A slow poll (60 s), so nothing here can pass on a heartbeat: the
    Refresh barrier is met from what has been delivered, a profile change is
    sent as soon as the head is read, and every POST is on its own
    connection with a stale one retried at once."""

    def _module_config(self) -> Dict[str, Any]:
        config = super()._module_config()
        config["analytics_forward"]["poll_seconds"] = 60
        return config

    def _head(self) -> Dict[str, Any]:
        resp = requests.get(
            f"{self.server_url}/_synapse/client/pangea/v1/analytics_forward/head",
            headers=self._h(SECRET),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _barrier_met(self, head: Dict[str, Any]) -> bool:
        acked = self.ingest.acknowledged()
        return bool(acked) and (
            max(b["delivered_through"] for b in acked) >= head["barrier_pos"]
            and max(b["profiles_clean_before"] for b in acked) >= head["barrier_time"]
        )

    async def _settled(self) -> None:
        """Startup re-send delivered, so profiles are clean."""
        await self._until(
            lambda: [
                b
                for b in self.ingest.acknowledged()
                if b["profiles_clean_before"] > 1_600_000_000_000
            ],
            "profiles clean after start",
        )

    async def test_barrier_met_by_what_is_delivered(self) -> None:
        await self._start()
        await self._settled()
        _, token = await self.new_user("bstudent")
        room = self._create_room(token, preset="private_chat")
        construct = self._send(token, room, "pangea.construct", {"n": 1})
        await self._until(lambda: self._acked_with(construct), "the construct")
        # Events the dashboard does not read move the head, not the barrier.
        self._send(token, room, "m.room.message", {"msgtype": "m.text", "body": "x"})
        head = self._head()
        self.assertGreater(head["stream_pos"], head["barrier_pos"])
        self.assertTrue(self._barrier_met(head))
        self._assert_no_forwarder_errors()

    async def test_head_sends_a_dirty_profile_at_once(self) -> None:
        await self._start()
        await self._settled()
        user, token = await self.new_user("bprofile")
        self._set_profile_field(user, token, PROFILE_FIELD, {"analytics": {}})
        started = time.monotonic()
        head = self._head()
        await self._until(lambda: self._barrier_met(head), "the barrier", timeout=5)
        self.assertLess(time.monotonic() - started, 5)  # not the 60 s poll
        # Met only by delivering the change, not by an older watermark.
        delivered = [
            p
            for b in self.ingest.acknowledged()
            for p in b["profiles"]
            if p["user_id"] == user and PROFILE_FIELD in (p["profile"] or {})
        ]
        self.assertTrue(delivered)
        self._assert_no_forwarder_errors()

    async def test_every_post_on_its_own_connection_stale_retried(self) -> None:
        await self._start()
        await self._settled()
        _, token = await self.new_user("bconn")
        room = self._create_room(token, preset="private_chat")
        before = len(self.ingest.ports())
        for n in range(3):
            event = self._send(token, room, "pangea.construct", {"n": n})
            await self._until(lambda: self._acked_with(event), "a delivery")
            await asyncio.sleep(1.5)  # past the server's keep-alive timeout
        ports = self.ingest.ports()[before:]
        self.assertEqual(len(ports), len(set(ports)))

        self.ingest.drop_next(1)  # the server closes under the request
        event = self._send(token, room, "pangea.construct", {"n": 9})
        started = time.monotonic()
        await self._until(lambda: self._acked_with(event), "the retried delivery")
        self.assertLess(time.monotonic() - started, 3)  # at once, no backoff
        self.assertEqual(self.ingest.dropped(), 1)
        self.assertFalse(
            [line for line in self.server_log() if "delivery failed" in line]
        )
        self._assert_no_forwarder_errors()
