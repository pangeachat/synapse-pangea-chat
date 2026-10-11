import asyncio
import logging
import subprocess
import sys
import uuid
from typing import Any, List, Tuple, Union, cast

import requests

from .base_e2e import BaseSynapseE2ETest

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    filename="synapse.log",
    filemode="w",
)

LIMIT_USER_DIRECTORY_SYNAPSE_CONFIG = {
    "user_directory": {
        "enabled": True,
        "search_all_users": True,
        "prefer_local_users": True,
        "show_locked_users": True,
    },
}


def _build_module_config(
    filter_search_if_missing_public_attribute: bool = True,
) -> dict:
    return {
        "limit_user_directory_public_attribute_search_path": "profile.user_settings.public",
        "limit_user_directory_whitelist_requester_id_patterns": [
            r"^@whitelisted_t\d+:my\.domain\.name$"
        ],
        "limit_user_directory_filter_search_if_missing_public_attribute": filter_search_if_missing_public_attribute,
    }


class LimitUserDirectoryHelpers:
    """Helpers shared by the test classes, one class per module config."""

    def _term(self, prefix: str) -> str:
        """A search term matching only this test's users named ``prefix...``.

        The directory tokenises a localpart such as ``user0_t3`` into ``user0``
        and ``t3``, and the search ANDs a prefix match per word, so adding the
        tag leaves out the users of every earlier test on the shared server.
        (A later test's tag, such as ``t30``, shares the prefix, but none of
        its users exist yet.)
        """
        return f"{prefix} {self.tag}"

    async def search_users(self, search_term: str, access_token: str) -> List[str]:
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/user_directory/search",
            json={"limit": 100, "search_term": search_term},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertIsInstance(response_json, dict)
        results = response_json.get("results")
        self.assertIsInstance(results, list)
        users: List[str] = []
        for result in results:
            user_id = result.get("user_id")
            self.assertIsInstance(user_id, str)
            users.append(user_id)
        return users

    async def search_users_with_retry(
        self,
        search_term: str,
        access_token: str,
        *,
        required_user_ids: List[str],
        retries: int = 180,
        delay_seconds: float = 0.5,
        nudge_room_id: Union[str, None] = None,
    ) -> List[str]:
        """Poll until all required users appear in the search results.

        Synapse's user-directory updater only runs when a new event fires the
        notifier; a transiently failed run leaves the shared-rooms tables stale
        until the next event, and polling alone never creates one (observed as
        90s of empty results in CI, runs 33079600680 / 33094403617). When
        nudge_room_id is set, send a message into that room between polls to
        re-kick the updater.
        """
        last_results: List[str] = []
        required = set(required_user_ids)
        for _ in range(retries):
            last_results = await self.search_users(search_term, access_token)
            if required.issubset(set(last_results)):
                return last_results
            if nudge_room_id is not None:
                requests.put(
                    f"{self.server_url}/_matrix/client/v3/rooms/{nudge_room_id}"
                    f"/send/m.room.message/{uuid.uuid4().hex}",
                    json={"msgtype": "m.text", "body": "nudge directory updater"},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            await asyncio.sleep(delay_seconds)

        return last_results

    async def get_public_attribute_of_user(
        self, user_id: str, access_token: str
    ) -> Union[bool, None]:
        response = requests.get(
            f"{self.server_url}/_matrix/client/v3/user/{user_id}/account_data/profile",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code == 404:
            return None
        response_json = response.json()
        self.assertIsInstance(response_json, dict)
        user_settings = response_json.get("user_settings", {})
        self.assertIsInstance(user_settings, dict)
        is_public = user_settings.get("public", None)
        if is_public is None:
            return None
        if isinstance(is_public, str):
            is_public = is_public.lower() == "true"
        return is_public

    async def set_public_attribute_of_user(
        self, user_id: str, public_attribute: bool, access_token: str
    ) -> None:
        response = requests.get(
            f"{self.server_url}/_matrix/client/v3/user/{user_id}/account_data/profile",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code == 404:
            response_json = {}
        else:
            response_json = response.json()
            if not isinstance(response_json, dict):
                self.fail(f"Response JSON is not a dictionary: {response_json}")

        update_json = response_json.copy()
        if "user_settings" not in update_json:
            update_json["user_settings"] = {}
        update_json["user_settings"]["public"] = public_attribute
        response = requests.put(
            f"{self.server_url}/_matrix/client/v3/user/{user_id}/account_data/profile",
            json=update_json,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(response.status_code, 200)
        user_public_attribute = await self.get_public_attribute_of_user(
            user_id, access_token
        )
        self.assertEqual(user_public_attribute, public_attribute)

    def assert_mounted_module(self) -> None:
        version_cmd = [
            sys.executable,
            "-m",
            "synapse_pangea_chat",
            "--version",
        ]
        subprocess.check_call(version_cmd)


class TestE2E(LimitUserDirectoryHelpers, BaseSynapseE2ETest):
    async def test_limit_user_directory(self) -> None:
        await self.homeserver(
            module_config=_build_module_config(),
            synapse_config_overrides=LIMIT_USER_DIRECTORY_SYNAPSE_CONFIG,
        )

        self.assert_mounted_module()

        creds: List[Tuple[str, str]] = []
        for i in range(6):
            creds.append(await self.new_user(f"user{i}"))

        for i in range(6):
            if i == 0 or i == 1:
                # User 0, 1: private
                await self.set_public_attribute_of_user(creds[i][0], False, creds[i][1])
            elif i == 2 or i == 3:
                # User 2, 3: public
                await self.set_public_attribute_of_user(creds[i][0], True, creds[i][1])
            elif i == 4 or i == 5:
                # User 4, 5: not set
                ...

        for i in range(6):
            (username, access_token) = creds[i]
            users = await self.search_users(self._term("user"), access_token)
            # Expect that the search results do not include the searcher's own ID.
            self.assertNotIn(username, users)
            for user in users:
                other_user_index = int(user[5])  # @user0_t1, @user1_t1, ...
                self.assertIn(other_user_index, [2, 3])

                user_is_public = await self.get_public_attribute_of_user(
                    user, creds[other_user_index][1]
                )
                self.assertEqual(user_is_public, True)

        # Register whitelisted user
        (whitelisted_username, whitelisted_access_token) = await self.new_user(
            "whitelisted", admin=True
        )
        for username, _access_token in creds:
            localpart = username.split(":", 1)[0].lstrip("@")
            users = await self.search_users_with_retry(
                localpart,
                whitelisted_access_token,
                required_user_ids=[username],
            )
            self.assertIn(username, users)

        # Shared room overrides private profile filtering.
        (userA, tokenA) = await self.new_user("userA")
        (userB, tokenB) = await self.new_user("userB")
        # Ensure both users have private profiles.
        await self.set_public_attribute_of_user(userA, False, tokenA)
        await self.set_public_attribute_of_user(userB, False, tokenB)

        # userA creates a private direct room.
        create_room_url = f"{self.server_url}/_matrix/client/v3/createRoom"
        create_room_payload = {"preset": "private_chat", "is_direct": True}
        response = requests.post(
            create_room_url,
            headers={"Authorization": f"Bearer {tokenA}"},
            json=cast(Any, create_room_payload),
        )
        self.assertEqual(response.status_code, 200)
        room_id = response.json()["room_id"]

        # userA invites userB.
        invite_url = f"{self.server_url}/_matrix/client/v3/rooms/{room_id}/invite"
        invite_payload = {"user_id": userB}
        response = requests.post(
            invite_url,
            headers={"Authorization": f"Bearer {tokenA}"},
            json=invite_payload,
        )
        self.assertEqual(response.status_code, 200)

        # userB joins the room.
        join_url = f"{self.server_url}/_matrix/client/v3/join/{room_id}"
        response = requests.post(
            join_url, headers={"Authorization": f"Bearer {tokenB}"}
        )
        self.assertEqual(response.status_code, 200)

        # Search for userB as userA; shared room should allow userB to appear in the results.
        user_b_localpart = userB.split(":", 1)[0].lstrip("@")
        users = await self.search_users_with_retry(
            user_b_localpart,
            tokenA,
            required_user_ids=[userB],
            nudge_room_id=room_id,
        )
        self.assertIn(userB, users)

    async def test_cannot_search_for_self(self) -> None:
        await self.homeserver(
            module_config=_build_module_config(),
            synapse_config_overrides=LIMIT_USER_DIRECTORY_SYNAPSE_CONFIG,
        )

        # Register and login a user.
        (selfUser, tokenSelf) = await self.new_user("selfUser")
        # Optionally, set the public attribute to True.
        await self.set_public_attribute_of_user(selfUser, True, tokenSelf)

        # Search for the user using their own token.
        results = await self.search_users(self._term("selfUser"), tokenSelf)
        # Assert that the result does not include the user's own id.
        self.assertNotIn(selfUser, results)


class TestE2EMissingPublicAttributeNotFiltered(
    LimitUserDirectoryHelpers, BaseSynapseE2ETest
):
    """The spam checker configured to show users with no public attribute."""

    async def test_missing_public_attribute_filtering(self) -> None:
        await self.homeserver(
            module_config=_build_module_config(
                filter_search_if_missing_public_attribute=False
            ),
            synapse_config_overrides=LIMIT_USER_DIRECTORY_SYNAPSE_CONFIG,
        )

        # Register two users: one with missing attribute and one explicitly public.
        (filterUser, tokenF) = await self.new_user("filterUser")
        (publicUser, tokenP) = await self.new_user("publicUser")

        # Set public attribute only for publicUser.
        await self.set_public_attribute_of_user(publicUser, True, tokenP)
        # Do not set for filterUser so its public attribute remains missing.

        # Register an extra user to perform the search.
        (searcher, tokenS) = await self.new_user("searcher")
        # Set searcher to public so they can search.
        await self.set_public_attribute_of_user(searcher, True, tokenS)

        # Search for all users using searcher's token.
        users = await self.search_users_with_retry(
            self._term("publicUser"), tokenS, required_user_ids=[publicUser]
        )

        # Expect both the explicitly public and the missing attribute user to appear.
        self.assertIn(publicUser, users)

        users = await self.search_users_with_retry(
            self._term("filterUser"), tokenS, required_user_ids=[filterUser]
        )
        self.assertIn(filterUser, users)
