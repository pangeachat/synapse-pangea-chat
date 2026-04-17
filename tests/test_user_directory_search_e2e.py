"""Integration tests for the custom user_directory/search endpoint.

Uses ``BaseSynapseE2ETest`` to spin up a local Synapse + PostgreSQL
instance with the ``PangeaChat`` module loaded.
"""

import asyncio
import logging
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests

from .base_e2e import BaseSynapseE2ETest

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    filename="synapse.log",
    filemode="w",
)

_SYNAPSE_CONFIG = {
    "rc_login": {
        "address": {"per_second": 9999, "burst_count": 9999},
    },
    "user_directory": {
        "enabled": True,
        "search_all_users": True,
        "prefer_local_users": True,
        "show_locked_users": True,
    },
}

_ENDPOINT = "http://localhost:8008/_synapse/client/pangea/v1/user_directory/search"


def _module_config(
    filter_search_if_missing_public_attribute: bool = True,
    user_directory_search_requests_per_burst: int = 10,
    user_directory_search_burst_duration_seconds: int = 60,
) -> dict:
    return {
        "limit_user_directory_public_attribute_search_path": "profile.user_settings.public",
        "limit_user_directory_whitelist_requester_id_patterns": [
            "@whitelisted:my.domain.name"
        ],
        "limit_user_directory_filter_search_if_missing_public_attribute": (
            filter_search_if_missing_public_attribute
        ),
        "user_directory_search_requests_per_burst": user_directory_search_requests_per_burst,
        "user_directory_search_burst_duration_seconds": user_directory_search_burst_duration_seconds,
    }


class TestUserDirectorySearchEndpoint(BaseSynapseE2ETest):
    """Tests for ``POST /_synapse/client/pangea/v1/user_directory/search``."""

    # ── helpers ──────────────────────────────────────────────────────

    def _search(
        self,
        search_term: str,
        access_token: str,
        limit: int = 50,
    ) -> Dict[str, Any]:
        resp = requests.post(
            _ENDPOINT,
            json={"search_term": search_term, "limit": limit},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _search_user_ids(
        self,
        search_term: str,
        access_token: str,
        limit: int = 50,
    ) -> List[str]:
        data = self._search(search_term, access_token, limit)
        return [r["user_id"] for r in data.get("results", [])]

    async def _search_user_ids_with_retry(
        self,
        search_term: str,
        access_token: str,
        *,
        required_user_ids: List[str],
        retries: int = 20,
        delay_seconds: float = 0.5,
    ) -> List[str]:
        last_results: List[str] = []
        required = set(required_user_ids)
        for _ in range(retries):
            last_results = self._search_user_ids(search_term, access_token)
            if required.issubset(set(last_results)):
                return last_results
            await asyncio.sleep(delay_seconds)

        return last_results

    async def _set_public(self, user_id: str, value: bool, access_token: str) -> None:
        get_resp = requests.get(
            f"http://localhost:8008/_matrix/client/v3/user/{user_id}/account_data/profile",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        body: dict = {} if get_resp.status_code == 404 else get_resp.json()
        body.setdefault("user_settings", {})["public"] = value
        put_resp = requests.put(
            f"http://localhost:8008/_matrix/client/v3/user/{user_id}/account_data/profile",
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(put_resp.status_code, 200)

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

    def _set_locked_by_admin(
        self,
        *,
        admin_access_token: str,
        target_user_id: str,
        locked: bool,
    ) -> None:
        encoded_user_id = quote(target_user_id, safe="")
        resp = requests.put(
            f"http://localhost:8008/_synapse/admin/v2/users/{encoded_user_id}",
            json={"locked": locked},
            headers={"Authorization": f"Bearer {admin_access_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    # ── tests ────────────────────────────────────────────────────────

    async def test_public_users_always_visible(self) -> None:
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

            # Create 6 users: 2 private, 2 public, 2 unset
            creds: List[Tuple[str, str]] = []
            for i in range(6):
                creds.append(
                    await self._register_and_login(
                        config_path, synapse_dir, f"usr{i}", f"pass{i}"
                    )
                )

            # usr0, usr1 → private
            await self._set_public(creds[0][0], False, creds[0][1])
            await self._set_public(creds[1][0], False, creds[1][1])
            # usr2, usr3 → public
            await self._set_public(creds[2][0], True, creds[2][1])
            await self._set_public(creds[3][0], True, creds[3][1])
            # usr4, usr5 → no attribute set

            # A regular user (usr0) searches — should see only public users
            results = self._search_user_ids("usr", creds[0][1])
            self.assertIn(creds[2][0], results)
            self.assertIn(creds[3][0], results)
            # Private and unset should NOT be visible
            self.assertNotIn(creds[1][0], results)
            self.assertNotIn(creds[4][0], results)
            self.assertNotIn(creds[5][0], results)
            # Self should never appear
            self.assertNotIn(creds[0][0], results)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_private_user_visible_via_shared_room(self) -> None:
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

            (user_a, token_a) = await self._register_and_login(
                config_path, synapse_dir, "sharedA", "passA"
            )
            (user_b, token_b) = await self._register_and_login(
                config_path, synapse_dir, "sharedB", "passB"
            )

            await self._set_public(user_a, False, token_a)
            await self._set_public(user_b, False, token_b)

            # Before sharing a room, B should NOT appear for A
            results_before = self._search_user_ids("sharedB", token_a)
            self.assertNotIn(user_b, results_before)

            # Create a private room and invite B
            resp = requests.post(
                "http://localhost:8008/_matrix/client/v3/createRoom",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"preset": "private_chat", "is_direct": True},
            )
            self.assertEqual(resp.status_code, 200)
            room_id = resp.json()["room_id"]

            requests.post(
                f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/invite",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"user_id": user_b},
            )
            requests.post(
                f"http://localhost:8008/_matrix/client/v3/join/{room_id}",
                headers={"Authorization": f"Bearer {token_b}"},
            )

            # Now B should appear for A
            results_after = self._search_user_ids("sharedB", token_a)
            self.assertIn(user_b, results_after)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_whitelisted_requester_sees_all(self) -> None:
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

            creds: List[Tuple[str, str]] = []
            for i in range(4):
                creds.append(
                    await self._register_and_login(
                        config_path, synapse_dir, f"vis{i}", f"pass{i}"
                    )
                )
            # vis0 public, vis1 private, vis2/vis3 unset
            await self._set_public(creds[0][0], True, creds[0][1])
            await self._set_public(creds[1][0], False, creds[1][1])

            # Register whitelisted admin
            (wl_user, wl_token) = await self._register_and_login(
                config_path, synapse_dir, "whitelisted", "pass", admin=True
            )

            results = self._search_user_ids("vis", wl_token)
            # Whitelisted sees ALL four users
            for uid, _ in creds:
                self.assertIn(uid, results)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_self_excluded(self) -> None:
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

            (user_id, token) = await self._register_and_login(
                config_path, synapse_dir, "selftest", "pass"
            )
            await self._set_public(user_id, True, token)

            results = self._search_user_ids("selftest", token)
            self.assertNotIn(user_id, results)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_missing_attribute_not_filtered_when_config_false(self) -> None:
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
                    filter_search_if_missing_public_attribute=False,
                    user_directory_search_requests_per_burst=120,
                ),
                synapse_config_overrides=_SYNAPSE_CONFIG,
            )

            (pub_user, pub_token) = await self._register_and_login(
                config_path, synapse_dir, "pubflag", "pass"
            )
            (miss_user, miss_token) = await self._register_and_login(
                config_path, synapse_dir, "missflag", "pass"
            )
            (searcher, searcher_token) = await self._register_and_login(
                config_path, synapse_dir, "searcher", "pass"
            )

            await self._set_public(pub_user, True, pub_token)
            await self._set_public(searcher, True, searcher_token)
            # missflag: no public attribute set

            pub_results = await self._search_user_ids_with_retry(
                "pubflag",
                searcher_token,
                required_user_ids=[pub_user],
            )
            self.assertIn(pub_user, pub_results)

            miss_results = await self._search_user_ids_with_retry(
                "missflag",
                searcher_token,
                required_user_ids=[miss_user],
            )
            self.assertIn(miss_user, miss_results)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_limit_respected(self) -> None:
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

            # Create 5 public users
            for i in range(5):
                (uid, tok) = await self._register_and_login(
                    config_path, synapse_dir, f"lim{i}", f"pass{i}"
                )
                await self._set_public(uid, True, tok)

            (searcher, s_token) = await self._register_and_login(
                config_path, synapse_dir, "srch", "pass"
            )
            await self._set_public(searcher, True, s_token)

            data = self._search("lim", s_token, limit=2)
            self.assertEqual(len(data["results"]), 2)
            self.assertTrue(data["limited"])

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_auth_required(self) -> None:
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

            resp = requests.post(
                _ENDPOINT,
                json={"search_term": "test"},
            )
            self.assertEqual(resp.status_code, 403)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_empty_search_returns_empty(self) -> None:
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

            (uid, token) = await self._register_and_login(
                config_path, synapse_dir, "emptytest", "pass"
            )

            resp = requests.post(
                _ENDPOINT,
                json={"search_term": ""},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(resp.status_code, 400)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_locked_user_visibility_respects_synapse_config(self) -> None:
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

            (admin_user, admin_token) = await self._register_and_login(
                config_path,
                synapse_dir,
                "adminlock",
                "pass",
                admin=True,
            )
            (target_user, target_token) = await self._register_and_login(
                config_path,
                synapse_dir,
                "lockedtarget",
                "pass",
            )
            (searcher_user, searcher_token) = await self._register_and_login(
                config_path,
                synapse_dir,
                "lockedsearcher",
                "pass",
            )

            # Make users public so visibility is not blocked by profile-sharing rules.
            await self._set_public(admin_user, True, admin_token)
            await self._set_public(target_user, True, target_token)
            await self._set_public(searcher_user, True, searcher_token)

            # Lock target via Synapse admin endpoint.
            self._set_locked_by_admin(
                admin_access_token=admin_token,
                target_user_id=target_user,
                locked=True,
            )

            # _SYNAPSE_CONFIG sets show_locked_users=True, matching Synapse behavior.
            # Locked users should still be returned in search results.
            results = self._search_user_ids("lockedtarget", searcher_token)
            self.assertIn(target_user, results)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
