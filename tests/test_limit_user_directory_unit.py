"""Unit tests for LimitUserDirectory.check_username_for_spam.

These mock the ModuleApi and database layer so they run instantly without
spinning up Synapse or PostgreSQL.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from synapse_pangea_chat.limit_user_directory import LimitUserDirectory


def _make_config(
    *,
    public_attribute_search_path: str = "profile.user_settings.public",
    whitelist_patterns: list | None = None,
    filter_if_missing: bool = True,
):
    config = MagicMock()
    config.limit_user_directory_public_attribute_search_path = (
        public_attribute_search_path
    )
    config.limit_user_directory_whitelist_requester_id_patterns = (
        whitelist_patterns or []
    )
    config.limit_user_directory_filter_search_if_missing_public_attribute = (
        filter_if_missing
    )
    return config


def _make_api(
    *, global_account_data: dict | None = None, shared_room_rows: list | None = None
):
    """Build a mock ModuleApi.

    global_account_data: mapping of (user_id, event_type) -> data dict.
        If None, all lookups return None.
    shared_room_rows: list of rows returned by the shared-rooms query.
        If None, returns [].
    """
    api = MagicMock()
    api.is_mine.return_value = True

    # account_data_manager.get_global
    account_data_mgr = MagicMock()

    async def _get_global(user_id: str, event_type: str):
        if global_account_data is None:
            return None
        return global_account_data.get((user_id, event_type))

    account_data_mgr.get_global = _get_global
    api.account_data_manager = account_data_mgr

    # datastores.main.db_pool.execute (for shared rooms query)
    db_pool = MagicMock()
    db_pool.execute = AsyncMock(return_value=shared_room_rows or [])
    datastores = MagicMock()
    datastores.main.db_pool = db_pool
    api._hs.get_datastores.return_value = datastores

    return api


def _build_handler(api, config):
    """Construct LimitUserDirectory without triggering register_spam_checker_callbacks."""
    with patch.object(api, "register_spam_checker_callbacks", MagicMock()):
        handler = LimitUserDirectory(config, api)
    return handler


class TestPublicUserBypassesRoomSharing(unittest.IsolatedAsyncioTestCase):
    """Public users must appear in search results regardless of shared rooms."""

    async def test_public_user_included_without_shared_rooms(self):
        """A public user appears even when requester shares no rooms with them."""
        config = _make_config()
        api = _make_api(
            global_account_data={
                ("@alice:my.server", "profile"): {"user_settings": {"public": True}},
            },
            shared_room_rows=[],  # no shared rooms
        )
        handler = _build_handler(api, config)

        result = await handler.check_username_for_spam(
            {"user_id": "@alice:my.server"}, "@bob:my.server"
        )
        # False = do NOT filter = include in results
        self.assertFalse(result)

    async def test_public_user_skips_room_query(self):
        """When a user is public, the shared-rooms DB query is never executed."""
        config = _make_config()
        api = _make_api(
            global_account_data={
                ("@alice:my.server", "profile"): {"user_settings": {"public": True}},
            },
        )
        handler = _build_handler(api, config)

        await handler.check_username_for_spam(
            {"user_id": "@alice:my.server"}, "@bob:my.server"
        )
        # The db_pool.execute should NOT have been called
        handler.room_store.db_pool.execute.assert_not_called()

    async def test_public_string_true_included(self):
        """Public attribute stored as string 'true' is treated as public."""
        config = _make_config()
        api = _make_api(
            global_account_data={
                ("@alice:my.server", "profile"): {"user_settings": {"public": "true"}},
            },
        )
        handler = _build_handler(api, config)

        result = await handler.check_username_for_spam(
            {"user_id": "@alice:my.server"}, "@bob:my.server"
        )
        self.assertFalse(result)

    async def test_public_string_True_included(self):
        """Public attribute stored as string 'True' (capitalized) is treated as public."""
        config = _make_config()
        api = _make_api(
            global_account_data={
                ("@alice:my.server", "profile"): {"user_settings": {"public": "True"}},
            },
        )
        handler = _build_handler(api, config)

        result = await handler.check_username_for_spam(
            {"user_id": "@alice:my.server"}, "@bob:my.server"
        )
        self.assertFalse(result)


class TestNonPublicUserRequiresSharedRoom(unittest.IsolatedAsyncioTestCase):
    """Non-public users should only appear if they share a room with the requester."""

    async def test_private_user_excluded_without_shared_rooms(self):
        """A private user is excluded when no shared rooms exist."""
        config = _make_config()
        api = _make_api(
            global_account_data={
                ("@alice:my.server", "profile"): {"user_settings": {"public": False}},
            },
            shared_room_rows=[],
        )
        handler = _build_handler(api, config)

        result = await handler.check_username_for_spam(
            {"user_id": "@alice:my.server"}, "@bob:my.server"
        )
        # True = filter = exclude from results
        self.assertTrue(result)

    async def test_private_user_included_with_shared_room(self):
        """A private user appears when they share at least one room with requester."""
        config = _make_config()
        api = _make_api(
            global_account_data={
                ("@alice:my.server", "profile"): {"user_settings": {"public": False}},
            },
            shared_room_rows=[("!room1:my.server",)],
        )
        handler = _build_handler(api, config)

        result = await handler.check_username_for_spam(
            {"user_id": "@alice:my.server"}, "@bob:my.server"
        )
        self.assertFalse(result)

    async def test_private_user_runs_room_query(self):
        """For non-public users, the shared-rooms DB query IS executed."""
        config = _make_config()
        api = _make_api(
            global_account_data={
                ("@alice:my.server", "profile"): {"user_settings": {"public": False}},
            },
            shared_room_rows=[],
        )
        handler = _build_handler(api, config)

        await handler.check_username_for_spam(
            {"user_id": "@alice:my.server"}, "@bob:my.server"
        )
        handler.room_store.db_pool.execute.assert_called_once()


class TestMissingPublicAttribute(unittest.IsolatedAsyncioTestCase):
    """Users with no account data or missing public attribute."""

    async def test_no_account_data_excluded_by_default(self):
        """No account data at all → excluded (filter_if_missing=True default)."""
        config = _make_config(filter_if_missing=True)
        api = _make_api(global_account_data=None)
        handler = _build_handler(api, config)

        result = await handler.check_username_for_spam(
            {"user_id": "@alice:my.server"}, "@bob:my.server"
        )
        self.assertTrue(result)

    async def test_no_account_data_included_when_config_false(self):
        """No account data → included when filter_if_missing=False."""
        config = _make_config(filter_if_missing=False)
        api = _make_api(global_account_data=None)
        handler = _build_handler(api, config)

        result = await handler.check_username_for_spam(
            {"user_id": "@alice:my.server"}, "@bob:my.server"
        )
        self.assertFalse(result)

    async def test_partial_path_missing_excluded(self):
        """Account data exists but nested path doesn't resolve → excluded."""
        config = _make_config(filter_if_missing=True)
        api = _make_api(
            global_account_data={
                ("@alice:my.server", "profile"): {
                    "user_settings": {}  # 'public' key missing
                },
            },
        )
        handler = _build_handler(api, config)

        result = await handler.check_username_for_spam(
            {"user_id": "@alice:my.server"}, "@bob:my.server"
        )
        self.assertTrue(result)

    async def test_missing_nested_key_excluded(self):
        """Account data has top-level key but user_settings missing → excluded."""
        config = _make_config(filter_if_missing=True)
        api = _make_api(
            global_account_data={
                ("@alice:my.server", "profile"): {},  # no 'user_settings'
            },
        )
        handler = _build_handler(api, config)

        result = await handler.check_username_for_spam(
            {"user_id": "@alice:my.server"}, "@bob:my.server"
        )
        self.assertTrue(result)


class TestWhitelistedRequester(unittest.IsolatedAsyncioTestCase):
    """Whitelisted requesters bypass all filtering."""

    async def test_whitelisted_requester_sees_private_users(self):
        """A whitelisted requester can see private users without shared rooms."""
        config = _make_config(
            whitelist_patterns=["@admin.*"],
        )
        api = _make_api(
            global_account_data={
                ("@alice:my.server", "profile"): {"user_settings": {"public": False}},
            },
            shared_room_rows=[],
        )
        handler = _build_handler(api, config)

        result = await handler.check_username_for_spam(
            {"user_id": "@alice:my.server"}, "@admin:my.server"
        )
        self.assertFalse(result)

    async def test_non_whitelisted_requester_filtered(self):
        """A non-whitelisted requester cannot see private users without shared rooms."""
        config = _make_config(
            whitelist_patterns=["@admin.*"],
        )
        api = _make_api(
            global_account_data={
                ("@alice:my.server", "profile"): {"user_settings": {"public": False}},
            },
            shared_room_rows=[],
        )
        handler = _build_handler(api, config)

        result = await handler.check_username_for_spam(
            {"user_id": "@alice:my.server"}, "@bob:my.server"
        )
        self.assertTrue(result)


class TestRemoteUsers(unittest.IsolatedAsyncioTestCase):
    """Remote users are always included."""

    async def test_remote_user_always_included(self):
        api = _make_api()
        api.is_mine.return_value = False
        config = _make_config()
        handler = _build_handler(api, config)

        result = await handler.check_username_for_spam(
            {"user_id": "@alice:remote.server"}, "@bob:my.server"
        )
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
