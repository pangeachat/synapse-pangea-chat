"""Real Synapse/Postgres/SMTP coverage of claiming a prepared course by verified
address: when one is added to an account, when an account signs in, when an
email sign-up completes, and what another account's link gets afterwards."""

import re
from urllib.parse import quote

import requests

from tests.base_e2e import BaseSynapseE2ETest
from tests.smtp_sink import SmtpSink, body_text

PREFIX = "/_synapse/client/pangea/"
APP = "https://app.example.test"
# As typed at the booth; Synapse stores verified addresses in lower case.
REQUESTED = "Teacher@School.example"
VERIFIED = "teacher@school.example"
# The test signs in six times; Synapse's default login limit allows three.
RATE_LIMITS = {
    "rc_login": {
        "address": {"per_second": 9999, "burst_count": 9999},
        "account": {"per_second": 9999, "burst_count": 9999},
    },
}


class TestClaimByEmailE2E(BaseSynapseE2ETest):
    def request(self, method, path, token, body=None):
        return requests.request(
            method,
            self.server_url + path,
            json=body,
            headers={"Authorization": "Bearer " + token},
            timeout=60,
        )

    def prepare(self, op, key, email):
        prepared = self.request(
            "POST",
            PREFIX + "v2/create_course_space",
            op,
            {
                "request_key": key,
                "teacher_email": email,
                "course_plan_id": "quest-fixture",
                "title": "Prepared course " + key,
                "target_language": "es",
            },
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        return prepared.json()["invitation_id"]

    def status(self, op, ident):
        return self.request("GET", PREFIX + "v2/course_invitations/" + ident, op).json()

    def joined_rooms(self, token):
        return self.request("GET", "/_matrix/client/v3/joined_rooms", token).json()[
            "joined_rooms"
        ]

    async def test_verified_address_claims_at_sign_in(self):
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
                    synapse_config_overrides={
                        "email": sink.synapse_email_config(),
                        **RATE_LIMITS,
                    },
                )
                users = {}
                tokens = {}
                for name in ("operator", "teacher", "other"):
                    await self.register_user(
                        config_path=config_path,
                        dir=synapse_dir,
                        user=name,
                        password="123123123",
                        admin=name == "operator",
                    )
                    users[name], tokens[name] = await self.login_user(name, "123123123")
                op = tokens["operator"]

                first = self.prepare(op, "first", REQUESTED)
                ready = sink.wait_for(REQUESTED, "Your quest is ready")
                self.assertIsNotNone(ready)
                self.assertIn("sign up or log in", " ".join(body_text(ready).split()))
                code = re.search(
                    re.escape(APP) + r"/([a-zA-Z0-9]{7})", body_text(ready)
                ).group(1)
                untouched = self.prepare(op, "untouched", "someone@else.example")

                # Adding the verified address claims during that request.
                added = self.request(
                    "PUT",
                    "/_synapse/admin/v2/users/" + quote(users["teacher"], safe=""),
                    op,
                    {"threepids": [{"medium": "email", "address": VERIFIED}]},
                )
                self.assertEqual(added.status_code, 200, added.text)
                claimed = self.status(op, first)
                self.assertEqual(claimed["status"], "completed", claimed)
                self.assertEqual(claimed["claimant"], users["teacher"])
                room = claimed["room_id"]
                self.assertEqual(self.joined_rooms(tokens["teacher"]), [room])
                power = self.request(
                    "GET",
                    "/_matrix/client/v3/rooms/"
                    + quote(room, safe="")
                    + "/state/m.room.power_levels",
                    tokens["teacher"],
                ).json()
                self.assertEqual(power["users"][users["teacher"]], 100)
                self.assertIsNotNone(sink.wait_for(REQUESTED, "Invite your students"))

                # Signing in again makes no second course.
                _, tokens["teacher"] = await self.login_user("teacher", "123123123")
                self.assertEqual(self.joined_rooms(tokens["teacher"]), [room])

                # Another account's link is spent, as for an unknown code.
                stolen = self.request(
                    "POST",
                    PREFIX + "v1/knock_with_code",
                    tokens["other"],
                    {"access_code": code},
                )
                self.assertEqual(stolen.status_code, 404, stolen.text)
                self.assertEqual(stolen.json()["errcode"], "ORG.PANGEA.CODE_NOT_FOUND")

                # A course prepared after the address was added is claimed
                # when the account next signs in.
                later = self.prepare(op, "later", VERIFIED)
                self.assertEqual(self.status(op, later)["status"], "prepared")
                _, tokens["teacher"] = await self.login_user("teacher", "123123123")
                later_status = self.status(op, later)
                self.assertEqual(later_status["status"], "completed", later_status)
                self.assertEqual(
                    sorted(self.joined_rooms(tokens["teacher"])),
                    sorted([room, later_status["room_id"]]),
                )

                # An account without a matching address claims nothing.
                _, tokens["other"] = await self.login_user("other", "123123123")
                self.assertEqual(self.joined_rooms(tokens["other"]), [])
                self.assertEqual(self.status(op, untouched)["status"], "prepared")
            finally:
                self.stop_synapse(
                    server_process=server_process,
                    stdout_thread=stdout_thread,
                    stderr_thread=stderr_thread,
                    synapse_dir=synapse_dir,
                    postgres=postgres,
                )

    async def test_email_sign_up_claims_before_any_login(self):
        """The app signs in from the /register response, which runs no login
        callback: the claim has to come from Synapse storing the address."""
        signup = "signup@school.example"
        secret = "claim-by-email-secret"
        register = "/_matrix/client/v3/register"
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
                    synapse_config_overrides={
                        "email": sink.synapse_email_config(),
                        "enable_registration": True,
                        "registrations_require_3pid": ["email"],
                        **RATE_LIMITS,
                    },
                )
                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user="operator",
                    password="123123123",
                    admin=True,
                )
                _, op = await self.login_user("operator", "123123123")
                ident = self.prepare(op, "signup", signup)

                account = {"username": "signup", "password": "123123123"}
                started = requests.post(self.server_url + register, json=account)
                self.assertEqual(started.status_code, 401, started.text)
                requested = requests.post(
                    self.server_url + register + "/email/requestToken",
                    json={"client_secret": secret, "email": signup, "send_attempt": 1},
                )
                self.assertEqual(requested.status_code, 200, requested.text)
                sid = requested.json()["sid"]
                validation = sink.wait_for(signup, "Validate")
                self.assertIsNotNone(validation)
                token = re.search(
                    r"submit_token\?\S*?token=([A-Za-z0-9]+)", body_text(validation)
                ).group(1)
                confirmed = requests.get(
                    self.server_url
                    + "/_matrix/client/unstable/registration/email/submit_token",
                    params={"token": token, "client_secret": secret, "sid": sid},
                )
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                completed = requests.post(
                    self.server_url + register,
                    json={
                        **account,
                        "auth": {
                            "type": "m.login.email.identity",
                            "threepid_creds": {"sid": sid, "client_secret": secret},
                            "session": started.json()["session"],
                        },
                    },
                )
                self.assertEqual(completed.status_code, 200, completed.text)

                claimed = self.status(op, ident)
                self.assertEqual(claimed["status"], "completed", claimed)
                self.assertEqual(claimed["claimant"], completed.json()["user_id"])
                self.assertEqual(
                    self.joined_rooms(completed.json()["access_token"]),
                    [claimed["room_id"]],
                )
            finally:
                self.stop_synapse(
                    server_process=server_process,
                    stdout_thread=stdout_thread,
                    stderr_thread=stderr_thread,
                    synapse_dir=synapse_dir,
                    postgres=postgres,
                )
