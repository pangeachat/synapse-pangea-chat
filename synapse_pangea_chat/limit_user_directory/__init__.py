import logging
import re
from typing import Any

from synapse.module_api import ModuleApi, UserProfile

logger = logging.getLogger("synapse.modules.synapse_pangea_chat.limit_user_directory")


class LimitUserDirectory:
    def __init__(self, config: Any, api: ModuleApi):
        self._api = api
        self._config = config

        self._api.register_spam_checker_callbacks(
            check_username_for_spam=self.check_username_for_spam,
        )
        self._datastores = self._api._hs.get_datastores()
        self.room_store = self._datastores.main

    async def check_username_for_spam(
        self, user_profile: UserProfile, requester_id: str
    ) -> bool:
        """
        Decide whether to filter a user from the user directory results.

        :param user_profile: The user profile to check.
        :param requester_id: The user ID of the requester, in @<username>:<server> format.

        # Return true to *exclude* the user from the results.
        """
        # Bypass the filter if the username matches the whitelist pattern.
        for (
            pattern
        ) in self._config.limit_user_directory_whitelist_requester_id_patterns:
            if re.match(pattern, requester_id):
                return False

        user_id = user_profile["user_id"]

        # For remote users, nothing to do.
        if not self._api.is_mine(user_id):
            return False

        # For local users, check if the user has their profile set to public
        public_attribute_search_path = (
            self._config.limit_user_directory_public_attribute_search_path.split(".")
        )
        # If the user does not set their profile to public, we default them to
        # be private, which is equivalent to returning True to indicate this
        # username should be filtered.
        global_data = await self._api.account_data_manager.get_global(
            user_id, public_attribute_search_path[0]
        )
        if global_data is None:
            return (
                self._config.limit_user_directory_filter_search_if_missing_public_attribute
            )

        for path in public_attribute_search_path[1:]:
            global_data = global_data.get(path, None)
            if global_data is None:
                return (
                    self._config.limit_user_directory_filter_search_if_missing_public_attribute
                )
        if isinstance(global_data, str):
            is_public = global_data.lower() == "true"
        elif isinstance(global_data, bool):
            is_public = global_data
        else:
            # Should be unreachable, so we log a warning and consider the data missing
            logger.warning(f"Unexpected type for public attribute: {type(global_data)}")
            return (
                self._config.limit_user_directory_filter_search_if_missing_public_attribute
            )

        if is_public:
            return False

        # search if requester shares any room (private or public) with the requestee
        shared_rooms_query = """
            SELECT room_id FROM users_who_share_private_rooms
            WHERE user_id = ? AND other_user_id = ?
            UNION
            SELECT a.room_id
            FROM users_in_public_rooms a
            INNER JOIN users_in_public_rooms b
            ON a.room_id = b.room_id
            WHERE a.user_id = ? AND b.user_id = ?
        """
        params = (requester_id, user_id, requester_id, user_id)

        # Check both private and public rooms in one query
        rows = await self.room_store.db_pool.execute(
            "get_shared_rooms",
            shared_rooms_query,
            *params,
        )

        # if any shared room exists then allow the user (do not filter)
        if len(rows) > 0:
            return False

        # otherwise filter the user since they do not share any room with the requester
        return True
