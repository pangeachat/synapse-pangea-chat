"""Integration tests for the find_user_by_email endpoint.

Uses ``BaseSynapseE2ETest`` to spin up a local Synapse + PostgreSQL instance
with the ``PangeaChat`` module loaded.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import requests

from .base_e2e import BaseSynapseE2ETest

logger = logging.getLogger(__name__)

_SYNAPSE_CONFIG = {
    "rc_login": {
        "address": {"per_second": 9999, "burst_count": 9999},
    },
}

_ENDPOINT = "http://localhost:8008/_synapse/client/pangea/v1/find_user_by_email"
_ADMIN_USERS_API = "http://localhost:8008/_synapse/admin/v2/users"


def _module_config(
    find_user_by_email_requests_per_burst: int = 100,
    find_user_by_email_burst_duration_seconds: int = 60,
) -> dict:
    return {
        "find_user_by_email_requests_per_burst": find_user_by_email_requests_per_burst,
        "find_user_by_email_burst_duration_seconds": find_user_by_email_burst_duration_seconds,
    }


class TestFindUserByEmailEndpoint(BaseSynapseE2ETest):
    """Tests for ``POST /_synapse/client/pangea/v1/find_user_by_email``."""

    # ── helpers ──────────────────────────────────────────────────────

    def _lookup(
        self,
        address: Any,
        access_token: Optional[str],
    ) -> requests.Response:
        headers = (
            {"Authorization": f"Bearer {access_token}"}
            if access_token is not None
            else {}
        )
        return requests.post(_ENDPOINT, json={"address": address}, headers=headers)

    def _lookup_ok(self, address: str, access_token: str) -> Dict[str, Any]:
        resp = self._lookup(address, access_token)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    async def _register_and_login(
        self,
        config_path: str,
        synapse_dir: str,
        username: str,
        password: str,
        admin: bool = False,
    ) -> Tuple[str, str]:
        await self.register_user(config_path, synapse_dir, username, password, admin)
        return await self.login_user(username, password)

    def _admin_update_user(
        self,
        *,
        admin_access_token: str,
        target_user_id: str,
        body: Dict[str, Any],
    ) -> None:
        encoded_user_id = quote(target_user_id, safe="")
        resp = requests.put(
            f"{_ADMIN_USERS_API}/{encoded_user_id}",
            json=body,
            headers={"Authorization": f"Bearer {admin_access_token}"},
        )
        self.assertIn(resp.status_code, (200, 201), resp.text)

    def _bind_email(
        self, *, admin_access_token: str, target_user_id: str, address: str
    ) -> None:
        self._admin_update_user(
            admin_access_token=admin_access_token,
            target_user_id=target_user_id,
            body={"threepids": [{"medium": "email", "address": address}]},
        )

    # ── tests ────────────────────────────────────────────────────────

    async def test_admin_resolves_account_by_email(self) -> None:
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config=_module_config(),
                synapse_config_overrides=_SYNAPSE_CONFIG,
            )

            (_, admin_token) = await self._register_and_login(
                config_path, synapse_dir, "rootadmin", "adminpass", admin=True
            )
            (teacher_id, teacher_token) = await self._register_and_login(
                config_path, synapse_dir, "teacher", "teacherpass"
            )

            self._bind_email(
                admin_access_token=admin_token,
                target_user_id=teacher_id,
                address="Person@School.edu",
            )
            display_resp = requests.put(
                f"http://localhost:8008/_matrix/client/v3/profile/{quote(teacher_id, safe='')}/displayname",
                json={"displayname": "Jane Doe"},
                headers={"Authorization": f"Bearer {teacher_token}"},
            )
            self.assertEqual(display_resp.status_code, 200, display_resp.text)

            # The spelling the address was bound under matches. Synapse
            # lower-cases addresses when binding, so that is what comes back.
            data = self._lookup_ok("Person@School.edu", admin_token)
            self.assertEqual(data["address"], "Person@School.edu")
            self.assertEqual(len(data["results"]), 1, data)
            match = data["results"][0]
            self.assertEqual(match["user_id"], teacher_id)
            self.assertEqual(match["address"], "person@school.edu")
            self.assertEqual(match["display_name"], "Jane Doe")
            self.assertFalse(match["deactivated"])

            # Any other casing of the query matches the same account.
            data = self._lookup_ok("person@SCHOOL.EDU", admin_token)
            self.assertEqual(
                [r["user_id"] for r in data["results"]],
                [teacher_id],
                data,
            )

            # Surrounding whitespace is trimmed.
            data = self._lookup_ok("  person@school.edu  ", admin_token)
            self.assertEqual([r["user_id"] for r in data["results"]], [teacher_id])

            # An address nobody registered is an empty list, not an error.
            data = self._lookup_ok("nobody@school.edu", admin_token)
            self.assertEqual(data["results"], [])

            # Exact match only — no domain or substring search.
            for probe in ("school.edu@school.edu", "erson@school.edu"):
                with self.subTest(probe=probe):
                    self.assertEqual(self._lookup_ok(probe, admin_token)["results"], [])

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_address_maps_to_one_account_and_deactivation_clears_it(self) -> None:
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config=_module_config(),
                synapse_config_overrides=_SYNAPSE_CONFIG,
            )

            (_, admin_token) = await self._register_and_login(
                config_path, synapse_dir, "rootadmin", "adminpass", admin=True
            )
            (first_id, _) = await self._register_and_login(
                config_path, synapse_dir, "dupe1", "pass1"
            )
            (second_id, _) = await self._register_and_login(
                config_path, synapse_dir, "dupe2", "pass2"
            )

            self._bind_email(
                admin_access_token=admin_token,
                target_user_id=first_id,
                address="shared@school.edu",
            )
            data = self._lookup_ok("shared@school.edu", admin_token)
            self.assertEqual([r["user_id"] for r in data["results"]], [first_id], data)

            # Synapse allows one account per address, so binding the same
            # address to a second account moves it off the first rather than
            # producing two matches.
            self._bind_email(
                admin_access_token=admin_token,
                target_user_id=second_id,
                address="SHARED@school.edu",
            )

            data = self._lookup_ok("shared@school.edu", admin_token)
            self.assertEqual([r["user_id"] for r in data["results"]], [second_id], data)

            # Synapse drops an account's threepid bindings on deactivation, so
            # a closed account falls out of this lookup entirely. "No match"
            # means "no live account", not "never signed up".
            self._admin_update_user(
                admin_access_token=admin_token,
                target_user_id=second_id,
                body={"deactivated": True},
            )

            self.assertEqual(
                self._lookup_ok("shared@school.edu", admin_token)["results"], []
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_access_control_and_validation(self) -> None:
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config=_module_config(),
                synapse_config_overrides=_SYNAPSE_CONFIG,
            )

            (_, admin_token) = await self._register_and_login(
                config_path, synapse_dir, "rootadmin", "adminpass", admin=True
            )
            (target_id, _) = await self._register_and_login(
                config_path, synapse_dir, "target", "targetpass"
            )
            (_, plain_token) = await self._register_and_login(
                config_path, synapse_dir, "plainuser", "plainpass"
            )

            self._bind_email(
                admin_access_token=admin_token,
                target_user_id=target_id,
                address="target@school.edu",
            )

            # No token at all.
            self.assertEqual(self._lookup("target@school.edu", None).status_code, 401)

            # A logged-in non-admin is refused even though the address exists,
            # so the refusal itself leaks nothing.
            resp = self._lookup("target@school.edu", plain_token)
            self.assertEqual(resp.status_code, 403, resp.text)
            self.assertNotIn(target_id, resp.text)

            # ...and equally refused for an address that does not exist.
            self.assertEqual(
                self._lookup("nobody@school.edu", plain_token).status_code, 403
            )

            # Malformed addresses are rejected before any lookup happens.
            for bad in ("", "   ", "not-an-email", "a@b@c", 42, None):
                with self.subTest(bad=bad):
                    self.assertEqual(self._lookup(bad, admin_token).status_code, 400)

            # Non-object body.
            resp = requests.post(
                _ENDPOINT,
                json=["person@school.edu"],
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            self.assertEqual(resp.status_code, 400, resp.text)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_rate_limit_applies_per_caller(self) -> None:
        postgres = synapse_dir = server_process = stdout_thread = stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config=_module_config(
                    find_user_by_email_requests_per_burst=3,
                    find_user_by_email_burst_duration_seconds=60,
                ),
                synapse_config_overrides=_SYNAPSE_CONFIG,
            )

            (_, admin_token) = await self._register_and_login(
                config_path, synapse_dir, "rootadmin", "adminpass", admin=True
            )
            (_, other_admin_token) = await self._register_and_login(
                config_path, synapse_dir, "otheradmin", "otherpass", admin=True
            )

            for _ in range(3):
                self.assertEqual(
                    self._lookup("person@school.edu", admin_token).status_code, 200
                )
            self.assertEqual(
                self._lookup("person@school.edu", admin_token).status_code, 429
            )

            # A different admin still has their own budget.
            self.assertEqual(
                self._lookup("person@school.edu", other_admin_token).status_code, 200
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
