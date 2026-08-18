"""Integration tests for the blocked join gate against a real local Synapse.

Design: .github/instructions/blocked-join-gate.instructions.md
"""

import logging
from typing import Any, Dict, cast

import requests

from .base_e2e import BaseSynapseE2ETest

logger = logging.getLogger(__name__)

PASSWORD = "123123123"
ACCESS_CODE = "blk1234"

# Setup issues a burst of invites/joins/knocks; lift the default ratelimits so
# throttling can't masquerade as a gate refusal.
SYNAPSE_CONFIG_OVERRIDES = {
    "rc_message": {"per_second": 1000, "burst_count": 100000},
    "rc_invites": {
        "per_room": {"per_second": 1000, "burst_count": 100000},
        "per_user": {"per_second": 1000, "burst_count": 100000},
        "per_issuer": {"per_second": 1000, "burst_count": 100000},
    },
    "rc_joins": {
        "local": {"per_second": 1000, "burst_count": 100000},
        "remote": {"per_second": 1000, "burst_count": 100000},
    },
}


class TestBlockedJoinGateE2E(BaseSynapseE2ETest):
    def _headers(self, token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def set_ignore_list(self, token: str, user_id: str, ignored: list) -> None:
        url = (
            f"{self.server_url}/_matrix/client/v3/user/{user_id}"
            "/account_data/m.ignored_user_list"
        )
        body: Dict[str, Any] = {
            "ignored_users": {ignored_id: {} for ignored_id in ignored}
        }
        response = requests.put(url, json=body, headers=self._headers(token))
        self.assertEqual(response.status_code, 200, response.text)

    async def create_room(self, token: str, join_rule: str, access_code=None):
        content: Dict[str, Any] = {"join_rule": join_rule}
        if access_code is not None:
            content["access_code"] = access_code
        body = {
            "visibility": "private",
            "preset": "private_chat",
            "initial_state": [
                {"type": "m.room.join_rules", "state_key": "", "content": content}
            ],
        }
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json=cast(Any, body),
            headers=self._headers(token),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["room_id"]

    async def set_power_level(
        self, room_id: str, admin_token: str, user_id: str, level: int
    ) -> None:
        url = (
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}"
            "/state/m.room.power_levels"
        )
        current = requests.get(url, headers=self._headers(admin_token)).json()
        current.setdefault("users", {})[user_id] = level
        response = requests.put(url, json=current, headers=self._headers(admin_token))
        self.assertEqual(response.status_code, 200, response.text)

    def knock(self, room_id: str, token: str) -> requests.Response:
        return requests.post(
            f"{self.server_url}/_matrix/client/v3/knock/{room_id}",
            json={},
            headers=self._headers(token),
        )

    def join(self, room_id: str, token: str) -> requests.Response:
        return requests.post(
            f"{self.server_url}/_matrix/client/v3/join/{room_id}",
            json={},
            headers=self._headers(token),
        )

    def knock_with_code(self, code: str, token: str) -> requests.Response:
        return requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/knock_with_code",
            json={"access_code": code},
            headers=self._headers(token),
        )

    def membership(self, room_id: str, user_id: str, admin_token: str):
        response = requests.get(
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id}"
            f"/state/m.room.member/{user_id}",
            headers=self._headers(admin_token),
        )
        if response.status_code == 404:
            return None
        return response.json().get("membership")

    def assert_generic_forbidden(self, response: requests.Response) -> None:
        self.assertEqual(response.status_code, 403, response.text)
        body = response.json()
        self.assertEqual(body.get("errcode"), "M_FORBIDDEN")
        # No reason leaks: nothing in the body says "block" or "ignore".
        self.assertNotIn("block", response.text.lower())
        self.assertNotIn("ignor", response.text.lower())

    async def test_blocked_requester_is_refused_on_every_entry_path(self) -> None:
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
                synapse_config_overrides=SYNAPSE_CONFIG_OVERRIDES
            )

            # Non-server-admin accounts: Synapse bypasses user_may_join_room
            # for server admins, so admin=True would mask the join gate.
            for name in ("owner", "coadmin", "member", "requester"):
                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user=name,
                    password=PASSWORD,
                    admin=False,
                )
            owner_id, owner_token = await self.login_user("owner", PASSWORD)
            coadmin_id, coadmin_token = await self.login_user("coadmin", PASSWORD)
            member_id, member_token = await self.login_user("member", PASSWORD)
            requester_id, requester_token = await self.login_user("requester", PASSWORD)

            knock_room = await self.create_room(owner_token, "knock")
            public_room = await self.create_room(owner_token, "public")
            code_room = await self.create_room(
                owner_token, "knock", access_code=ACCESS_CODE
            )
            rooms = [knock_room, public_room, code_room]

            # coadmin (PL 100) and member (PL 0) join every room.
            for room_id in rooms:
                for uid, tok in (
                    (coadmin_id, coadmin_token),
                    (member_id, member_token),
                ):
                    self.assertTrue(
                        await self.invite_user_to_room(room_id, uid, owner_token)
                    )
                    self.assertTrue(await self.accept_room_invitation(room_id, tok))
                await self.set_power_level(room_id, owner_token, coadmin_id, 100)

            def deny_knock(room_id: str) -> None:
                # Owner denies the knock so the requester is back to no
                # membership before the next scenario.
                requests.post(
                    f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/kick",
                    json={"user_id": requester_id},
                    headers=self._headers(owner_token),
                )

            # --- A non-admin's block does not gate anything ---
            await self.set_ignore_list(member_token, member_id, [requester_id])
            self.assertEqual(
                self.knock(knock_room, requester_token).status_code,
                200,
                "knock should pass when only a PL-0 member blocks the requester",
            )
            deny_knock(knock_room)

            # --- Only one of two admins blocks: still not gated ---
            await self.set_ignore_list(coadmin_token, coadmin_id, [requester_id])
            self.assertEqual(
                self.knock(knock_room, requester_token).status_code,
                200,
                "knock should pass while another admin has not blocked",
            )
            deny_knock(knock_room)

            # --- Every admin blocks the requester ---
            await self.set_ignore_list(owner_token, owner_id, [requester_id])

            # Knock: refused before it lands, no knock membership recorded.
            self.assert_generic_forbidden(self.knock(knock_room, requester_token))
            self.assertNotEqual(
                self.membership(knock_room, requester_id, owner_token), "knock"
            )

            # Direct join to a public room: refused.
            self.assert_generic_forbidden(self.join(public_room, requester_token))
            self.assertIsNone(self.membership(public_room, requester_id, owner_token))

            # A pending invite does not override the block.
            self.assertTrue(
                await self.invite_user_to_room(public_room, requester_id, owner_token)
            )
            self.assert_generic_forbidden(self.join(public_room, requester_token))
            self.assertEqual(
                self.membership(public_room, requester_id, owner_token), "invite"
            )

            # knock_with_code: the code is valid but every matched room is
            # blocked, so a generic 403 with no ban errcode and no room list.
            response = self.knock_with_code(ACCESS_CODE, requester_token)
            self.assert_generic_forbidden(response)
            self.assertNotIn("banned", response.json())
            self.assertNotIn("rooms", response.json())
            self.assertIsNone(self.membership(code_room, requester_id, owner_token))

            # --- One admin un-blocks: every path opens again ---
            await self.set_ignore_list(coadmin_token, coadmin_id, [])
            self.assertEqual(self.knock(knock_room, requester_token).status_code, 200)
            self.assertEqual(self.join(public_room, requester_token).status_code, 200)
            self.assertEqual(
                self.knock_with_code(ACCESS_CODE, requester_token).status_code, 200
            )
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_gate_can_be_disabled_by_config(self) -> None:
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
                module_config={"blocked_join_gate_enabled": False},
                synapse_config_overrides=SYNAPSE_CONFIG_OVERRIDES,
            )
            for name in ("owner", "requester"):
                await self.register_user(
                    config_path=config_path,
                    dir=synapse_dir,
                    user=name,
                    password=PASSWORD,
                    admin=False,
                )
            owner_id, owner_token = await self.login_user("owner", PASSWORD)
            requester_id, requester_token = await self.login_user("requester", PASSWORD)
            room_id = await self.create_room(owner_token, "knock", ACCESS_CODE)
            await self.set_ignore_list(owner_token, owner_id, [requester_id])

            self.assertEqual(self.knock(room_id, requester_token).status_code, 200)
            self.assertEqual(
                self.knock_with_code(ACCESS_CODE, requester_token).status_code, 200
            )
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
