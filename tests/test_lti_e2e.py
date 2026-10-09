"""End-to-end tests for the LTI 1.3 tool core.

A real Synapse with the module, and a stand-in platform served over HTTPS from
a throwaway CA (trusted through SSL_CERT_FILE), so registration, the JWKS
fetch, login initiation, the launch and operator approval all run through the
module's real HTTP client, store and resources.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import psycopg2
import requests

from .base_e2e import BaseSynapseE2ETest
from .lti_platform_double import (
    INSTRUCTOR,
    HttpsPlatformServer,
    new_rsa_key,
    private_pem,
    public_jwk,
)
from .test_logcontext_e2e import LEAK_MARKERS

BASE = "http://localhost:8008/_synapse/client/pangea/v1/lti"
LAUNCH_URL = BASE + "/launch"
TOOL_KEY = new_rsa_key()
EMAIL = "student.private@school.example"

_SYNAPSE_CONFIG: Dict[str, Any] = {
    "public_baseurl": "http://localhost:8008/",
    # The stand-in platform runs on loopback, which Synapse's outbound
    # blocklist refuses by default.
    "ip_range_whitelist": ["127.0.0.1/32", "::1/128"],
    "rc_login": {"address": {"per_second": 9999, "burst_count": 9999}},
}


class LtiEndpointsE2ETest(BaseSynapseE2ETest):
    async def _start(self, module_config: Dict[str, Any]) -> Tuple[Any, ...]:
        return await self.start_test_synapse(
            module_config=module_config,
            synapse_config_overrides=_SYNAPSE_CONFIG,
        )

    def _stop(self, parts: Tuple[Any, ...]) -> None:
        if not parts:
            return
        postgres, synapse_dir, _, server_process, stdout_thread, stderr_thread = parts
        self.stop_synapse(
            server_process=server_process,
            stdout_thread=stdout_thread,
            stderr_thread=stderr_thread,
            synapse_dir=synapse_dir,
            postgres=postgres,
        )

    def _logs(self) -> str:
        return "\n".join(self.server_stdout_lines + self.server_stderr_lines)

    def _assert_lti_absent(self) -> None:
        for path in ("/jwks", "/login", "/launch", "/register", "/platforms"):
            response = requests.get(BASE + path, timeout=10)
            self.assertEqual(response.status_code, 404, path)

    async def test_lti_endpoints_refuse_to_start_without_a_usable_key(self) -> None:
        """`module refuses to start the LTI endpoints when the Secrets
        Manager-backed private key is absent or malformed, and never generates
        or accepts an inline fallback key` (INV-8). Synapse itself still
        serves; only the LTI paths are missing."""
        short_key = private_pem(new_rsa_key(1024))
        for lti_config in (
            {},
            {"private_key_pem": "-----BEGIN PRIVATE KEY-----\nAAAA\n"},
            {"private_key_pem": short_key},
        ):
            parts: Tuple[Any, ...] = ()
            try:
                parts = await self._start({"lti": lti_config})
                self._assert_lti_absent()
                health = requests.get("http://localhost:8008/health", timeout=10)
                self.assertEqual(health.status_code, 200)
                logs = self._logs()
                self.assertIn("LTI endpoints not started", logs)
                self.assertNotIn(short_key.splitlines()[1], logs)
            finally:
                self._stop(parts)

    async def test_lti_endpoints_absent_when_not_configured(self) -> None:
        parts: Tuple[Any, ...] = ()
        try:
            parts = await self._start({})
            self._assert_lti_absent()
        finally:
            self._stop(parts)

    async def test_registration_approval_login_and_launch(self) -> None:
        parts: Tuple[Any, ...] = ()
        previous_ca = os.environ.get("SSL_CERT_FILE")
        with HttpsPlatformServer() as platform_server:
            os.environ["SSL_CERT_FILE"] = platform_server.ca_path
            try:
                parts = await self._start(
                    {"lti": {"private_key_pem": private_pem(TOOL_KEY)}}
                )
                await self._flow(parts, platform_server)
            finally:
                if previous_ca is None:
                    os.environ.pop("SSL_CERT_FILE", None)
                else:
                    os.environ["SSL_CERT_FILE"] = previous_ca
                self._stop(parts)

    # -- the flow ---------------------------------------------------------

    async def _flow(self, parts: Tuple[Any, ...], server: HttpsPlatformServer) -> None:
        _, synapse_dir, config_path, _, _, _ = parts
        double = server.platform

        # `jwks publishes only the public half of the configured key`.
        jwks = requests.get(BASE + "/jwks", timeout=10)
        self.assertEqual(jwks.status_code, 200)
        [published] = jwks.json()["keys"]
        self.assertEqual(set(published), {"kty", "kid", "alg", "use", "n", "e"})
        self.assertEqual(published["n"], public_jwk(TOOL_KEY, "x")["n"])

        static = requests.get(BASE + "/config", timeout=10)
        self.assertEqual(static.status_code, 200)
        self.assertEqual(
            static.json()["extensions"][0]["settings"]["placements"][0]["placement"],
            "course_navigation",
        )

        # Redirects are never followed: the issuer-host check is against the
        # URL the document was fetched from, so a redirect would let any
        # document claim the redirecting host's issuer.
        redirected = requests.get(
            BASE + "/register",
            params={
                "openid_configuration": server.base_url + "/redirect",
                "registration_token": server.registration_token,
            },
            timeout=30,
        )
        self.assertEqual(redirected.status_code, 400, redirected.text)
        self.assertIn("configuration_status_302", redirected.text)
        self.assertEqual(server.registrations, [])

        # Dynamic Registration lands the platform as pending.
        registered = requests.get(
            BASE + "/register",
            params={
                "openid_configuration": server.base_url
                + "/.well-known/openid-configuration",
                "registration_token": server.registration_token,
            },
            timeout=30,
        )
        self.assertEqual(registered.status_code, 200, registered.text)
        self.assertIn("org.imsglobal.lti.close", registered.text)
        [sent] = server.registrations
        self.assertEqual(sent["initiate_login_uri"], BASE + "/login")
        self.assertEqual(sent["redirect_uris"], [LAUNCH_URL])
        self.assertEqual(sent["jwks_uri"], BASE + "/jwks")

        # `registration checks openid config host`: an issuer on another host
        # than the configuration URL is refused and nothing is stored.
        server.issuer_override = "https://evil.example"
        mismatched = requests.get(
            BASE + "/register",
            params={
                "openid_configuration": server.base_url
                + "/.well-known/openid-configuration",
                "registration_token": server.registration_token,
            },
            timeout=30,
        )
        server.issuer_override = None
        self.assertEqual(mismatched.status_code, 400, mismatched.text)
        self.assertEqual(len(server.registrations), 1)

        await self.register_user(config_path, synapse_dir, "operator", "pw-op", True)
        await self.register_user(config_path, synapse_dir, "teacher", "pw-t", False)
        _, admin_token = await self.login_user("operator", "pw-op")
        _, user_token = await self.login_user("teacher", "pw-t")

        listed = self._get_platforms(admin_token)
        self.assertEqual(listed.status_code, 200, listed.text)
        [row] = listed.json()["platforms"]
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["issuer"], server.base_url)
        self.assertEqual(row["client_id"], double.client_id)
        self.assertEqual(row["deployment_ids"], [double.deployment_id])
        platform_id = row["platform_id"]
        self.assertEqual(self._get_platforms(user_token).status_code, 403)
        self.assertEqual(self._get_platforms(None).status_code, 401)

        # `pending platform launch refused` (INV-8), first at login.
        pending_login = self._login(server)
        self.assertEqual(pending_login.status_code, 403, pending_login.text)
        self.assertEqual(
            pending_login.json()["errcode"], "ORG.PANGEA.LTI_PLATFORM_NOT_APPROVED"
        )

        # `approve endpoint refuses non-server-admin`.
        self.assertEqual(self._approve(platform_id, user_token).status_code, 403)
        self.assertEqual(self._approve(platform_id, None).status_code, 401)
        self.assertEqual(
            self._approve("no-such-platform", admin_token).status_code, 404
        )
        approved = self._approve(platform_id, admin_token)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["state"], "approved")
        self.assertEqual(self._approve(platform_id, admin_token).status_code, 200)

        # `approved platform launch accepted`.
        state, nonce, cookie = self._login_ok(server)
        token = double.sign(double.claims(nonce=nonce, launch_url=LAUNCH_URL))
        launched = self._launch(token, state, cookie)
        self.assertEqual(launched.status_code, 501, launched.text)
        self.assertEqual(launched.json()["path"], "learner")
        self.assertEqual(server.jwks_requests, 1)

        # A reused state (and so its nonce) is refused.
        replay = self._launch(token, state, cookie)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.json()["reason"], "unknown_state")

        # A state presented without this browser's cookie is refused.
        state, nonce, cookie = self._login_ok(server)
        token = double.sign(double.claims(nonce=nonce, launch_url=LAUNCH_URL))
        no_cookie = self._launch(token, state, None)
        self.assertEqual(no_cookie.status_code, 400)
        self.assertEqual(no_cookie.json()["reason"], "state_mismatch")
        wrong_cookie = self._launch(token, state, (cookie[0], "x" + cookie[1]))
        self.assertEqual(wrong_cookie.json()["reason"], "state_mismatch")
        # ...and the state still works for its own browser, once.
        self.assertEqual(self._launch(token, state, cookie).status_code, 501)

        # A token minted for another login's nonce is refused.
        state_a, _, cookie_a = self._login_ok(server)
        _, nonce_b, _ = self._login_ok(server)
        cross = double.sign(double.claims(nonce=nonce_b, launch_url=LAUNCH_URL))
        crossed = self._launch(cross, state_a, cookie_a)
        self.assertEqual(crossed.status_code, 400)
        self.assertEqual(crossed.json()["reason"], "bad_nonce")

        # An instructor launch takes the connect path.
        state, nonce, cookie = self._login_ok(server)
        token = double.sign(
            double.claims(nonce=nonce, launch_url=LAUNCH_URL, roles=[INSTRUCTOR])
        )
        self.assertEqual(
            self._launch(token, state, cookie).json()["path"], "instructor"
        )

        # A launch for a deployment the platform never registered is refused.
        state, nonce, cookie = self._login_ok(server)
        claims = double.claims(nonce=nonce, launch_url=LAUNCH_URL)
        claims["https://purl.imsglobal.org/spec/lti/claim/deployment_id"] = "dep-x"
        unknown = self._launch(double.sign(claims), state, cookie)
        self.assertEqual(unknown.json()["reason"], "unknown_deployment")

        # A platform that is no longer approved by the time the launch arrives
        # is refused at the launch too, not only at login.
        state, nonce, cookie = self._login_ok(server)
        self._set_platform_state(platform_id, "pending")
        token = double.sign(double.claims(nonce=nonce, launch_url=LAUNCH_URL))
        refused = self._launch(token, state, cookie)
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(refused.json()["reason"], "platform_not_approved")
        self._set_platform_state(platform_id, "approved")

        # A key the platform never published is not fetched again and again.
        state, nonce, cookie = self._login_ok(server)
        rogue = double.sign(
            double.claims(nonce=nonce, launch_url=LAUNCH_URL),
            headers={"kid": "unknown-kid"},
        )
        before = server.jwks_requests
        self.assertEqual(
            self._launch(rogue, state, cookie).json()["reason"], "unknown_key"
        )
        state, nonce, cookie = self._login_ok(server)
        rogue = double.sign(
            double.claims(nonce=nonce, launch_url=LAUNCH_URL),
            headers={"kid": "unknown-kid"},
        )
        self._launch(rogue, state, cookie)
        self.assertLessEqual(server.jwks_requests - before, 1)

        # The resources, the store and the shared JWKS fetch hand their
        # logcontext back correctly (the same markers as test_logcontext_e2e).
        await asyncio.sleep(2)
        leaked = [
            line
            for line in self.server_stdout_lines + self.server_stderr_lines
            if any(marker in line for marker in LEAK_MARKERS)
        ]
        self.assertEqual(leaked, [], "\n".join(leaked))

        # `approval logged without keys or emails` and `no key or email in
        # logs` (INV-8).
        logs = self._logs()
        self.assertIn("LTI platform approved", logs)
        # The registration token arrives in the query string (the Dynamic
        # Registration spec puts it there). Synapse's INFO access log records
        # the URI when the request finishes, so it must be redacted by then.
        # (Synapse's own DEBUG "Received request" line is written before any
        # module code runs and is out of the module's reach.)
        info_and_above = "\n".join(
            line for line in logs.splitlines() if " - DEBUG - " not in line
        )
        self.assertIn("/lti/register?<redacted>", info_and_above)
        self.assertNotIn(server.registration_token, info_and_above)
        self.assertNotIn(EMAIL, logs)
        self.assertNotIn(token.split(".")[1], logs)
        for line in private_pem(TOOL_KEY).splitlines()[1:-1]:
            self.assertNotIn(line, logs)

    # -- helpers ------------------------------------------------------------

    def _headers(self, token: Optional[str]) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _get_platforms(self, token: Optional[str]) -> requests.Response:
        return requests.get(
            BASE + "/platforms", headers=self._headers(token), timeout=10
        )

    def _approve(self, platform_id: str, token: Optional[str]) -> requests.Response:
        return requests.post(
            f"{BASE}/platforms/{platform_id}/approve",
            json={},
            headers=self._headers(token),
            timeout=10,
        )

    def _login(self, server: HttpsPlatformServer) -> requests.Response:
        return requests.get(
            BASE + "/login",
            params={
                "iss": server.base_url,
                "login_hint": "opaque-hint-1",
                "lti_message_hint": "opaque-message-hint",
                "target_link_uri": LAUNCH_URL,
                "client_id": server.platform.client_id,
                "lti_deployment_id": server.platform.deployment_id,
            },
            allow_redirects=False,
            timeout=10,
        )

    def _login_ok(
        self, server: HttpsPlatformServer
    ) -> Tuple[str, str, Tuple[str, str]]:
        response = self._login(server)
        self.assertEqual(response.status_code, 302, response.text)
        location = urlparse(response.headers["Location"])
        self.assertEqual(
            f"{location.scheme}://{location.netloc}{location.path}",
            server.base_url + "/authorize",
        )
        query = {k: v[0] for k, v in parse_qs(location.query).items()}
        self.assertEqual(query["scope"], "openid")
        self.assertEqual(query["response_type"], "id_token")
        self.assertEqual(query["response_mode"], "form_post")
        self.assertEqual(query["prompt"], "none")
        self.assertEqual(query["client_id"], server.platform.client_id)
        self.assertEqual(query["redirect_uri"], LAUNCH_URL)
        self.assertEqual(query["login_hint"], "opaque-hint-1")
        self.assertEqual(query["lti_message_hint"], "opaque-message-hint")
        set_cookie = response.headers["Set-Cookie"]
        for attribute in ("HttpOnly", "Secure", "SameSite=None", "Max-Age=600"):
            self.assertIn(attribute, set_cookie)
        name, _, rest = set_cookie.partition("=")
        value = rest.split(";", 1)[0]
        self.assertEqual(value, query["state"])
        return query["state"], query["nonce"], (name, value)

    def _launch(
        self, id_token: str, state: str, cookie: Optional[Tuple[str, str]]
    ) -> requests.Response:
        headers = {}
        if cookie is not None:
            headers["Cookie"] = f"{cookie[0]}={cookie[1]}"
        return requests.post(
            LAUNCH_URL,
            data={"id_token": id_token, "state": state},
            headers=headers,
            allow_redirects=False,
            timeout=30,
        )

    def _set_platform_state(self, platform_id: str, state: str) -> None:
        connection = psycopg2.connect(self.database_url)
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE lti_platform SET state = %s WHERE platform_id = %s",
                    (state, platform_id),
                )
        finally:
            connection.close()
