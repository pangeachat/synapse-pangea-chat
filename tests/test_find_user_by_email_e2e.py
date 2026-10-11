"""Integration tests for the find_user_by_email endpoint.

Uses ``BaseSynapseE2ETest`` to spin up a local Synapse + PostgreSQL instance
with the ``PangeaChat`` module loaded.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests

from .base_e2e import BaseSynapseE2ETest

logger = logging.getLogger(__name__)

_SYNAPSE_CONFIG = {
    "rc_login": {
        "address": {"per_second": 9999, "burst_count": 9999},
    },
}


def _module_config(
    find_user_by_email_requests_per_burst: int = 100,
    find_user_by_email_burst_duration_seconds: int = 60,
) -> dict:
    return {
        "find_user_by_email_requests_per_burst": find_user_by_email_requests_per_burst,
        "find_user_by_email_burst_duration_seconds": find_user_by_email_burst_duration_seconds,
    }


class FindUserByEmailHelpers:
    """Helpers shared by the test classes, one class per module config."""

    # ── helpers ──────────────────────────────────────────────────────

    def _endpoint(self) -> str:
        return f"{self.server_url}/_synapse/client/pangea/v1/find_user_by_email"

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
        return requests.post(
            self._endpoint(), json={"address": address}, headers=headers
        )

    def _lookup_ok(self, address: str, access_token: str) -> Dict[str, Any]:
        resp = self._lookup(address, access_token)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _admin_update_user(
        self,
        *,
        admin_access_token: str,
        target_user_id: str,
        body: Dict[str, Any],
    ) -> None:
        encoded_user_id = quote(target_user_id, safe="")
        resp = requests.put(
            f"{self.server_url}/_synapse/admin/v2/users/{encoded_user_id}",
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


class TestFindUserByEmailEndpoint(FindUserByEmailHelpers, BaseSynapseE2ETest):
    """Tests for ``POST /_synapse/client/pangea/v1/find_user_by_email``.

    An address belongs to at most one account server-wide, so each test binds
    addresses carrying its own tag.
    """

    # ── tests ────────────────────────────────────────────────────────

    async def test_admin_resolves_account_by_email(self) -> None:
        await self.homeserver(
            module_config=_module_config(),
            synapse_config_overrides=_SYNAPSE_CONFIG,
        )

        (_, admin_token) = await self.new_user("rootadmin", admin=True)
        (teacher_id, teacher_token) = await self.new_user("teacher")
        tag = self.tag

        self._bind_email(
            admin_access_token=admin_token,
            target_user_id=teacher_id,
            address=f"Person.{tag}@School.edu",
        )
        display_resp = requests.put(
            f"{self.server_url}/_matrix/client/v3/profile/{quote(teacher_id, safe='')}/displayname",
            json={"displayname": "Jane Doe"},
            headers={"Authorization": f"Bearer {teacher_token}"},
        )
        self.assertEqual(display_resp.status_code, 200, display_resp.text)

        # The spelling the address was bound under matches. Synapse
        # lower-cases addresses when binding, so that is what comes back.
        data = self._lookup_ok(f"Person.{tag}@School.edu", admin_token)
        self.assertEqual(data["address"], f"Person.{tag}@School.edu")
        self.assertEqual(len(data["results"]), 1, data)
        match = data["results"][0]
        self.assertEqual(match["user_id"], teacher_id)
        self.assertEqual(match["address"], f"person.{tag}@school.edu")
        self.assertEqual(match["display_name"], "Jane Doe")
        self.assertFalse(match["deactivated"])

        # Any other casing of the query matches the same account.
        data = self._lookup_ok(f"person.{tag}@SCHOOL.EDU", admin_token)
        self.assertEqual(
            [r["user_id"] for r in data["results"]],
            [teacher_id],
            data,
        )

        # Surrounding whitespace is trimmed.
        data = self._lookup_ok(f"  person.{tag}@school.edu  ", admin_token)
        self.assertEqual([r["user_id"] for r in data["results"]], [teacher_id])

        # An address nobody registered is an empty list, not an error.
        data = self._lookup_ok(f"nobody.{tag}@school.edu", admin_token)
        self.assertEqual(data["results"], [])

        # Exact match only — no domain or substring search.
        for probe in ("school.edu@school.edu", f"erson.{tag}@school.edu"):
            with self.subTest(probe=probe):
                self.assertEqual(self._lookup_ok(probe, admin_token)["results"], [])

    async def test_address_maps_to_one_account_and_deactivation_clears_it(self) -> None:
        await self.homeserver(
            module_config=_module_config(),
            synapse_config_overrides=_SYNAPSE_CONFIG,
        )

        (_, admin_token) = await self.new_user("rootadmin", admin=True)
        (first_id, _) = await self.new_user("dupe1")
        (second_id, _) = await self.new_user("dupe2")
        shared = f"shared.{self.tag}@school.edu"

        self._bind_email(
            admin_access_token=admin_token,
            target_user_id=first_id,
            address=shared,
        )
        data = self._lookup_ok(shared, admin_token)
        self.assertEqual([r["user_id"] for r in data["results"]], [first_id], data)

        # Synapse allows one account per address, so binding the same
        # address to a second account moves it off the first rather than
        # producing two matches.
        self._bind_email(
            admin_access_token=admin_token,
            target_user_id=second_id,
            address=f"SHARED.{self.tag}@school.edu",
        )

        data = self._lookup_ok(shared, admin_token)
        self.assertEqual([r["user_id"] for r in data["results"]], [second_id], data)

        # Synapse drops an account's threepid bindings on deactivation, so
        # a closed account falls out of this lookup entirely. "No match"
        # means "no live account", not "never signed up".
        self._admin_update_user(
            admin_access_token=admin_token,
            target_user_id=second_id,
            body={"deactivated": True},
        )

        self.assertEqual(self._lookup_ok(shared, admin_token)["results"], [])

    async def test_access_control_and_validation(self) -> None:
        await self.homeserver(
            module_config=_module_config(),
            synapse_config_overrides=_SYNAPSE_CONFIG,
        )

        (_, admin_token) = await self.new_user("rootadmin", admin=True)
        (target_id, _) = await self.new_user("target")
        (_, plain_token) = await self.new_user("plainuser")
        target = f"target.{self.tag}@school.edu"

        self._bind_email(
            admin_access_token=admin_token,
            target_user_id=target_id,
            address=target,
        )

        # No token at all.
        self.assertEqual(self._lookup(target, None).status_code, 401)

        # A logged-in non-admin is refused even though the address exists,
        # so the refusal itself leaks nothing.
        resp = self._lookup(target, plain_token)
        self.assertEqual(resp.status_code, 403, resp.text)
        self.assertNotIn(target_id, resp.text)

        # ...and equally refused for an address that does not exist.
        self.assertEqual(
            self._lookup(f"nobody.{self.tag}@school.edu", plain_token).status_code,
            403,
        )

        # Malformed addresses are rejected before any lookup happens.
        for bad in ("", "   ", "not-an-email", "a@b@c", 42, None):
            with self.subTest(bad=bad):
                self.assertEqual(self._lookup(bad, admin_token).status_code, 400)

        # Non-object body.
        resp = requests.post(
            self._endpoint(),
            json=[target],
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)


class TestFindUserByEmailRateLimit(FindUserByEmailHelpers, BaseSynapseE2ETest):
    """The endpoint with a per-caller budget of three calls a minute."""

    async def test_rate_limit_applies_per_caller(self) -> None:
        await self.homeserver(
            module_config=_module_config(
                find_user_by_email_requests_per_burst=3,
                find_user_by_email_burst_duration_seconds=60,
            ),
            synapse_config_overrides=_SYNAPSE_CONFIG,
        )

        (_, admin_token) = await self.new_user("rootadmin", admin=True)
        (_, other_admin_token) = await self.new_user("otheradmin", admin=True)

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
