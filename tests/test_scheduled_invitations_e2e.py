"""A scheduled invitation against a real Synapse, Postgres and SMTP sink: the
looping call emails a due row once, through Synapse's own mail path, and leaves
a revoked row and a later row alone. It waits for real ticks, so this class is
in ``scripts/select_tests.py``'s measured list."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from typing import Any, Dict, List
from urllib.parse import quote

import psycopg2
import requests
import testing.postgresql

from synapse_pangea_chat.student_invitations.scheduled_send import TICK_SECONDS
from tests.base_e2e import BaseSynapseE2ETest
from tests.smtp_sink import SmtpSink, body_text

SI = "/_synapse/client/pangea/v1/student_invitations/"
APP = "https://app.example.test"
CODE = "sch3dcd"
TEMPLATE = "LINK {{ join_url }} COURSE {{ course_title }}"
SUBJECT = "Join Spanish 101 on Pangea Chat"
DAY_MS = 24 * 60 * 60 * 1000


class TestScheduledInvitationsE2E(BaseSynapseE2ETest):
    sink: SmtpSink

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.sink = SmtpSink().start()
        cls.addClassCleanup(cls.sink.stop)
        cls.templates = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.templates)
        for name in ("course_invite.html", "course_invite.txt"):
            with open(os.path.join(cls.templates, name), "w", encoding="utf-8") as f:
                f.write(TEMPLATE)

    def call(self, method: str, path: str, token: str, body: Any = None) -> Any:
        resp = requests.request(
            method,
            self.server_url + path,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def course(self, token: str) -> str:
        room = self.call(
            "POST",
            "/_matrix/client/v3/createRoom",
            token,
            {
                "name": "Spanish 101",
                "preset": "private_chat",
                "creation_content": {"type": "m.space"},
            },
        )["room_id"]
        self.call(
            "PUT",
            f"/_matrix/client/v3/rooms/{quote(room, safe='')}"
            "/state/m.room.join_rules/",
            token,
            {"join_rule": "knock", "access_code": CODE},
        )
        return room

    def rows(self, room: str, token: str) -> Dict[str, Dict[str, Any]]:
        resp = requests.get(
            self.server_url + SI + "list",
            params={"room_id": room},
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return {i["invitation_id"]: i for i in resp.json()["invitations"]}

    def schedule(self, room: str, token: str, ident: str, at: int) -> None:
        body = self.call(
            "POST",
            SI + "schedule",
            token,
            {
                "room_id": room,
                "send_at_ms": at,
                "items": [{"invitation_id": ident, "expected_send_count": 0}],
            },
        )
        self.assertEqual(body["results"][0]["outcome"], "scheduled", body)

    async def test_a_due_row_is_emailed_once_and_revoked_and_later_rows_are_not(self):
        await self.homeserver(
            module_config={"app_base_url": APP},
            synapse_config_overrides={
                "email": self.sink.synapse_email_config(),
                "templates": {"custom_template_directory": self.templates},
            },
        )
        _, token = await self.new_user("teacher")
        room = self.course(token)
        due, revoked, later = (
            f"{name}+{self.tag}@school.example" for name in ("due", "revoked", "later")
        )
        added: List[Dict[str, Any]] = self.call(
            "POST",
            SI + "add",
            token,
            {"room_id": room, "emails": [due, revoked, later], "source": "manual"},
        )["invitations"]
        ids = [row["invitation_id"] for row in added]
        soon = int(time.time() * 1000) + 2000
        self.schedule(room, token, ids[0], soon)
        self.schedule(room, token, ids[1], soon)
        far = int(time.time() * 1000) + DAY_MS
        self.schedule(room, token, ids[2], far)
        self.call(
            "POST", SI + "revoke", token, {"room_id": room, "invitation_id": ids[1]}
        )
        self.assertEqual(self.rows(room, token)[ids[0]]["send_at_ms"], soon)
        self.assertEqual(self.sink.messages_to(due), [], "nothing before its time")

        mail = self.sink.wait_for(due, SUBJECT, timeout=TICK_SECONDS + 30)
        self.assertIsNotNone(mail)
        self.assertGreaterEqual(int(time.time() * 1000), soon)
        self.assertIn(f"LINK {APP}/{CODE}?inv={ids[0]}", body_text(mail))

        # One more real tick: nothing goes twice, and the others stay unsent.
        time.sleep(TICK_SECONDS + 5)
        self.assertEqual(len(self.sink.messages_to(due)), 1)
        self.assertEqual(self.sink.messages_to(revoked), [])
        self.assertEqual(self.sink.messages_to(later), [])
        rows = self.rows(room, token)
        self.assertEqual(rows[ids[0]]["send_count"], 1)
        self.assertIsNone(rows[ids[0]]["send_at_ms"])
        self.assertEqual(rows[ids[2]]["send_at_ms"], far)
        events = self.call("GET", SI + f"events?room_id={quote(room, safe='')}", token)[
            "events"
        ]
        sent = [e for e in events if e["action"] == "invite_sent"]
        self.assertEqual(
            [(e["target"], e["actor"]) for e in sent], [(ids[0], "system")]
        )

    async def test_a_moved_row_sends_once_at_its_new_time_and_send_now_clears_its_own(
        self,
    ):
        await self.homeserver(
            module_config={"app_base_url": APP},
            synapse_config_overrides={
                "email": self.sink.synapse_email_config(),
                "templates": {"custom_template_directory": self.templates},
            },
        )
        teacher, token = await self.new_user("teacher")
        room = self.course(token)
        moved, early = (
            f"{name}+{self.tag}@school.example" for name in ("moved", "early")
        )
        added: List[Dict[str, Any]] = self.call(
            "POST",
            SI + "add",
            token,
            {"room_id": room, "emails": [moved, early], "source": "manual"},
        )["invitations"]
        moved_id, early_id = (row["invitation_id"] for row in added)
        first = int(time.time() * 1000) + 2000
        self.schedule(room, token, moved_id, first)
        self.schedule(room, token, early_id, first)
        # A full tick falls between the two times.
        second = first + (TICK_SECONDS + 10) * 1000
        self.schedule(room, token, moved_id, second)
        self.assertEqual(self.rows(room, token)[moved_id]["send_at_ms"], second)

        answer = self.call(
            "POST",
            SI + "send",
            token,
            {
                "room_id": room,
                "early": True,
                "items": [{"invitation_id": early_id, "expected_send_count": 0}],
            },
        )
        self.assertEqual(answer["results"][0]["outcome"], "sent", answer)
        self.assertIsNotNone(self.sink.wait_for(early, SUBJECT, timeout=30))
        self.assertIsNone(self.rows(room, token)[early_id]["send_at_ms"])

        time.sleep(max(0.0, second / 1000 - time.time() - 1))
        self.assertEqual(self.sink.messages_to(moved), [], "silent at the first time")

        mail = self.sink.wait_for(moved, SUBJECT, timeout=TICK_SECONDS + 30)
        self.assertIsNotNone(mail)
        self.assertGreaterEqual(int(time.time() * 1000), second)

        # One more real tick: neither goes twice.
        time.sleep(TICK_SECONDS + 5)
        self.assertEqual(len(self.sink.messages_to(moved)), 1)
        self.assertEqual(len(self.sink.messages_to(early)), 1)
        rows = self.rows(room, token)
        self.assertEqual(rows[moved_id]["send_count"], 1)
        self.assertEqual(rows[early_id]["send_count"], 1)
        self.assertIsNone(rows[moved_id]["send_at_ms"])
        events = self.call("GET", SI + f"events?room_id={quote(room, safe='')}", token)[
            "events"
        ]
        moves = [
            e
            for e in events
            if e["action"] == "invite_scheduled" and e["target"] == moved_id
        ]
        self.assertEqual(len(moves), 2)
        sent = {e["target"]: e["actor"] for e in events if e["action"] == "invite_sent"}
        self.assertEqual(sent, {moved_id: "system", early_id: teacher})


class TestDueQueryOnPostgres(unittest.TestCase):
    """The additive migration and the due query on real Postgres."""

    def test_the_column_is_added_with_rows_in_place_and_the_due_query_uses_its_index(
        self,
    ) -> None:
        from synapse.storage.engines import PostgresEngine

        from synapse_pangea_chat.student_invitations.store import SCHEMA, migrate

        class Txn:
            def __init__(self, cursor: Any) -> None:
                self._cursor = cursor
                self.database_engine = PostgresEngine.__new__(PostgresEngine)

            def execute(self, sql: str, args: Any = ()) -> None:
                self._cursor.execute(sql.replace("?", "%s"), args)

            def fetchall(self) -> List[Any]:
                return self._cursor.fetchall()

        with testing.postgresql.Postgresql() as pg:
            conn = psycopg2.connect(**pg.dsn())
            try:
                cur = conn.cursor()
                for sql in SCHEMA:
                    cur.execute(sql)
                # The earlier build's table, with a row in it.
                cur.execute("ALTER TABLE pangea_seat_invitation DROP COLUMN send_at_ms")
                cur.execute(
                    "INSERT INTO pangea_seat_invitation (id, course_room_id,"
                    " email_key, state, source, invited_by, created_at_ms)"
                    " VALUES ('a', 'r', 'k', 'invited', 'manual', 't', 1)"
                )
                for _ in range(2):
                    migrate(Txn(cur))
                cur.execute("SELECT id, send_at_ms FROM pangea_seat_invitation")
                self.assertEqual(cur.fetchall(), [("a", None)])
                # Many rows, none waiting: the planner must take the index.
                cur.execute(
                    "INSERT INTO pangea_seat_invitation (id, course_room_id,"
                    " email_key, state, source, invited_by, created_at_ms)"
                    " SELECT 'r' || g, 'r', 'k' || g, 'invited', 'manual', 't', 1"
                    " FROM generate_series(1, 5000) g"
                )
                cur.execute("ANALYZE pangea_seat_invitation")
                cur.execute(
                    "EXPLAIN SELECT id FROM pangea_seat_invitation"
                    " WHERE state = 'invited' AND send_at_ms IS NOT NULL"
                    " AND send_at_ms <= 5 ORDER BY send_at_ms, id LIMIT 1"
                )
                plan = "\n".join(r[0] for r in cur.fetchall())
                self.assertIn("pangea_seat_invitation_due", plan)
            finally:
                conn.close()
