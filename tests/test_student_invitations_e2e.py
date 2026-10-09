"""Student invitations against a real Synapse, Postgres and SMTP sink: the
routes' auth and power-level gates, the invite email, the confirm claim, the
verified-address claim at sign-in, the leave/kick/ban release, and the
database's own guard against two claims racing."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import psycopg2
import requests

from synapse_pangea_chat.config import (
    MANAGED_DISCLOSURE_TEXT,
    MANAGED_DISCLOSURE_VERSION,
)
from tests.base_e2e import BaseSynapseE2ETest
from tests.smtp_sink import SmtpSink, body_text

P = "/_synapse/client/pangea/v1/"
SI = P + "student_invitations/"
APP = "https://app.example.test"
CODE = "cl4sscd"
INVITED = "Student@School.example"
INVITED_KEY = "student@school.example"
SECOND = "second@school.example"
THIRD = "third@school.example"
LATE = "late@school.example"
MEMBER = "member@school.example"
OTHER = "other@gmail.example"
ADDRESSES = (INVITED_KEY, SECOND, THIRD, LATE, MEMBER, OTHER)
FORBIDDEN = {"error": "Forbidden: course admin required", "errcode": "M_FORBIDDEN"}
NOT_FOUND = {"error": "Not found", "errcode": "M_NOT_FOUND"}
V = MANAGED_DISCLOSURE_VERSION
SYNAPSE_LIMITS = {
    "rc_login": {
        "address": {"per_second": 9999, "burst_count": 9999},
        "account": {"per_second": 9999, "burst_count": 9999},
    },
    "rc_joins": {"local": {"per_second": 9999, "burst_count": 9999}},
    "rc_message": {"per_second": 9999, "burst_count": 9999},
    "rc_invites": {
        "per_room": {"per_second": 9999, "burst_count": 9999},
        "per_user": {"per_second": 9999, "burst_count": 9999},
    },
}
TEMPLATE = "LINK {{ join_url }} COURSE {{ course_title }} CANVAS {{ canvas_connected }}"


class TestStudentInvitationsE2E(BaseSynapseE2ETest):
    # -- helpers --

    def call(
        self,
        method: str,
        path: str,
        token: Optional[str] = None,
        body: Any = None,
        params: Optional[Dict[str, str]] = None,
    ) -> requests.Response:
        headers = {"Authorization": "Bearer " + token} if token else {}
        return requests.request(
            method,
            self.server_url + path,
            json=body,
            params=params,
            headers=headers,
            timeout=60,
        )

    def ok(self, method: str, path: str, token: Optional[str], **kw: Any) -> Any:
        resp = self.call(method, path, token, **kw)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    async def boot(self, module_config: Optional[Dict[str, Any]] = None) -> None:
        self.sink = SmtpSink().__enter__()
        self.addCleanup(self.sink.__exit__)
        template_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, template_dir)
        for name in ("course_invite.html", "course_invite.txt"):
            with open(os.path.join(template_dir, name), "w", encoding="utf-8") as f:
                f.write(TEMPLATE)
        (
            postgres,
            synapse_dir,
            self.config_path,
            server_process,
            stdout_thread,
            stderr_thread,
        ) = await self.start_test_synapse(
            module_config={"app_base_url": APP, **(module_config or {})},
            synapse_config_overrides={
                "email": self.sink.synapse_email_config(),
                "templates": {"custom_template_directory": template_dir},
                **SYNAPSE_LIMITS,
            },
        )
        self.synapse_dir = synapse_dir
        self.addCleanup(
            self.stop_synapse,
            server_process=server_process,
            stdout_thread=stdout_thread,
            stderr_thread=stderr_thread,
            synapse_dir=synapse_dir,
            postgres=postgres,
        )
        self.users: Dict[str, str] = {}
        self.tokens: Dict[str, str] = {}

    async def user(self, name: str, admin: bool = False) -> str:
        await self.register_user(
            self.config_path, self.synapse_dir, name, name + "-pw-123", admin
        )
        self.users[name], self.tokens[name] = await self.login_user(
            name, name + "-pw-123"
        )
        return self.tokens[name]

    def bind(self, name: str, *addresses: str) -> None:
        resp = self.call(
            "PUT",
            "/_synapse/admin/v2/users/" + quote(self.users[name], safe=""),
            self.tokens["root"],
            body={"threepids": [{"medium": "email", "address": a} for a in addresses]},
        )
        self.assertIn(resp.status_code, (200, 201), resp.text)

    def course(self, owner: str, name: str = "Spanish 101", code: str = CODE) -> str:
        room = self.ok(
            "POST",
            "/_matrix/client/v3/createRoom",
            self.tokens[owner],
            body={
                "name": name,
                "preset": "private_chat",
                "creation_content": {"type": "m.space"},
            },
        )["room_id"]
        self.ok(
            "PUT",
            self.state_url(room, "m.room.join_rules"),
            self.tokens[owner],
            body={"join_rule": "knock", "access_code": code},
        )
        return room

    def state_url(self, room: str, event_type: str, key: str = "") -> str:
        return (
            f"/_matrix/client/v3/rooms/{quote(room, safe='')}/state/{event_type}/{key}"
        )

    def set_power(self, room: str, user: str, level: int) -> None:
        url = self.state_url(room, "m.room.power_levels")
        content = self.ok("GET", url, self.tokens["teacher"])
        content.setdefault("users", {})[self.users[user]] = level
        self.ok("PUT", url, self.tokens["teacher"], body=content)

    def invite_and_join(self, room: str, name: str) -> None:
        self.ok(
            "POST",
            f"/_matrix/client/v3/rooms/{quote(room, safe='')}/invite",
            self.tokens["teacher"],
            body={"user_id": self.users[name]},
        )
        self.ok(
            "POST",
            f"/_matrix/client/v3/join/{quote(room, safe='')}",
            self.tokens[name],
            body={},
        )

    def joined_rooms(self, name: str) -> List[str]:
        return self.ok("GET", "/_matrix/client/v3/joined_rooms", self.tokens[name])[
            "joined_rooms"
        ]

    def membership(self, room: str, name: str) -> Optional[str]:
        resp = self.call(
            "GET",
            self.state_url(room, "m.room.member", self.users[name]),
            self.tokens["teacher"],
        )
        return resp.json().get("membership") if resp.status_code == 200 else None

    def add(self, room: str, *emails: str) -> List[Dict[str, Any]]:
        return self.ok(
            "POST",
            SI + "add",
            self.tokens["teacher"],
            body={"room_id": room, "emails": list(emails), "source": "manual"},
        )["invitations"]

    def listing(self, room: str) -> Dict[str, Dict[str, Any]]:
        body = self.ok(
            "GET", SI + "list", self.tokens["teacher"], params={"room_id": room}
        )
        return {i["invitation_id"]: i for i in body["invitations"]}

    def confirm(self, name: str, ident: str, version: int = V) -> requests.Response:
        return self.call(
            "POST",
            SI + "confirm",
            self.tokens[name],
            body={"invitation_id": ident, "disclosure_version": version},
        )

    def managed(self, user_id: str, room: str) -> Optional[Tuple[Any, ...]]:
        conn = psycopg2.connect(self.database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, course_room_id, invited_by FROM pangea_managed_account"
                    " WHERE user_id = %s AND course_room_id = %s",
                    (user_id, room),
                )
                return cur.fetchone()
        finally:
            conn.close()

    def module_log_lines(self) -> List[str]:
        return [
            line
            for line in self.server_stdout_lines + self.server_stderr_lines
            if "synapse_pangea_chat" in line
        ]

    # -- tests --

    async def test_teacher_routes_require_pl100_with_one_403_and_students_see_only_their_own(
        self,
    ):
        await self.boot()
        for name in ("teacher", "coteacher", "outsider", "student"):
            await self.user(name)
        await self.user("root", admin=True)
        room = self.course("teacher")
        other_room = self.course("outsider", name="Not yours", code="n0ty0ur")
        self.invite_and_join(room, "coteacher")
        self.set_power(room, "coteacher", 50)
        (inv,) = self.add(room, INVITED)
        ident = inv["invitation_id"]
        self.bind("outsider", OTHER)
        self.assertEqual(
            self.confirm("outsider", ident).json()["result"], "pending_approval"
        )

        calls = [
            ("POST", "add", {"emails": [SECOND], "source": "manual"}),
            (
                "POST",
                "send",
                {"items": [{"invitation_id": ident, "expected_send_count": 0}]},
            ),
            ("GET", "list", None),
            ("POST", "revoke", {"invitation_id": ident}),
            ("GET", "pending_approvals", None),
            (
                "POST",
                "decide",
                {
                    "invitation_id": ident,
                    "user_id": self.users["outsider"],
                    "decision": "grant",
                },
            ),
            ("POST", "approve_all", {}),
            ("POST", "invite_member", {"user_id": self.users["coteacher"]}),
            ("GET", "live", {"invitation_id": ident}),
        ]
        for caller in ("coteacher", "outsider", "student"):
            for target in (room, "!unknown:my.domain.name", other_room):
                if caller == "outsider" and target == other_room:
                    continue  # their own course
                for method, path, extra in calls:
                    if method == "GET":
                        params = {"room_id": target, **(extra or {})}
                        resp = self.call(
                            method, SI + path, self.tokens[caller], params=params
                        )
                    else:
                        resp = self.call(
                            method,
                            SI + path,
                            self.tokens[caller],
                            body={"room_id": target, **extra},
                        )
                    self.assertEqual(
                        (resp.status_code, resp.json()),
                        (403, FORBIDDEN),
                        (caller, path, target),
                    )
                    self.assertNotIn("gmail", resp.text)
        for method, path, _ in calls:
            resp = self.call(method, SI + path, None, body={"room_id": room})
            self.assertEqual(resp.status_code, 401, path)
            self.assertEqual(resp.json()["errcode"], "M_UNAUTHORIZED")
        for path in ("confirm", "mine/pending", "mine/joined"):
            resp = self.call("POST" if path == "confirm" else "GET", SI + path, None)
            self.assertEqual(resp.status_code, 401, path)
        # Nothing changed, nothing was sent.
        listed = self.listing(room)
        self.assertEqual(list(listed), [ident])
        self.assertEqual(listed[ident]["state"], "invited")
        self.assertEqual(listed[ident]["send_count"], 0)
        self.assertEqual(self.sink.messages_to(INVITED), [])
        # The course's admin (PL100) and a room-version creator pass.
        live = self.ok(
            "GET",
            SI + "live",
            self.tokens["teacher"],
            params={"room_id": room, "invitation_id": ident},
        )
        self.assertEqual(live, {"live": True, "state": "invited"})
        (pending,) = self.ok(
            "GET",
            SI + "pending_approvals",
            self.tokens["teacher"],
            params={"room_id": room},
        )["pending"]
        self.assertEqual(pending["signed_up_as_email"], OTHER)
        # Promoting the co-teacher to 100 admits them: checked per request.
        self.set_power(room, "coteacher", 100)
        self.ok("GET", SI + "list", self.tokens["coteacher"], params={"room_id": room})
        # A course admin who leaves the course keeps power level 100 in the
        # room state, but is no longer its admin.
        self.ok(
            "POST",
            f"/_matrix/client/v3/rooms/{quote(room, safe='')}/leave",
            self.tokens["coteacher"],
            body={},
        )
        resp = self.call(
            "GET", SI + "list", self.tokens["coteacher"], params={"room_id": room}
        )
        self.assertEqual((resp.status_code, resp.json()), (403, FORBIDDEN))
        # Class code join alone records nothing.
        resp = self.call(
            "POST",
            P + "knock_with_code",
            self.tokens["student"],
            body={"access_code": CODE},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.ok(
            "POST",
            f"/_matrix/client/v3/join/{quote(room, safe='')}",
            self.tokens["student"],
            body={},
        )
        self.assertIn(room, self.joined_rooms("student"))
        self.assertEqual(
            self.ok("GET", SI + "mine/joined", self.tokens["student"]),
            {"invitations": []},
        )
        self.assertIsNone(self.managed(self.users["student"], room))
        self.assertEqual(list(self.listing(room)), [ident])
        # The course's own admin claiming an invitation here is joined, not
        # managed (owner amendment 2026-10-09).
        self.bind("teacher", THIRD)
        (own,) = self.add(room, THIRD)
        claimed = self.confirm("teacher", own["invitation_id"])
        self.assertEqual(claimed.json()["result"], "claimed", claimed.text)
        self.assertEqual(self.listing(room)[own["invitation_id"]]["state"], "joined")
        self.assertIsNone(self.managed(self.users["teacher"], room))

    async def test_invite_confirm_claim_release_flow(self):
        await self.boot()
        for name in ("teacher", "student", "other", "late", "member", "third"):
            await self.user(name)
        await self.user("root", admin=True)
        room = self.course("teacher")
        self.bind("student", INVITED_KEY)
        self.bind("other", OTHER)
        self.bind("third", THIRD)

        # Add: case and space variants are one invitation; bad input stores nothing.
        a, a2, b, c, late = self.add(
            room, INVITED, "  student@SCHOOL.example ", SECOND, THIRD, LATE
        )
        self.assertEqual(a["invitation_id"], a2["invitation_id"])
        bad = self.call(
            "POST",
            SI + "add",
            self.tokens["teacher"],
            body={"room_id": room, "emails": ["x", MEMBER], "source": "csv"},
        )
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["invalid_indexes"], [0])
        self.assertNotIn(MEMBER, bad.text)

        # Send: the invite email carries the class link plus the id.
        sent = self.ok(
            "POST",
            SI + "send",
            self.tokens["teacher"],
            body={
                "room_id": room,
                "items": [
                    {"invitation_id": a["invitation_id"], "expected_send_count": 0}
                ],
            },
        )
        self.assertEqual(sent["results"][0]["outcome"], "sent", sent)
        mail = self.sink.wait_for(INVITED, "Join Spanish 101 on Pangea Chat")
        self.assertIsNotNone(mail)
        text = body_text(mail)
        self.assertIn(f"LINK {APP}/{CODE}?inv={a['invitation_id']}", text)
        self.assertIn("CANVAS False", text)
        self.assertNotIn("school", text.split("LINK", 1)[1].split("COURSE")[0])
        stale = self.ok(
            "POST",
            SI + "send",
            self.tokens["teacher"],
            body={
                "room_id": room,
                "items": [
                    {"invitation_id": a["invitation_id"], "expected_send_count": 0}
                ],
            },
        )
        self.assertEqual(
            (stale["results"][0]["outcome"], stale["results"][0]["reason"]),
            ("skipped", "stale"),
        )

        # Public reads: hint and disclosure.
        hint = self.ok(
            "GET", SI + "hint", None, params={"invitation_id": a["invitation_id"]}
        )
        self.assertEqual(
            hint,
            {"course_name": "Spanish 101", "masked_email_hint": "s***@school.example"},
        )
        disclosure = self.ok("GET", P + "managed_disclosure", None)
        self.assertEqual(disclosure, {"version": V, "text": MANAGED_DISCLOSURE_TEXT})

        # The student with the invited address verified sees it, confirms, claims.
        pending = self.ok("GET", SI + "mine/pending", self.tokens["student"])
        self.assertEqual(
            pending,
            {
                "invitations": [
                    {
                        "invitation_id": a["invitation_id"],
                        "room_id": room,
                        "course_name": "Spanish 101",
                    }
                ]
            },
        )
        outdated = self.confirm("student", a["invitation_id"], V - 1)
        self.assertEqual(
            (outdated.status_code, outdated.json()["errcode"]),
            (409, "ORG.PANGEA.DISCLOSURE_OUTDATED"),
        )
        claimed = self.confirm("student", a["invitation_id"])
        self.assertEqual(
            (claimed.status_code, claimed.json()),
            (
                200,
                {
                    "result": "claimed",
                    "invitation_id": a["invitation_id"],
                    "room_id": room,
                },
            ),
        )
        self.assertIn(room, self.joined_rooms("student"))
        self.assertEqual(
            self.managed(self.users["student"], room),
            (self.users["student"], room, self.users["teacher"]),
        )
        self.assertEqual(
            self.ok("GET", SI + "mine/joined", self.tokens["student"]),
            {
                "invitations": [
                    {
                        "invitation_id": a["invitation_id"],
                        "room_id": room,
                        "invited_by": self.users["teacher"],
                    }
                ]
            },
        )
        self.assertEqual(
            self.ok("GET", SI + "mine/pending", self.tokens["student"]),
            {"invitations": []},
        )
        self.assertEqual(
            self.call(
                "GET", SI + "hint", None, params={"invitation_id": a["invitation_id"]}
            ).json(),
            NOT_FOUND,
        )

        # Another address: a pending approval the teacher grants.
        self.assertEqual(
            self.confirm("other", b["invitation_id"]).json()["result"],
            "pending_approval",
        )
        self.assertNotIn(room, self.joined_rooms("other"))
        (row,) = self.ok(
            "GET",
            SI + "pending_approvals",
            self.tokens["teacher"],
            params={"room_id": room},
        )["pending"]
        self.assertEqual(
            (row["user_id"], row["signed_up_as_email"]), (self.users["other"], OTHER)
        )
        self.assertEqual(self.listing(room)[b["invitation_id"]]["pending_count"], 1)
        granted = self.ok(
            "POST",
            SI + "decide",
            self.tokens["teacher"],
            body={
                "room_id": room,
                "invitation_id": b["invitation_id"],
                "user_id": self.users["other"],
                "decision": "grant",
            },
        )
        self.assertEqual(granted["invitation"]["state"], "joined")
        self.assertIn(room, self.joined_rooms("other"))

        # A confirmed invitation is claimed when its address is verified later.
        self.assertEqual(
            self.confirm("late", late["invitation_id"]).json()["result"],
            "pending_approval",
        )
        self.bind("late", LATE)
        self.assertEqual(self.listing(room)[late["invitation_id"]]["state"], "joined")
        self.assertIn(room, self.joined_rooms("late"))
        self.assertIsNotNone(self.managed(self.users["late"], room))

        # Leave, kick and ban release the invitation and the managed record.
        self.ok(
            "POST",
            f"/_matrix/client/v3/rooms/{quote(room, safe='')}/leave",
            self.tokens["student"],
            body={},
        )
        self.ok(
            "POST",
            f"/_matrix/client/v3/rooms/{quote(room, safe='')}/kick",
            self.tokens["teacher"],
            body={"user_id": self.users["other"]},
        )
        self.assertEqual(
            self.confirm("third", c["invitation_id"]).json()["result"], "claimed"
        )
        self.ok(
            "POST",
            f"/_matrix/client/v3/rooms/{quote(room, safe='')}/ban",
            self.tokens["teacher"],
            body={"user_id": self.users["third"]},
        )
        listed = self.listing(room)
        for ident, name in ((a, "student"), (b, "other"), (c, "third")):
            self.assertEqual(listed[ident["invitation_id"]]["state"], "left", name)
            self.assertIsNone(self.managed(self.users[name], room), name)
        self.assertEqual(listed[late["invitation_id"]]["state"], "joined")

        # Rejoining with the class code restores nothing.
        resp = self.call(
            "POST",
            P + "knock_with_code",
            self.tokens["student"],
            body={"access_code": CODE},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.ok(
            "POST",
            f"/_matrix/client/v3/join/{quote(room, safe='')}",
            self.tokens["student"],
            body={},
        )
        await self.login_user("student", "student-pw-123")
        self.assertEqual(self.listing(room)[a["invitation_id"]]["state"], "left")
        self.assertIsNone(self.managed(self.users["student"], room))
        self.assertEqual(
            self.ok("GET", SI + "mine/joined", self.tokens["student"]),
            {"invitations": []},
        )

        # Re-invite resets the row; a fresh confirmation claims again.
        (again,) = self.add(room, INVITED)
        self.assertEqual(
            (again["invitation_id"], again["state"], again["send_count"]),
            (a["invitation_id"], "invited", 1),
        )
        self.assertEqual(
            self.confirm("student", a["invitation_id"]).json()["result"], "claimed"
        )

        # Invite to a seat: from the member's own address, which nothing returns.
        resp = self.call(
            "POST",
            P + "knock_with_code",
            self.tokens["member"],
            body={"access_code": CODE},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.ok(
            "POST",
            f"/_matrix/client/v3/join/{quote(room, safe='')}",
            self.tokens["member"],
            body={},
        )
        no_email = self.call(
            "POST",
            SI + "invite_member",
            self.tokens["teacher"],
            body={"room_id": room, "user_id": self.users["member"]},
        )
        self.assertEqual(
            (no_email.status_code, no_email.json()["errcode"]),
            (409, "ORG.PANGEA.NO_EMAIL"),
        )
        self.bind("member", MEMBER)
        made = self.ok(
            "POST",
            SI + "invite_member",
            self.tokens["teacher"],
            body={"room_id": room, "user_id": self.users["member"]},
        )
        self.assertEqual(set(made), {"invitation_id", "state"})
        listed = self.listing(room)[made["invitation_id"]]
        self.assertEqual((listed["email"], listed["source"]), (None, "member"))
        self.assertNotIn(MEMBER, str(self.listing(room)))
        self.assertEqual(
            self.ok("GET", SI + "mine/pending", self.tokens["member"])["invitations"][
                0
            ]["invitation_id"],
            made["invitation_id"],
        )

        # Revoke releases the managed record and keeps the membership.
        revoked = self.ok(
            "POST",
            SI + "revoke",
            self.tokens["teacher"],
            body={"room_id": room, "invitation_id": late["invitation_id"]},
        )
        self.assertEqual(revoked["invitation"]["state"], "revoked")
        self.assertIsNone(self.managed(self.users["late"], room))
        self.assertEqual(self.membership(room, "late"), "join")

        # No address in any of the module's log lines.
        lines = "\n".join(self.module_log_lines()).lower()
        self.assertIn("student invitation", lines)
        for address in ADDRESSES:
            self.assertNotIn(address, lines)

    async def test_hint_lookup_is_rate_limited_per_ip_and_repeated_probing_is_throttled(
        self,
    ):
        await self.boot(
            {
                "student_invitations_hint_requests_per_burst": 3,
                "student_invitations_hint_burst_duration_seconds": 60,
            }
        )
        await self.user("teacher")
        room = self.course("teacher")
        (inv,) = self.add(room, INVITED)
        statuses = [
            self.call(
                "GET", SI + "hint", None, params={"invitation_id": ident}
            ).status_code
            for ident in (
                "guess-1",
                "guess-2",
                inv["invitation_id"],
                inv["invitation_id"],
            )
        ]
        self.assertEqual(statuses, [404, 404, 200, 429])
        limited = self.call(
            "GET", SI + "hint", None, params={"invitation_id": inv["invitation_id"]}
        )
        self.assertEqual(
            limited.json(), {"error": "Rate limited", "errcode": "M_LIMIT_EXCEEDED"}
        )
        self.assertNotIn("school", limited.text)
        # The disclosure has its own budget.
        self.ok("GET", P + "managed_disclosure", None)

    async def test_concurrent_claims_never_double_claim(self):
        await self.boot()
        for name in ("teacher", "student", "a", "b"):
            await self.user(name)
        await self.user("root", admin=True)
        room = self.course("teacher")
        self.bind("student", INVITED_KEY, SECOND)
        first, second = self.add(room, INVITED, SECOND)
        # Already in the course through the class code, so the race is the
        # claim's alone and not two joins of one account.
        resp = self.call(
            "POST",
            P + "knock_with_code",
            self.tokens["student"],
            body={"access_code": CODE},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.ok(
            "POST",
            f"/_matrix/client/v3/join/{quote(room, safe='')}",
            self.tokens["student"],
            body={},
        )
        shared = self.add(room, THIRD)[0]["invitation_id"]
        for name in ("a", "b"):
            self.assertEqual(
                self.confirm(name, shared).json()["result"], "pending_approval"
            )

        def run(calls: List[Any]) -> List[requests.Response]:
            results: List[Optional[requests.Response]] = [None] * len(calls)
            barrier = threading.Barrier(len(calls))

            def go(i: int) -> None:
                barrier.wait()
                results[i] = calls[i]()

            threads = [
                threading.Thread(target=go, args=(i,)) for i in range(len(calls))
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            return [r for r in results if r is not None]

        # One account, two invitations of one course, confirmed at once.
        responses = run(
            [
                lambda: self.confirm("student", first["invitation_id"]),
                lambda: self.confirm("student", second["invitation_id"]),
            ]
        )
        outcomes = sorted(
            (r.status_code, r.json().get("result") or r.json().get("errcode"))
            for r in responses
        )
        self.assertEqual(
            outcomes, [(200, "claimed"), (409, "ORG.PANGEA.ALREADY_CLAIMED_IN_COURSE")]
        )
        listed = self.listing(room)
        self.assertEqual(
            sorted(listed[i["invitation_id"]]["state"] for i in (first, second)),
            ["invited", "joined"],
        )

        # Two grants of one invitation to two accounts, at once.
        def grant(name: str) -> requests.Response:
            return self.call(
                "POST",
                SI + "decide",
                self.tokens["teacher"],
                body={
                    "room_id": room,
                    "invitation_id": shared,
                    "user_id": self.users[name],
                    "decision": "grant",
                },
            )

        responses = run([lambda: grant("a"), lambda: grant("b")])
        self.assertEqual(
            sorted(r.status_code for r in responses),
            [200, 409],
            [r.text for r in responses],
        )
        claimant = self.listing(room)[shared]["claimant"]
        self.assertIn(claimant, (self.users["a"], self.users["b"]))
        loser = self.users["b"] if claimant == self.users["a"] else self.users["a"]
        self.assertIsNone(self.managed(loser, room))
        self.assertIsNotNone(self.managed(claimant, room))
