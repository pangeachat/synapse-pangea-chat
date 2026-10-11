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
PASSWORD = "123123123"


class ClaimByEmailHelpers:
    """What the two classes share. They differ in whether Synapse's own
    registration is open, so each has a homeserver of its own; the sink is in
    that homeserver's config, so it lives as long as the class."""

    sink: SmtpSink

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()  # type: ignore[misc]
        cls.sink = SmtpSink().start()
        cls.addClassCleanup(cls.sink.stop)

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
                # Request keys are unique server-wide.
                "request_key": f"{key}-{self.tag}",
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


class TestClaimByEmailE2E(ClaimByEmailHelpers, BaseSynapseE2ETest):
    async def test_verified_address_claims_at_sign_in(self):
        # As typed at the booth; Synapse stores verified addresses in lower case.
        requested = f"Teacher+{self.tag}@School.example"
        verified = f"teacher+{self.tag}@school.example"
        await self.homeserver(
            module_config={"app_base_url": APP},
            synapse_config_overrides={"email": self.sink.synapse_email_config()},
        )
        users = {}
        tokens = {}
        for name in ("operator", "teacher", "other"):
            users[name], tokens[name] = await self.new_user(
                name, admin=name == "operator", password=PASSWORD
            )
        op = tokens["operator"]

        first = self.prepare(op, "first", requested)
        ready = self.sink.wait_for(requested, "Your quest is ready")
        self.assertIsNotNone(ready)
        self.assertIn("sign up or log in", " ".join(body_text(ready).split()))
        code = re.search(re.escape(APP) + r"/([a-zA-Z0-9]{7})", body_text(ready)).group(
            1
        )
        untouched = self.prepare(op, "untouched", f"someone+{self.tag}@else.example")

        # Adding the verified address claims during that request.
        added = self.request(
            "PUT",
            "/_synapse/admin/v2/users/" + quote(users["teacher"], safe=""),
            op,
            {"threepids": [{"medium": "email", "address": verified}]},
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
        self.assertIsNotNone(self.sink.wait_for(requested, "Invite your students"))

        # Signing in again makes no second course.
        _, tokens["teacher"] = await self.login_user(
            self.localpart("teacher"), PASSWORD
        )
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
        later = self.prepare(op, "later", verified)
        self.assertEqual(self.status(op, later)["status"], "prepared")
        _, tokens["teacher"] = await self.login_user(
            self.localpart("teacher"), PASSWORD
        )
        later_status = self.status(op, later)
        self.assertEqual(later_status["status"], "completed", later_status)
        self.assertEqual(
            sorted(self.joined_rooms(tokens["teacher"])),
            sorted([room, later_status["room_id"]]),
        )

        # An account without a matching address claims nothing.
        _, tokens["other"] = await self.login_user(self.localpart("other"), PASSWORD)
        self.assertEqual(self.joined_rooms(tokens["other"]), [])
        self.assertEqual(self.status(op, untouched)["status"], "prepared")


class TestClaimByEmailSignUpE2E(ClaimByEmailHelpers, BaseSynapseE2ETest):
    async def test_email_sign_up_claims_before_any_login(self):
        """The app signs in from the /register response, which runs no login
        callback: the claim has to come from Synapse storing the address."""
        signup = f"signup+{self.tag}@school.example"
        secret = "claim-by-email-secret"
        register = "/_matrix/client/v3/register"
        await self.homeserver(
            module_config={"app_base_url": APP},
            synapse_config_overrides={
                "email": self.sink.synapse_email_config(),
                "enable_registration": True,
                "registrations_require_3pid": ["email"],
            },
        )
        _, op = await self.new_user("operator", admin=True)
        ident = self.prepare(op, "signup", signup)

        account = {"username": self.localpart("signup"), "password": PASSWORD}
        started = requests.post(self.server_url + register, json=account)
        self.assertEqual(started.status_code, 401, started.text)
        requested = requests.post(
            self.server_url + register + "/email/requestToken",
            json={"client_secret": secret, "email": signup, "send_attempt": 1},
        )
        self.assertEqual(requested.status_code, 200, requested.text)
        sid = requested.json()["sid"]
        validation = self.sink.wait_for(signup, "Validate")
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
