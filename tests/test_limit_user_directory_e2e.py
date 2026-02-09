import logging
import subprocess
import sys
from typing import List, Tuple, Union

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


def _build_module_config(
    filter_search_if_missing_public_attribute: bool = True,
) -> dict:
    return {
        "limit_user_directory_public_attribute_search_path": "profile.user_settings.public",
        "limit_user_directory_whitelist_requester_id_patterns": [
            "@whitelisted:my.domain.name"
        ],
        "limit_user_directory_filter_search_if_missing_public_attribute": filter_search_if_missing_public_attribute,
    }


class TestE2E(BaseSynapseE2ETest):
    async def search_users(self, search_term: str, access_token: str) -> List[str]:
        response = requests.post(
            "http://localhost:8008/_matrix/client/v3/user_directory/search",
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

    async def get_public_attribute_of_user(
        self, user_id: str, access_token: str
    ) -> Union[bool, None]:
        response = requests.get(
            f"http://localhost:8008/_matrix/client/v3/user/{user_id}/account_data/profile",
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
            f"http://localhost:8008/_matrix/client/v3/user/{user_id}/account_data/profile",
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
            f"http://localhost:8008/_matrix/client/v3/user/{user_id}/account_data/profile",
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

    async def test_limit_user_directory(self) -> None:
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config=_build_module_config(),
                synapse_config_overrides=LIMIT_USER_DIRECTORY_SYNAPSE_CONFIG,
            )

            self.assert_mounted_module()

            creds: List[Tuple[str, str]] = []
            for i in range(6):
                await self.register_user(
                    config_path, synapse_dir, f"user{i}", f"password{i}", False
                )
                (username, access_token) = await self.login_user(
                    f"user{i}", f"password{i}"
                )
                creds.append((username, access_token))

            for i in range(6):
                if i == 0 or i == 1:
                    # User 0, 1: private
                    await self.set_public_attribute_of_user(
                        creds[i][0], False, creds[i][1]
                    )
                elif i == 2 or i == 3:
                    # User 2, 3: public
                    await self.set_public_attribute_of_user(
                        creds[i][0], True, creds[i][1]
                    )
                elif i == 4 or i == 5:
                    # User 4, 5: not set
                    ...

            for i in range(6):
                (username, access_token) = creds[i]
                users = await self.search_users("user", access_token)
                # Expect that the search results do not include the searcher's own ID.
                self.assertNotIn(username, users)
                for user in users:
                    other_user_index = int(user[5])  # @user0, @user1, @user2, ...
                    self.assertIn(other_user_index, [2, 3])

                    user_is_public = await self.get_public_attribute_of_user(
                        user, creds[other_user_index][1]
                    )
                    self.assertEqual(user_is_public, True)

            # Register whitelisted user
            await self.register_user(
                config_path, synapse_dir, "whitelisted", "password", True
            )
            (whitelisted_username, whitelisted_access_token) = await self.login_user(
                "whitelisted", "password"
            )
            users = await self.search_users("user", whitelisted_access_token)
            self.assertEqual(len(users), 6)

            # Shared room overrides private profile filtering.
            await self.register_user(
                config_path, synapse_dir, "userA", "passwordA", False
            )
            await self.register_user(
                config_path, synapse_dir, "userB", "passwordB", False
            )
            (userA, tokenA) = await self.login_user("userA", "passwordA")
            (userB, tokenB) = await self.login_user("userB", "passwordB")
            # Ensure both users have private profiles.
            await self.set_public_attribute_of_user(userA, False, tokenA)
            await self.set_public_attribute_of_user(userB, False, tokenB)

            # userA creates a private direct room.
            create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"
            create_room_payload = {"preset": "private_chat", "is_direct": True}
            response = requests.post(
                create_room_url,
                headers={"Authorization": f"Bearer {tokenA}"},
                json=create_room_payload,
            )
            self.assertEqual(response.status_code, 200)
            room_id = response.json()["room_id"]

            # userA invites userB.
            invite_url = (
                f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/invite"
            )
            invite_payload = {"user_id": userB}
            response = requests.post(
                invite_url,
                headers={"Authorization": f"Bearer {tokenA}"},
                json=invite_payload,
            )
            self.assertEqual(response.status_code, 200)

            # userB joins the room.
            join_url = f"http://localhost:8008/_matrix/client/v3/join/{room_id}"
            response = requests.post(
                join_url, headers={"Authorization": f"Bearer {tokenB}"}
            )
            self.assertEqual(response.status_code, 200)

            # Search for userB as userA; shared room should allow userB to appear in the results.
            users = await self.search_users("userB", tokenA)
            self.assertIn(userB, users)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_missing_public_attribute_filtering(self) -> None:
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config=_build_module_config(
                    filter_search_if_missing_public_attribute=False
                ),
                synapse_config_overrides=LIMIT_USER_DIRECTORY_SYNAPSE_CONFIG,
            )

            # Register two users: one with missing attribute and one explicitly public.
            await self.register_user(
                config_path, synapse_dir, "filterUser", "passwordF", False
            )
            await self.register_user(
                config_path, synapse_dir, "publicUser", "passwordP", False
            )
            (filterUser, tokenF) = await self.login_user("filterUser", "passwordF")
            (publicUser, tokenP) = await self.login_user("publicUser", "passwordP")

            # Set public attribute only for publicUser.
            await self.set_public_attribute_of_user(publicUser, True, tokenP)
            # Do not set for filterUser so its public attribute remains missing.

            # Register an extra user to perform the search.
            await self.register_user(
                config_path, synapse_dir, "searcher", "passwordS", False
            )
            (searcher, tokenS) = await self.login_user("searcher", "passwordS")
            # Set searcher to public so they can search.
            await self.set_public_attribute_of_user(searcher, True, tokenS)

            # Search for all users using searcher's token.
            users = await self.search_users("publicUser", tokenS)

            # Expect both the explicitly public and the missing attribute user to appear.
            self.assertIn(publicUser, users)

            users = await self.search_users("filterUser", tokenS)
            self.assertIn(filterUser, users)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_cannot_search_for_self(self) -> None:
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config=_build_module_config(),
                synapse_config_overrides=LIMIT_USER_DIRECTORY_SYNAPSE_CONFIG,
            )

            # Register and login a user.
            await self.register_user(
                config_path, synapse_dir, "selfUser", "passwordSelf", False
            )
            (selfUser, tokenSelf) = await self.login_user("selfUser", "passwordSelf")
            # Optionally, set the public attribute to True.
            await self.set_public_attribute_of_user(selfUser, True, tokenSelf)

            # Search for the user using their own token.
            results = await self.search_users("selfUser", tokenSelf)
            # Assert that the result does not include the user's own id.
            self.assertNotIn(selfUser, results)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
