"""The Canvas hand-offs end to end (lanes B2, B3): a real Synapse with the
module, real Postgres, and a stand-in Canvas over HTTPS (throwaway CA) that
registers, signs launches, issues NRPS tokens and serves a paged roster.

One flow, in order: registration and approval; an instructor's first launch,
own sign-in and link; the connect to a course they administer; the status
read; the roster import through the module's real HTTP client; a student's
first launch, own sign-in, confirmation and claim; a later launch's login
token, used once with the standard `m.login.token`. Then the logs.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

import jwt
import psycopg2
import requests

from synapse_pangea_chat.config import MANAGED_DISCLOSURE_VERSION

from .base_e2e import BaseSynapseE2ETest
from .lti_platform_double import (
    INSTRUCTOR,
    LEARNER,
    HttpsPlatformServer,
    new_rsa_key,
    private_pem,
)
from .test_logcontext_e2e import LEAK_MARKERS

P = "/_synapse/client/pangea/v1/"
LTI = P + "lti/"
BASE = "http://localhost:8008"
LAUNCH_URL = BASE + "/" + LTI.lstrip("/") + "launch"
APP = "https://app.example.test"
DASH = "https://admin.example.test"
TOOL_KEY = new_rsa_key()
STUDENT_SUB = "canvas-student-sub-31"
TEACHER_SUB = "canvas-teacher-sub-8"
CANVAS_EMAIL = "canvas.reported@school.example"
NO_EMAIL_SUB = "canvas-noemail-sub-55"
V = MANAGED_DISCLOSURE_VERSION
LIMITS = {
    "rc_login": {
        "address": {"per_second": 9999, "burst_count": 9999},
        "account": {"per_second": 9999, "burst_count": 9999},
    },
    "rc_joins": {"local": {"per_second": 9999, "burst_count": 9999}},
    "rc_message": {"per_second": 9999, "burst_count": 9999},
}


class CanvasRosterE2ETest(BaseSynapseE2ETest):
    # -- helpers ------------------------------------------------------------

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
            allow_redirects=False,
            timeout=60,
        )

    def ok(self, method: str, path: str, token: Optional[str], **kw: Any) -> Any:
        response = self.call(method, path, token, **kw)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def user(self, name: str, admin: bool = False) -> str:
        await self.register_user(
            self.config_path, self.synapse_dir, name, name + "-pw-123", admin
        )
        self.users[name], self.tokens[name] = await self.login_user(
            name, name + "-pw-123"
        )
        return self.tokens[name]

    def course(self, owner: str) -> str:
        room = self.ok(
            "POST",
            "/_matrix/client/v3/createRoom",
            self.tokens[owner],
            body={
                "name": "Spanish 101",
                "preset": "private_chat",
                "creation_content": {"type": "m.space"},
            },
        )["room_id"]
        self.ok(
            "PUT",
            f"/_matrix/client/v3/rooms/{quote(room, safe='')}/state/m.room.join_rules/",
            self.tokens[owner],
            body={"join_rule": "knock", "access_code": "cl4sscd"},
        )
        return room

    def db(self, sql: str, args: Tuple[Any, ...] = ()) -> List[Tuple[Any, ...]]:
        connection = psycopg2.connect(self.database_url)
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(sql, args)
                return list(cursor.fetchall()) if cursor.description else []
        finally:
            connection.close()

    def launch(
        self, server: HttpsPlatformServer, *, sub: str, roles: List[str]
    ) -> requests.Response:
        login = requests.get(
            self.server_url + "/" + LTI.lstrip("/") + "login",
            params={
                "iss": server.base_url,
                "login_hint": "hint",
                "target_link_uri": LAUNCH_URL,
                "client_id": server.platform.client_id,
                "lti_deployment_id": server.platform.deployment_id,
            },
            allow_redirects=False,
            timeout=10,
        )
        self.assertEqual(login.status_code, 302, login.text)
        query = {
            k: v[0]
            for k, v in parse_qs(urlparse(login.headers["Location"]).query).items()
        }
        name, _, rest = login.headers["Set-Cookie"].partition("=")
        cookie = rest.split(";", 1)[0]
        token = server.platform.sign(
            server.platform.claims(
                nonce=query["nonce"],
                launch_url=LAUNCH_URL,
                roles=roles,
                sub=sub,
                email=CANVAS_EMAIL,
            )
        )
        return requests.post(
            self.server_url + "/" + LTI.lstrip("/") + "launch",
            data={"id_token": token, "state": query["state"]},
            headers={"Cookie": f"{name}={cookie}"},
            allow_redirects=False,
            timeout=30,
        )

    def redirect(self, response: requests.Response) -> Tuple[str, Dict[str, str]]:
        self.assertEqual(response.status_code, 302, response.text)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        location = urlparse(response.headers["Location"])
        query = {k: v[0] for k, v in parse_qs(location.query).items()}
        return f"{location.scheme}://{location.netloc}{location.path}", query

    # -- the flow -------------------------------------------------------------

    async def test_canvas_connect_import_link_and_login_token(self) -> None:
        template_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, template_dir)
        for name in ("course_invite.html", "course_invite.txt"):
            with open(os.path.join(template_dir, name), "w", encoding="utf-8") as f:
                f.write("{{ join_url }}")
        previous_ca = os.environ.get("SSL_CERT_FILE")
        with HttpsPlatformServer() as server:
            os.environ["SSL_CERT_FILE"] = server.ca_path
            try:
                parts = await self.start_test_synapse(
                    module_config={
                        "lti": {"private_key_pem": private_pem(TOOL_KEY)},
                        "app_base_url": APP,
                        "admin_dash_base_url": DASH,
                    },
                    synapse_config_overrides={
                        "public_baseurl": BASE + "/",
                        "ip_range_whitelist": ["127.0.0.1/32", "::1/128"],
                        "templates": {"custom_template_directory": template_dir},
                        **LIMITS,
                    },
                )
                (
                    postgres,
                    self.synapse_dir,
                    self.config_path,
                    process,
                    out,
                    err,
                ) = parts
                self.addCleanup(
                    self.stop_synapse,
                    server_process=process,
                    stdout_thread=out,
                    stderr_thread=err,
                    synapse_dir=self.synapse_dir,
                    postgres=postgres,
                )
                self.users: Dict[str, str] = {}
                self.tokens: Dict[str, str] = {}
                await self._flow(server)
            finally:
                if previous_ca is None:
                    os.environ.pop("SSL_CERT_FILE", None)
                else:
                    os.environ["SSL_CERT_FILE"] = previous_ca

    async def _flow(self, server: HttpsPlatformServer) -> None:
        for name in ("teacher", "student", "outsider"):
            await self.user(name)
        await self.user("root", admin=True)
        room = self.course("teacher")

        # Registration and operator approval (B1).
        registered = requests.get(
            self.server_url + "/" + LTI.lstrip("/") + "register",
            params={
                "openid_configuration": server.base_url
                + "/.well-known/openid-configuration",
                "registration_token": server.registration_token,
            },
            timeout=30,
        )
        self.assertEqual(registered.status_code, 200, registered.text)
        [platform] = self.ok("GET", LTI + "platforms", self.tokens["root"])["platforms"]
        self.ok(
            "POST",
            LTI + f"platforms/{platform['platform_id']}/approve",
            self.tokens["root"],
        )

        # -- Instructor: first launch, own sign-in, link, connect ----------
        target, query = self.redirect(
            self.launch(server, sub=TEACHER_SUB, roles=[INSTRUCTOR])
        )
        self.assertEqual(target, APP + "/lti/link")
        self.assertEqual(set(query), {"ticket", "role"})
        instructor_ticket = query["ticket"]
        no_token = self.call(
            "POST", LTI + "link", None, body={"ticket": instructor_ticket}
        )
        self.assertEqual(no_token.status_code, 401, no_token.text)
        bad_token = self.call(
            "POST", LTI + "link", "not-a-token", body={"ticket": instructor_ticket}
        )
        self.assertEqual(bad_token.status_code, 401, bad_token.text)
        linked = self.ok(
            "POST",
            LTI + "link",
            self.tokens["teacher"],
            body={"ticket": instructor_ticket},
        )
        self.assertEqual(linked["next"], "connect")
        connect_url = urlparse(linked["connect_url"])
        self.assertEqual(
            f"{connect_url.scheme}://{connect_url.netloc}{connect_url.path}",
            DASH + "/canvas-connect",
        )
        connect_ticket = parse_qs(connect_url.query)["ticket"][0]

        status = self.call(
            "GET",
            LTI + "course_status",
            self.tokens["teacher"],
            params={"room_id": room},
        )
        self.assertEqual(status.json(), {"status": "unconnected"}, status.text)
        # Another account cannot complete it (and burns the ticket).
        wrong = self.call(
            "POST",
            LTI + "connect",
            self.tokens["outsider"],
            body={"ticket": connect_ticket, "room_id": room},
        )
        self.assertEqual(wrong.status_code, 403, wrong.text)
        self.assertEqual(wrong.json()["errcode"], "ORG.PANGEA.TICKET_WRONG_ACCOUNT")
        self.assertEqual(self.db("SELECT * FROM lti_course_link"), [])
        # The linked instructor's next launch goes straight to admin-dash.
        target, query = self.redirect(
            self.launch(server, sub=TEACHER_SUB, roles=[INSTRUCTOR])
        )
        self.assertEqual((target, set(query)), (DASH + "/canvas-connect", {"ticket"}))
        connected = self.ok(
            "POST",
            LTI + "connect",
            self.tokens["teacher"],
            body={"ticket": query["ticket"], "room_id": room},
        )
        self.assertEqual(connected, {"status": "connected"})
        self.assertEqual(
            self.ok(
                "GET",
                LTI + "course_status",
                self.tokens["teacher"],
                params={"room_id": room},
            ),
            {"status": "connected"},
        )
        outsider_status = self.call(
            "GET",
            LTI + "course_status",
            self.tokens["outsider"],
            params={"room_id": room},
        )
        self.assertEqual(outsider_status.status_code, 403, outsider_status.text)

        # -- Import: NRPS through the real client, two pages ---------------
        server.nrps_pages = [
            [
                {
                    "user_id": STUDENT_SUB,
                    "roles": [LEARNER],
                    "email": CANVAS_EMAIL,
                    "status": "Active",
                },
                {"user_id": TEACHER_SUB, "roles": [INSTRUCTOR], "email": "t@x.example"},
            ],
            [{"user_id": NO_EMAIL_SUB, "roles": [LEARNER]}],
        ]
        refused = self.call(
            "POST", LTI + "import", self.tokens["outsider"], body={"room_id": room}
        )
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual((server.token_requests, server.nrps_requests), ([], []))
        imported = self.ok(
            "POST", LTI + "import", self.tokens["teacher"], body={"room_id": room}
        )
        self.assertEqual(
            imported,
            {
                "imported": 1,
                "attached": 0,
                "unchanged": 0,
                "conflicts": [],
                "no_email": 1,
            },
        )
        [token_request] = server.token_requests
        self.assertEqual(token_request["grant_type"], "client_credentials")
        tool_jwks = self.ok("GET", LTI + "jwks", None)
        claims = jwt.decode(
            token_request["client_assertion"],
            key=jwt.PyJWK(tool_jwks["keys"][0]).key,
            algorithms=["RS256"],
            audience=server.base_url + "/token",
        )
        self.assertEqual(claims["iss"], server.platform.client_id)
        self.assertEqual(
            [r["path"] for r in server.nrps_requests], ["/nrps", "/nrps?page=2"]
        )
        for request in server.nrps_requests:
            self.assertEqual(request["authorization"], "Bearer " + server.access_token)
            self.assertEqual(
                request["accept"],
                "application/vnd.ims.lti-nrps.v2.membershipcontainer+json",
            )
        [row] = self.db(
            "SELECT id, state, source, lti_user_id FROM pangea_student_invitation"
        )
        invitation_id = row[0]
        self.assertEqual(row[1:], ("invited", "canvas", STUDENT_SUB))

        # -- Student: first launch, own sign-in, confirmation, claim -------
        target, query = self.redirect(
            self.launch(server, sub=STUDENT_SUB, roles=[LEARNER])
        )
        self.assertEqual(target, APP + "/lti/link")
        self.assertEqual(query["course"], "Spanish 1")
        self.assertNotIn("loginToken", query)
        link_ticket = query["ticket"]
        body = {"ticket": link_ticket, "confirmed": True, "disclosure_version": V}
        self.assertEqual(
            self.call("POST", LTI + "link", None, body=body).status_code, 401
        )
        claimed = self.ok("POST", LTI + "link", self.tokens["student"], body=body)
        self.assertEqual(
            claimed,
            {
                "next": "app",
                "claimed": [{"invitation_id": invitation_id, "room_id": room}],
                "login_token": None,
            },
        )
        replay = self.call("POST", LTI + "link", self.tokens["student"], body=body)
        self.assertEqual(replay.status_code, 410, replay.text)
        self.assertIn(
            room,
            self.ok("GET", "/_matrix/client/v3/joined_rooms", self.tokens["student"])[
                "joined_rooms"
            ],
        )
        self.assertEqual(
            self.db(
                "SELECT user_id FROM pangea_managed_account WHERE course_room_id = %s",
                (room,),
            ),
            [(self.users["student"],)],
        )
        # The Canvas email was never bound to the account.
        self.assertEqual(
            self.ok("GET", "/_matrix/client/v3/account/3pid", self.tokens["student"])[
                "threepids"
            ],
            [],
        )
        self.assertEqual(
            sorted(
                self.db(
                    "SELECT auth_provider, external_id, user_id FROM user_external_ids"
                )
            ),
            sorted(
                [
                    ("lti:" + server.base_url, TEACHER_SUB, self.users["teacher"]),
                    ("lti:" + server.base_url, STUDENT_SUB, self.users["student"]),
                ]
            ),
        )

        # -- Later launch: a single-use login token ------------------------
        target, query = self.redirect(
            self.launch(server, sub=STUDENT_SUB, roles=[LEARNER])
        )
        self.assertEqual((target, set(query)), (APP + "/lti/token", {"loginToken"}))
        login = {"type": "m.login.token", "token": query["loginToken"]}
        first = self.call("POST", "/_matrix/client/v3/login", None, body=login)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["user_id"], self.users["student"])
        second = self.call("POST", "/_matrix/client/v3/login", None, body=login)
        self.assertEqual(second.status_code, 403, second.text)

        # -- A server admin never gets a launch login token -----------------
        promoted = self.call(
            "PUT",
            "/_synapse/admin/v2/users/" + quote(self.users["student"], safe=""),
            self.tokens["root"],
            body={"admin": True},
        )
        self.assertEqual(promoted.status_code, 200, promoted.text)
        admin_launch = self.launch(server, sub=STUDENT_SUB, roles=[LEARNER])
        self.assertEqual(admin_launch.status_code, 302, admin_launch.text)
        self.assertEqual(admin_launch.headers["Location"], APP + "/home/login")

        # -- Logs ------------------------------------------------------------
        logs = "\n".join(self.server_stdout_lines + self.server_stderr_lines)
        self.assertIn("LTI roster imported", logs)
        self.assertIn("LTI login token refused: server admin", logs)
        self.assertIn("/lti/course_status?<redacted>", logs)
        beyond_request_lines = "\n".join(
            line for line in logs.splitlines() if "Received request:" not in line
        )
        for secret in (
            CANVAS_EMAIL,
            "t@x.example",
            STUDENT_SUB,
            TEACHER_SUB,
            NO_EMAIL_SUB,
            instructor_ticket,
            connect_ticket,
            link_ticket,
            query["loginToken"],
            server.access_token,
            token_request["client_assertion"],
        ):
            self.assertNotIn(secret, beyond_request_lines)
        for line in private_pem(TOOL_KEY).splitlines()[1:-1]:
            self.assertNotIn(line, logs)
        leaked = [
            line for line in logs.splitlines() if any(m in line for m in LEAK_MARKERS)
        ]
        self.assertEqual(leaked, [], "\n".join(leaked))
