"""Real Synapse/Postgres/SMTP coverage of roomless invitations and recovery."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import psycopg2
import requests

from tests.base_e2e import BaseSynapseE2ETest
from tests.smtp_sink import SmtpSink, body_text

PREFIX = "/_synapse/client/pangea/"
EMAIL = "requester@school.example"
APP = "https://app.example.test"


class TestDeferredCourseClaimE2E(BaseSynapseE2ETest):
    def request(self, method, path, token, body=None):
        return requests.request(
            method,
            self.server_url + path,
            json=body,
            headers={"Authorization": "Bearer " + token},
            timeout=30,
        )

    def post(self, path, token, body):
        return self.request("POST", PREFIX + path, token, body)

    def state(self, token, room, kind):
        r = self.request(
            "GET",
            "/_matrix/client/v3/rooms/" + quote(room, safe="") + "/state/" + kind,
            token,
        )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    async def test_roomless_claim_replay_recovery_and_instructor_grant(self):
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        with SmtpSink() as sink:
            try:
                (
                    postgres,
                    synapse_dir,
                    config_path,
                    server_process,
                    stdout_thread,
                    stderr_thread,
                ) = await self.start_test_synapse(
                    module_config={"app_base_url": APP},
                    synapse_config_overrides={"email": sink.synapse_email_config()},
                )
                tokens = {}
                users = {}
                for name in ("operator", "teacher", "student"):
                    await self.register_user(
                        config_path=config_path,
                        dir=synapse_dir,
                        user=name,
                        password="123123123",
                        admin=name == "operator",
                    )
                    users[name], tokens[name] = await self.login_user(name, "123123123")
                op, teacher, student = (
                    tokens[n] for n in ("operator", "teacher", "student")
                )
                body = {
                    "request_key": "request-1",
                    "teacher_email": EMAIL,
                    "course_plan_id": "quest-fixture",
                    "title": "Prepared course",
                    "target_language": "es",
                }
                denied = self.post("v2/create_course_space", teacher, body)
                self.assertEqual(denied.status_code, 403, denied.text)
                prepared = self.post("v2/create_course_space", op, body)
                self.assertEqual(prepared.status_code, 200, prepared.text)
                result = prepared.json()
                ident = result["invitation_id"]
                self.assertEqual(result["status"], "prepared")
                self.assertIsNone(result["room_id"])
                self.assertEqual(result["delivery_outcome"], "accepted")
                self.assertNotIn(EMAIL, prepared.text)
                ready = sink.wait_for(EMAIL, "Your quest is ready")
                self.assertIsNotNone(ready)
                code = re.search(
                    re.escape(APP) + r"/([a-zA-Z0-9]{7})", body_text(ready)
                ).group(1)
                self.assertNotIn(code, prepared.text)
                repeated = self.post("v2/create_course_space", op, body)
                self.assertEqual(repeated.json()["invitation_id"], ident)
                self.assertEqual(len(sink.messages_to(EMAIL)), 1)
                conflict = self.post(
                    "v2/create_course_space", op, {**body, "title": "Changed"}
                )
                self.assertEqual(conflict.status_code, 409, conflict.text)
                preview = self.post(
                    "v1/preview_with_code", teacher, {"access_code": code}
                )
                self.assertEqual(preview.status_code, 409, preview.text)
                self.assertEqual(
                    preview.json()["errcode"], "ORG.PANGEA.COURSE_NOT_CREATED"
                )
                status_path = PREFIX + "v2/course_invitations/" + ident
                self.assertEqual(
                    self.request("GET", status_path, op).json()["status"], "prepared"
                )
                self.assertEqual(
                    self.request("GET", status_path, student).status_code, 403
                )
                reminder = self.post(
                    "v2/send_course_claim_reminder",
                    op,
                    {
                        "invitation_id": ident,
                        "subject": "Invitation reminder",
                        "body": "Your prepared course awaits.",
                        "cta_label": "Claim course",
                    },
                )
                self.assertEqual(reminder.status_code, 200, reminder.text)
                message = sink.wait_for(EMAIL, "Invitation reminder")
                new_code = re.search(
                    re.escape(APP) + r"/([a-zA-Z0-9]{7})", body_text(message)
                ).group(1)
                self.assertNotEqual(code, new_code)
                # Neither the operator nor a conversational bot is a member.
                claimed = self.post(
                    "v1/knock_with_code", teacher, {"access_code": code}
                )
                self.assertEqual(claimed.status_code, 200, claimed.text)
                room = claimed.json()["already_joined"][0]
                self.assertEqual(claimed.json()["rooms"], [])
                members = self.request(
                    "GET",
                    "/_matrix/client/v3/rooms/"
                    + quote(room, safe="")
                    + "/joined_members",
                    teacher,
                ).json()["joined"]
                self.assertEqual(set(members), {users["teacher"]})
                self.assertEqual(
                    self.state(teacher, room, "m.room.power_levels")["users"][
                        users["teacher"]
                    ],
                    100,
                )
                self.assertEqual(
                    self.state(teacher, room, "pangea.course_plan"),
                    {"uuid": "quest-fixture", "l2": "es"},
                )
                self.assertTrue(
                    self.state(teacher, room, "pangea.course_settings")[
                        "require_analytics_access"
                    ]
                )
                self.assertIsNotNone(sink.wait_for(EMAIL, "Invite your students"))
                for link in (code, new_code):
                    replay = self.post(
                        "v1/knock_with_code", teacher, {"access_code": link}
                    )
                    self.assertEqual(replay.json()["already_joined"], [room])
                    stolen = self.post(
                        "v1/knock_with_code", student, {"access_code": link}
                    )
                    self.assertEqual(stolen.status_code, 404, stolen.text)
                self.assertEqual(len(sink.messages_to(EMAIL)), 3)
                self.assertEqual(
                    self.post(
                        "v1/preview_with_code", teacher, {"access_code": code}
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    self.post(
                        "v1/preview_with_code", student, {"access_code": code}
                    ).status_code,
                    404,
                )
                # Simulate loss of the post-create association/completion write.
                with psycopg2.connect(self.database_url) as conn, conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM pangea_course_claim WHERE room_id = %s", (room,)
                    )
                    cur.execute(
                        "UPDATE pangea_course_invitation SET status = 'provisioning', room_id = NULL, completed_at_ms = NULL WHERE invitation_id = %s",
                        (ident,),
                    )
                recovered = self.post(
                    "v1/knock_with_code", teacher, {"access_code": code}
                )
                self.assertEqual(recovered.status_code, 200, recovered.text)
                self.assertEqual(recovered.json()["already_joined"], [room])
                # Current client's join after already_joined remains idempotent.
                joined = self.request(
                    "POST",
                    "/_matrix/client/v3/rooms/" + quote(room, safe="") + "/join",
                    teacher,
                    {},
                )
                self.assertEqual(joined.status_code, 200, joined.text)
                join_rules = self.state(teacher, room, "m.room.join_rules")
                student_invite = self.post(
                    "v1/knock_with_code",
                    student,
                    {"access_code": join_rules["access_code"]},
                )
                self.assertEqual(student_invite.status_code, 200, student_invite.text)
                joined = self.request(
                    "POST",
                    "/_matrix/client/v3/rooms/" + quote(room, safe="") + "/join",
                    student,
                    {},
                )
                self.assertEqual(joined.status_code, 200, joined.text)
                rules_path = (
                    "/_matrix/client/v3/rooms/"
                    + quote(room, safe="")
                    + "/state/m.room.join_rules"
                )
                grant = self.request(
                    "PUT",
                    rules_path,
                    teacher,
                    {**join_rules, "admin_access_code": "c0teach"},
                )
                self.assertEqual(grant.status_code, 200, grant.text)
                grant = self.post(
                    "v1/knock_with_code", student, {"access_code": "c0teach"}
                )
                self.assertEqual(grant.status_code, 200, grant.text)
                self.assertEqual(grant.json()["already_joined"], [room])
                power = self.state(teacher, room, "m.room.power_levels")
                self.assertEqual(power["users"][users["student"]], 100)
                # Demotion must beat completed replay and partial recovery.
                power["users"][users["teacher"]] = 0
                demote = self.request(
                    "PUT",
                    "/_matrix/client/v3/rooms/"
                    + quote(room, safe="")
                    + "/state/m.room.power_levels",
                    teacher,
                    power,
                )
                self.assertEqual(demote.status_code, 200, demote.text)
                self.assertEqual(
                    self.post(
                        "v1/knock_with_code", teacher, {"access_code": code}
                    ).status_code,
                    404,
                )
                with psycopg2.connect(self.database_url) as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE pangea_course_invitation SET status = 'provisioning' WHERE invitation_id = %s",
                        (ident,),
                    )
                partial = self.post(
                    "v1/knock_with_code", teacher, {"access_code": code}
                )
                self.assertEqual(partial.status_code, 503, partial.text)
                self.assertEqual(
                    self.state(student, room, "m.room.power_levels")["users"][
                        users["teacher"]
                    ],
                    0,
                )
                revoke = self.request("DELETE", status_path, op)
                self.assertEqual(revoke.status_code, 200, revoke.text)
                self.assertEqual(
                    self.post(
                        "v1/knock_with_code", teacher, {"access_code": code}
                    ).status_code,
                    404,
                )
                # Two accounts race over a new preparation: only one room/winner.
                other = self.post(
                    "v2/create_course_space",
                    op,
                    {
                        **body,
                        "request_key": "race",
                        "teacher_email": "race@school.example",
                    },
                )
                self.assertEqual(other.status_code, 200, other.text)
                race_mail = sink.wait_for("race@school.example", "Your quest is ready")
                race_code = re.search(
                    re.escape(APP) + r"/([a-zA-Z0-9]{7})", body_text(race_mail)
                ).group(1)
                with ThreadPoolExecutor(2) as pool:
                    futures = [
                        pool.submit(
                            self.post,
                            "v1/knock_with_code",
                            tok,
                            {"access_code": race_code},
                        )
                        for tok in (teacher, student)
                    ]
                    responses = [f.result() for f in futures]
                self.assertEqual(
                    sorted(r.status_code for r in responses),
                    [200, 404],
                    [r.text for r in responses],
                )
                race_status = self.request(
                    "GET",
                    PREFIX + "v2/course_invitations/" + other.json()["invitation_id"],
                    op,
                )
                self.assertEqual(race_status.json()["status"], "completed")
                self.assertNotEqual(race_status.json()["room_id"], room)
                self.assertNotIn(
                    EMAIL, json.dumps(self.state(student, room, "m.room.create"))
                )
            finally:
                self.stop_synapse(
                    server_process=server_process,
                    stdout_thread=stdout_thread,
                    stderr_thread=stderr_thread,
                    synapse_dir=synapse_dir,
                    postgres=postgres,
                )
