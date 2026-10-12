"""Integration tests for the member_analytics_profiles endpoint.

Uses ``BaseSynapseE2ETest`` to spin up a local Synapse + PostgreSQL instance
with the ``PangeaChat`` module loaded.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import quote

import requests

from .base_e2e import BaseSynapseE2ETest

FIELD = "pangea.analytics_profile"

_SYNAPSE_CONFIG = {
    "rc_joins": {
        "local": {"per_second": 9999, "burst_count": 9999},
    },
    "rc_message": {"per_second": 9999, "burst_count": 9999},
}


def _module_config(*, requests_per_burst: int = 100) -> Dict[str, Any]:
    return {
        "member_analytics_profiles_requests_per_burst": requests_per_burst,
        "member_analytics_profiles_burst_duration_seconds": 60,
    }


class MemberAnalyticsProfilesHelpers:
    """Helpers shared by the test classes, one class per module config."""

    def _headers(self, token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _call(self, params: Any, token: Optional[str]) -> requests.Response:
        headers = self._headers(token) if token is not None else {}
        return requests.get(
            f"{self.server_url}/_synapse/client/pangea/v1/member_analytics_profiles",
            params=params,
            headers=headers,
        )

    def _profiles(self, room_id: str, token: str) -> Dict[str, Any]:
        resp = self._call({"room_id": room_id}, token)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["profiles"]

    def _profile_url(self, user_id: str, field: str = "") -> str:
        url = f"{self.server_url}/_matrix/client/v3/profile/{quote(user_id, safe='')}"
        return f"{url}/{field}" if field else url

    def _publish(self, user_id: str, token: str, profile: Any) -> None:
        resp = requests.put(
            self._profile_url(user_id, FIELD),
            json={FIELD: profile},
            headers=self._headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def _room_post(self, room_id: str, action: str, token: str, **body: Any) -> None:
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/rooms/{quote(room_id, safe='')}/{action}",
            json=body,
            headers=self._headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    def _create_room(self, token: str) -> str:
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json={"visibility": "private", "preset": "public_chat"},
            headers=self._headers(token),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["room_id"]


class TestMemberAnalyticsProfilesEndpoint(
    MemberAnalyticsProfilesHelpers, BaseSynapseE2ETest
):
    """Tests for ``GET /_synapse/client/pangea/v1/member_analytics_profiles``."""

    async def test_member_reads_every_joined_members_profile(self) -> None:
        await self.homeserver(
            module_config=_module_config(),
            synapse_config_overrides=_SYNAPSE_CONFIG,
        )
        (alice_id, alice) = await self.new_user("alice")
        (bob_id, bob) = await self.new_user("bob")
        (carol_id, carol) = await self.new_user("carol")
        (_, dave) = await self.new_user("dave")
        (eve_id, eve) = await self.new_user("eve")
        (_, outsider) = await self.new_user("outsider")

        alice_profile = {"analytics": {"es": {"level": 3, "stars": 7}}}
        bob_profile = {"analytics": {"es": {"level": 5, "stars": 21}}}
        self._publish(alice_id, alice, alice_profile)
        self._publish(bob_id, bob, bob_profile)

        room = self._create_room(alice)
        for token in (bob, carol, dave):
            self._room_post(room, "join", token)
        self._room_post(room, "leave", dave)
        self._room_post(room, "invite", alice, user_id=eve_id)

        # Every joined member, each with their field or an empty profile.
        # The member who left and the one only invited are not listed.
        expected = {
            alice_id: {FIELD: alice_profile},
            bob_id: {FIELD: bob_profile},
            carol_id: {},
        }
        self.assertEqual(self._profiles(room, alice), expected)
        self.assertEqual(self._profiles(room, carol), expected)

        # Each profile matches what the profile API answers for that member.
        resp = requests.get(self._profile_url(bob_id), headers=self._headers(alice))
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual({FIELD: resp.json()[FIELD]}, expected[bob_id])

        # Every refusal is the same 403 with the same body: an outsider, an
        # invited user, a member who left, and an unknown room.
        refusal = self._call({"room_id": room}, outsider)
        self.assertEqual(refusal.status_code, 403, refusal.text)
        for room_id, token in (
            (room, eve),
            (room, dave),
            ("!doesnotexist:my.domain.name", alice),
        ):
            with self.subTest(room_id=room_id):
                resp = self._call({"room_id": room_id}, token)
                self.assertEqual(resp.status_code, 403, resp.text)
                self.assertEqual(resp.json(), refusal.json())

        # Validation and authentication.
        self.assertEqual(self._call({"room_id": room}, None).status_code, 401)
        # The last is two bytes that are not UTF-8.
        for bad in ({}, {"room_id": ""}, {"room_id": "not-a-room"}, "room_id=%FF%FE"):
            with self.subTest(bad=bad):
                self.assertEqual(self._call(bad, alice).status_code, 400)
        two_rooms = self._call([("room_id", room), ("room_id", room)], alice)
        self.assertEqual(two_rooms.status_code, 400, two_rooms.text)

        self.assertIn("member_analytics_profiles: room=", "\n".join(self.server_log()))

    async def test_a_kicked_or_banned_caller_is_refused(self) -> None:
        await self.homeserver(
            module_config=_module_config(),
            synapse_config_overrides=_SYNAPSE_CONFIG,
        )
        (_, alice) = await self.new_user("alice")
        (frank_id, frank) = await self.new_user("frank")
        (gina_id, gina) = await self.new_user("gina")
        (_, outsider) = await self.new_user("outsider")
        room = self._create_room(alice)
        for token in (frank, gina):
            self._room_post(room, "join", token)
        self._room_post(room, "kick", alice, user_id=frank_id)
        self._room_post(room, "ban", alice, user_id=gina_id)

        refusal = self._call({"room_id": room}, outsider)
        self.assertEqual(refusal.status_code, 403, refusal.text)
        for token in (frank, gina):
            resp = self._call({"room_id": room}, token)
            self.assertEqual(resp.status_code, 403, resp.text)
            self.assertEqual(resp.json(), refusal.json())

    async def test_a_value_that_is_not_an_object_comes_back_as_stored(
        self,
    ) -> None:
        await self.homeserver(
            module_config=_module_config(),
            synapse_config_overrides=_SYNAPSE_CONFIG,
        )
        (alice_id, alice) = await self.new_user("alice")
        (hank_id, hank) = await self.new_user("hank")
        (ivy_id, ivy) = await self.new_user("ivy")
        # The profile API accepts any JSON value for a custom field, so a
        # member can store one the app never writes.
        self._publish(hank_id, hank, None)
        self._publish(ivy_id, ivy, "just-a-string")
        room = self._create_room(alice)
        for token in (hank, ivy):
            self._room_post(room, "join", token)

        self.assertEqual(
            self._profiles(room, alice),
            {
                alice_id: {},
                hank_id: {FIELD: None},
                ivy_id: {FIELD: "just-a-string"},
            },
        )


class TestMemberAnalyticsProfilesRateLimit(
    MemberAnalyticsProfilesHelpers, BaseSynapseE2ETest
):
    """The endpoint with a per-caller budget of two calls a minute."""

    async def test_rate_limited_per_caller(self) -> None:
        await self.homeserver(
            module_config=_module_config(requests_per_burst=2),
            synapse_config_overrides=_SYNAPSE_CONFIG,
        )
        (_, alice) = await self.new_user("alice")
        (_, bob) = await self.new_user("bob")
        room = self._create_room(alice)
        self._room_post(room, "join", bob)
        self.assertEqual(self._call({"room_id": room}, alice).status_code, 200)
        self.assertEqual(self._call({"room_id": room}, alice).status_code, 200)
        self.assertEqual(self._call({"room_id": room}, alice).status_code, 429)
        # Another caller has their own budget.
        self.assertEqual(self._call({"room_id": room}, bob).status_code, 200)
