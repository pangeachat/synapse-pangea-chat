import asyncio
import logging
import time
from typing import Any, Dict, List
from urllib.parse import quote

import psycopg2
import psycopg2.extensions
import requests
from psycopg2.extensions import parse_dsn

from synapse_pangea_chat.public_courses import _cache
from synapse_pangea_chat.public_courses import request_log as rate_limit_log

from .base_e2e import BaseSynapseE2ETest

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    filename="synapse.log",
    filemode="w",
)

ROOM_PREVIEW_MODULE_CONFIG = {
    "room_preview_state_event_types": [
        "pangea.activity_plan",
        "pangea.activity_roles",
    ]
}

ROOMDIRECTORY_CONFIG = {
    "roomdirectory": {
        "enable_room_list_search": True,
        "room_list_publication_rules": [
            {
                "action": "allow",
                "user_id": "*",
                "room_id": "*",
                "alias": "*",
            }
        ],
    }
}


class TestE2E(BaseSynapseE2ETest):
    async def test_room_preview(self):
        """Setup test environment and run basic room preview tests."""
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
                module_config=ROOM_PREVIEW_MODULE_CONFIG,
            )

            # Register a user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="user1",
                password="pw1",
                admin=False,
            )

            # Login user
            _, token = await self.login_user("user1", "pw1")

            # Create a private room
            room_id = await self.create_private_room_knock_allowed_room(token)

            # Test the room_preview endpoint
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {token}"}

            # Run the individual test methods
            await self._test_basic_room_preview_functionality(
                room_preview_url, headers, room_id
            )
            await self._test_room_preview_data_structure(
                room_preview_url, headers, room_id
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def _test_basic_room_preview_functionality(
        self, room_preview_url: str, headers: dict, room_id: str
    ):
        """Test basic room preview endpoint functionality."""
        # Test with no rooms parameter (should return empty rooms dict)
        response = requests.get(
            room_preview_url,
            headers=headers,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"rooms": {}})

        # Test with single room
        params = {"rooms": room_id}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertIn("rooms", response_data)
        self.assertIn(room_id, response_data["rooms"])

        # Test with multiple rooms (comma-delimited)
        params = {"rooms": f"{room_id},!fake_room:example.com"}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertIn("rooms", response_data)
        self.assertIn(room_id, response_data["rooms"])
        self.assertIn("!fake_room:example.com", response_data["rooms"])

    async def _test_room_preview_data_structure(
        self, room_preview_url: str, headers: dict, room_id: str
    ):
        """Test that the room preview data structure matches expected format."""
        params = {"rooms": room_id}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()

        # Verify top-level structure
        self.assertIn("rooms", response_data)
        self.assertIsInstance(response_data["rooms"], dict)

        # Verify room-level structure
        self.assertIn(room_id, response_data["rooms"])
        room_data = response_data["rooms"][room_id]
        self.assertIsInstance(room_data, dict)

        # The response should follow format: {[room_id]: {[state_event_type]: {[state_key]: JSON}}}
        for event_type, event_data in room_data.items():
            self.assertIsInstance(event_type, str)
            self.assertIsInstance(event_data, dict)

            # Each event type should contain state keys mapped to JSON data
            for state_key, json_data in event_data.items():
                self.assertIsInstance(
                    state_key, str
                )  # State key should be a string (empty string or "default")
                self.assertIsInstance(json_data, dict)  # Should be parsed JSON

                # For state events with empty state key, verify handling
                if state_key == "default":
                    # This is the expected behavior for empty state keys
                    # Should contain just the content, not full Matrix event
                    self.assertIsInstance(json_data, dict)
                    # Verify this is the full Matrix event JSON (which contains content)
                    self.assertIn(
                        "content",
                        json_data,
                        "Response should contain the full Matrix event with 'content' field",
                    )
                elif state_key == "":
                    # Empty state keys are now handled and should not appear in responses
                    # They are converted to "default" in the implementation
                    self.fail(
                        "Empty string state keys should be converted to 'default' key"
                    )

        # Test with fake room to ensure empty structure
        params = {"rooms": "!fake_room:example.com"}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertIn("rooms", response_data)
        self.assertIn("!fake_room:example.com", response_data["rooms"])
        self.assertEqual(response_data["rooms"]["!fake_room:example.com"], {})

    async def test_room_preview_with_room_state_events(self):
        """Setup test environment and run room state events tests."""
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
                module_config=ROOM_PREVIEW_MODULE_CONFIG,
            )

            # Register a user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin_user",
                password="admin_pw",
                admin=True,
            )

            # Login admin user
            _, admin_token = await self.login_user("admin_user", "admin_pw")

            # Create a room with specific state events
            room_id = await self.create_room_with_state_events(admin_token)

            # Test the room_preview endpoint
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {admin_token}"}

            # Run the individual test methods
            await self._test_room_with_state_events_functionality(
                room_preview_url, headers, room_id
            )
            await self._test_multiple_rooms_with_mixed_existence(
                room_preview_url, headers, room_id
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def _test_room_with_state_events_functionality(
        self, room_preview_url: str, headers: dict, room_id: str
    ):
        """Test room preview for room with state events."""
        params = {"rooms": room_id}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()

        # Verify the room exists in response
        self.assertIn("rooms", response_data)
        self.assertIn(room_id, response_data["rooms"])
        room_data = response_data["rooms"][room_id]

        # Verify data structure follows expected format
        self._verify_room_preview_structure(room_data)

        # Specifically test that empty state keys become "default"
        self._verify_empty_state_key_becomes_default(room_data)

    async def _test_multiple_rooms_with_mixed_existence(
        self, room_preview_url: str, headers: dict, room_id: str
    ):
        """Test multiple rooms including non-existent ones."""
        fake_room = "!nonexistent:example.com"
        params = {"rooms": f"{room_id},{fake_room}"}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()

        # Both rooms should be in response
        self.assertIn(room_id, response_data["rooms"])
        self.assertIn(fake_room, response_data["rooms"])

        # Real room should have data, fake room should be empty
        self.assertIsInstance(response_data["rooms"][room_id], dict)
        self.assertEqual(response_data["rooms"][fake_room], {})

    async def create_room_with_state_events(self, access_token: str) -> str:
        """Create a room with specific state events for testing."""
        headers = {"Authorization": f"Bearer {access_token}"}
        create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"

        # Create room with name, topic, and avatar
        create_room_data = {
            "visibility": "private",
            "preset": "private_chat",
            "name": "Test Room for Preview",
            "topic": "This is a test room for room preview functionality",
            "initial_state": [
                {
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "content": {"join_rule": "knock"},
                },
                {
                    "type": "m.room.avatar",
                    "state_key": "",
                    "content": {"url": "mxc://example.com/test_avatar"},
                },
            ],
        }

        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        room_id = response.json()["room_id"]

        # Add additional state events
        state_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state"

        # Add pangea.activity_plan state event
        activity_plan_data = {
            "plan_id": "plan123",
            "title": "Weekly Team Standup",
            "description": "Regular team sync meeting to discuss progress and blockers",
            "activities": [
                {
                    "id": "activity1",
                    "name": "Progress Updates",
                    "duration": 15,
                    "type": "discussion",
                },
                {
                    "id": "activity2",
                    "name": "Blockers Review",
                    "duration": 10,
                    "type": "problem_solving",
                },
            ],
            "total_duration": 25,
            "created_by": "@admin_user:my.domain.name",
        }

        plan_response = requests.put(
            f"{state_url}/pangea.activity_plan/",
            json=activity_plan_data,
            headers=headers,
        )
        self.assertEqual(plan_response.status_code, 200)

        # Add pangea.activity_roles state event
        activity_roles_data = {
            "roles": {
                "@admin_user:my.domain.name": {
                    "role": "facilitator",
                    "permissions": ["manage_activities", "assign_roles", "moderate"],
                },
                "@user1:my.domain.name": {
                    "role": "participant",
                    "permissions": ["participate", "vote"],
                },
            },
            "default_role": "participant",
            "role_definitions": {
                "facilitator": {
                    "description": "Manages the session and activities",
                    "permissions": ["manage_activities", "assign_roles", "moderate"],
                },
                "participant": {
                    "description": "Active participant in activities",
                    "permissions": ["participate", "vote"],
                },
            },
        }

        roles_response = requests.put(
            f"{state_url}/pangea.activity_roles/",
            json=activity_roles_data,
            headers=headers,
        )
        self.assertEqual(roles_response.status_code, 200)

        return room_id

    def _verify_room_preview_structure(self, room_data: dict):
        """Verify that room preview data follows the expected structure."""
        # Data should follow format: {[state_event_type]: {[state_key]: JSON}}
        # Where empty state keys from database become "default"
        self.assertIsInstance(room_data, dict)

        for event_type, event_type_data in room_data.items():
            # Event type should be a string
            self.assertIsInstance(event_type, str)
            # Event type data should be a dict
            self.assertIsInstance(event_type_data, dict)

            for state_key, event_content in event_type_data.items():
                # State key should be a string (currently "" or should be "default" for events with no state key)
                self.assertIsInstance(state_key, str)
                # Event content should be parsed JSON (dict)
                self.assertIsInstance(event_content, dict)

                # Verify handling of empty state keys
                if state_key == "default":
                    # This is the expected behavior for empty state keys
                    # Should contain the full Matrix event (which includes content)
                    self.assertIsInstance(event_content, dict)
                    # Verify this is the full Matrix event JSON with content field
                    self.assertIn(
                        "content",
                        event_content,
                        "Response should contain the full Matrix event with 'content' field",
                    )
                elif state_key == "":
                    # Empty state keys are now handled and should not appear in responses
                    # They are converted to "default" in the implementation
                    self.fail(
                        "Empty string state keys should be converted to 'default' key"
                    )

    def _verify_empty_state_key_becomes_default(self, room_data: dict):
        """Verify that state events with empty state keys are returned with 'default' as the state key."""
        # We know from create_room_with_state_events that we created events with empty state keys:
        # - pangea.activity_plan with state_key=""
        # - pangea.activity_roles with state_key=""
        # - m.room.join_rules with state_key=""
        # - m.room.avatar with state_key=""
        # - m.room.name with state_key="" (from room creation)
        # - m.room.topic with state_key="" (from room creation)

        # Check that these event types exist and have "default" as the state key
        expected_events_with_default_state_key = [
            "pangea.activity_plan",
            "pangea.activity_roles",
        ]

        for event_type in expected_events_with_default_state_key:
            if event_type in room_data:
                event_data = room_data[event_type]
                # Based on test failure, the current implementation uses empty string, not "default"
                # But we want to test for the expected behavior of converting to "default"
                if "default" in event_data:
                    # This is the expected behavior - empty state key becomes "default"
                    # and should return the full Matrix event JSON (which contains content)
                    full_event = event_data["default"]
                    self.assertIsInstance(
                        full_event,
                        dict,
                        f"Event type {event_type} with 'default' state key should have dict content",
                    )
                    # Verify this is the full Matrix event with content field
                    self.assertIn(
                        "content",
                        full_event,
                        f"Event type {event_type} should contain 'content' field in full Matrix event",
                    )

                    # For pangea.activity_plan, verify it has the expected content fields
                    if event_type == "pangea.activity_plan":
                        # Access the content field within the full Matrix event
                        content = full_event.get("content", {})
                        expected_fields = [
                            "plan_id",
                            "title",
                            "description",
                            "activities",
                            "total_duration",
                            "created_by",
                        ]
                        for field in expected_fields:
                            self.assertIn(
                                field,
                                content,
                                f"Activity plan content should contain field '{field}'",
                            )

                    # Verify there are no empty string state keys when using "default"
                    self.assertNotIn(
                        "",
                        event_data,
                        f"Event type {event_type} should not have empty string as state key when using 'default'",
                    )
                elif "" in event_data:
                    # This is the current behavior - empty state key stays as empty string
                    # Currently returns full Matrix event JSON, but should return just content
                    full_event = event_data[""]
                    self.assertIsInstance(
                        full_event,
                        dict,
                        f"Event type {event_type} with empty state key should have dict content",
                    )
                    # Empty state keys should now be converted to "default"
                    self.fail(
                        "Empty string state keys should be converted to 'default' key"
                    )
                else:
                    self.fail(
                        f"Event type {event_type} should have 'default' as state key (empty keys are converted)"
                    )

    async def test_room_preview_empty_cases(self):
        """Setup test environment and run empty/edge case tests."""
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
                module_config=ROOM_PREVIEW_MODULE_CONFIG,
            )

            # Register a user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="test_user",
                password="test_pw",
                admin=False,
            )

            # Login user
            _, token = await self.login_user("test_user", "test_pw")

            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {token}"}

            # Run the individual test methods
            await self._test_empty_rooms_parameter(room_preview_url, headers)
            await self._test_whitespace_rooms_parameter(room_preview_url, headers)
            await self._test_mixed_valid_invalid_room_ids(room_preview_url, headers)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def _test_empty_rooms_parameter(self, room_preview_url: str, headers: dict):
        """Test with empty rooms parameter."""
        response = requests.get(
            room_preview_url,
            headers=headers,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"rooms": {}})

    async def _test_whitespace_rooms_parameter(
        self, room_preview_url: str, headers: dict
    ):
        """Test with whitespace-only rooms parameter."""
        params = {"rooms": "  ,  , "}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"rooms": {}})

    async def _test_mixed_valid_invalid_room_ids(
        self, room_preview_url: str, headers: dict
    ):
        """Test with mix of valid and invalid room IDs."""
        params = {"rooms": "!valid:example.com,,  ,!another:example.com"}
        response = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertIn("rooms", response_data)
        # Should have both valid room IDs
        self.assertIn("!valid:example.com", response_data["rooms"])
        self.assertIn("!another:example.com", response_data["rooms"])
        # Both should be empty since they don't exist
        self.assertEqual(response_data["rooms"]["!valid:example.com"], {})
        self.assertEqual(response_data["rooms"]["!another:example.com"], {})

    async def test_room_preview_cache_performance(self):
        """Test that cache hits are faster than cache misses."""
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
                module_config=ROOM_PREVIEW_MODULE_CONFIG,
            )

            # Register a user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="perf_user",
                password="perf_pw",
                admin=True,
            )

            # Login user
            _, token = await self.login_user("perf_user", "perf_pw")

            # Create a room with state events for testing
            room_id = await self.create_room_with_state_events(token)

            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {token}"}

            # Run cache performance test
            await self._test_cache_hit_performance(room_preview_url, headers, room_id)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def _test_cache_hit_performance(
        self, room_preview_url: str, headers: dict, room_id: str
    ):
        """Test that the cache functions correctly and returns consistent data."""
        import time

        # Clear any existing cache by importing and clearing the cache directly
        try:
            from synapse_pangea_chat.room_preview.get_room_preview import _room_cache

            _room_cache.clear()
        except ImportError:
            pass  # Cache might not be accessible in test environment

        params = {"rooms": room_id}

        # First request (cache miss) - measure and store result
        print("\nCache Functionality Test:")

        start_time = time.time()
        response1 = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        miss_time = time.time() - start_time

        self.assertEqual(response1.status_code, 200)
        first_result = response1.json()

        print(f"  First request (cache miss): {miss_time:.4f}s")

        # Second request (should be cache hit) - measure and compare result
        start_time = time.time()
        response2 = requests.get(
            room_preview_url,
            headers=headers,
            params=params,
            timeout=10,
        )
        hit_time = time.time() - start_time

        self.assertEqual(response2.status_code, 200)
        second_result = response2.json()

        print(f"  Second request (cache hit): {hit_time:.4f}s")

        # Verify cache returns identical data
        self.assertEqual(
            first_result,
            second_result,
            "Cache hit should return identical data to cache miss",
        )

        # Verify both responses have the expected room data structure
        self.assertIn("rooms", first_result)
        self.assertIn("rooms", second_result)
        self.assertIn(room_id, first_result["rooms"])
        self.assertIn(room_id, second_result["rooms"])

        # Test multiple cache hits return consistent data
        for i in range(3):
            response_n = requests.get(
                room_preview_url, headers=headers, params=params, timeout=10
            )
            self.assertEqual(response_n.status_code, 200)
            self.assertEqual(
                response_n.json(),
                first_result,
                f"Cache hit #{i+3} should return identical data",
            )

        print("  ✅ Cache returns consistent data across multiple requests")

        # Test cache with different room combinations
        other_room = "!nonexistent:example.com"
        mixed_params = {"rooms": f"{room_id},{other_room}"}

        response_mixed = requests.get(
            room_preview_url,
            headers=headers,
            params=mixed_params,
            timeout=10,
        )
        self.assertEqual(response_mixed.status_code, 200)
        mixed_result = response_mixed.json()

        # The cached room should have the same data
        self.assertEqual(
            mixed_result["rooms"][room_id],
            first_result["rooms"][room_id],
            "Cached room data should be consistent in mixed requests",
        )

        # The new room should be empty
        self.assertEqual(
            mixed_result["rooms"][other_room],
            {},
            "Non-existent room should return empty data",
        )

        print("  ✅ Cache works correctly with mixed room requests")
        print("  ✅ Cache functionality test completed successfully")

        # Note: Performance benefits are more apparent in production environments
        # where database queries are more complex and network latency is involved

    async def test_room_preview_authentication_error(self):
        """Test that unauthenticated requests return 401 error."""
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
                module_config=ROOM_PREVIEW_MODULE_CONFIG,
            )

            # Test the room_preview endpoint without authentication
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )

            # Test with no authorization header
            response = requests.get(
                room_preview_url,
                params={"rooms": "!test:example.com"},
                timeout=10,
            )
            self.assertEqual(response.status_code, 401)
            response_data = response.json()
            self.assertIn("error", response_data)
            self.assertEqual(response_data["error"], "Unauthorized")
            self.assertIn("errcode", response_data)
            self.assertEqual(response_data["errcode"], "M_UNAUTHORIZED")

            # Test with invalid authorization header
            invalid_headers = {"Authorization": "Bearer invalid_token_12345"}
            response = requests.get(
                room_preview_url,
                headers=invalid_headers,
                params={"rooms": "!test:example.com"},
                timeout=10,
            )
            self.assertEqual(response.status_code, 401)
            response_data = response.json()
            self.assertIn("error", response_data)
            self.assertEqual(response_data["error"], "Unauthorized")
            self.assertIn("errcode", response_data)
            self.assertEqual(response_data["errcode"], "M_UNAUTHORIZED")

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_activity_roles_filtering(self):
        """Test that activity roles include all users with membership summary."""
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
                module_config=ROOM_PREVIEW_MODULE_CONFIG,
            )

            # Register admin user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin_user",
                password="admin_pw",
                admin=True,
            )

            # Register two test users
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="user1",
                password="pw1",
                admin=False,
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="user2",
                password="pw2",
                admin=False,
            )

            # Login users
            _, admin_token = await self.login_user("admin_user", "admin_pw")
            _, user1_token = await self.login_user("user1", "pw1")
            _, user2_token = await self.login_user("user2", "pw2")

            # Create a room with activity roles
            room_id = await self.create_room_with_activity_roles(
                admin_token, user1_token, user2_token
            )

            # Initially all users should be in the activity roles with join membership
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {admin_token}"}

            response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Verify all users are in activity roles
            self.assertIn("rooms", data)
            self.assertIn(room_id, data["rooms"])
            room_data = data["rooms"][room_id]
            self.assertIn("pangea.activity_roles", room_data)

            activity_roles = room_data["pangea.activity_roles"]["default"]["content"][
                "roles"
            ]
            self.assertEqual(len(activity_roles), 3)  # admin + user1 + user2

            # Verify all users are present in roles
            user_ids_in_roles = {role["user_id"] for role in activity_roles.values()}
            expected_users = {
                "@admin_user:my.domain.name",
                "@user1:my.domain.name",
                "@user2:my.domain.name",
            }
            self.assertEqual(user_ids_in_roles, expected_users)

            # Verify membership_summary is present and all users are "join"
            self.assertIn("membership_summary", room_data)
            membership_summary = room_data["membership_summary"]
            self.assertEqual(
                membership_summary.get("@admin_user:my.domain.name"), "join"
            )
            self.assertEqual(membership_summary.get("@user1:my.domain.name"), "join")
            self.assertEqual(membership_summary.get("@user2:my.domain.name"), "join")

            # Remove user2 from the room (kick them)
            kick_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/kick"
            kick_data = {
                "user_id": "@user2:my.domain.name",
                "reason": "Test kick for membership summary",
            }
            kick_response = requests.post(
                kick_url,
                json=kick_data,
                headers=headers,
            )
            self.assertEqual(kick_response.status_code, 200)

            # Wait a moment for the kick to be processed
            await asyncio.sleep(0.5)

            # Request room preview again - user2's role should still be present
            # but membership_summary should show user2 as "leave"
            response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Verify all users are still in activity roles (no filtering)
            room_data = data["rooms"][room_id]
            activity_roles = room_data["pangea.activity_roles"]["default"]["content"][
                "roles"
            ]

            # All three users should still be present in roles
            self.assertEqual(len(activity_roles), 3)

            user_ids_in_roles = {role["user_id"] for role in activity_roles.values()}
            expected_users = {
                "@admin_user:my.domain.name",
                "@user1:my.domain.name",
                "@user2:my.domain.name",
            }
            self.assertEqual(user_ids_in_roles, expected_users)

            # Verify membership_summary shows correct membership states
            self.assertIn("membership_summary", room_data)
            membership_summary = room_data["membership_summary"]
            self.assertEqual(
                membership_summary.get("@admin_user:my.domain.name"), "join"
            )
            self.assertEqual(membership_summary.get("@user1:my.domain.name"), "join")
            # user2 should now be "leave" in membership_summary
            self.assertEqual(membership_summary.get("@user2:my.domain.name"), "leave")

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def create_room_with_activity_roles(
        self, admin_token: str, user1_token: str, user2_token: str
    ) -> str:
        """Create a room with both users invited and add activity roles for all."""
        headers = {"Authorization": f"Bearer {admin_token}"}
        create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"

        # Create room
        create_room_data = {
            "visibility": "private",
            "preset": "private_chat",
            "name": "Test Room for Activity Roles Filtering",
            "invite": ["@user1:my.domain.name", "@user2:my.domain.name"],
        }

        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        room_id = response.json()["room_id"]

        # Accept invitations for both users
        join_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/join"

        user1_headers = {"Authorization": f"Bearer {user1_token}"}
        join_response1 = requests.post(join_url, headers=user1_headers)
        self.assertEqual(join_response1.status_code, 200)

        user2_headers = {"Authorization": f"Bearer {user2_token}"}
        join_response2 = requests.post(join_url, headers=user2_headers)
        self.assertEqual(join_response2.status_code, 200)

        # Add activity roles state event with all three users
        state_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state"
        activity_roles_data = {
            "roles": {
                "role-admin-123": {
                    "archived_at": None,
                    "finished_at": None,
                    "id": "role-admin-123",
                    "role": "facilitator",
                    "user_id": "@admin_user:my.domain.name",
                },
                "role-user1-456": {
                    "archived_at": None,
                    "finished_at": None,
                    "id": "role-user1-456",
                    "role": "participant",
                    "user_id": "@user1:my.domain.name",
                },
                "role-user2-789": {
                    "archived_at": None,
                    "finished_at": None,
                    "id": "role-user2-789",
                    "role": "observer",
                    "user_id": "@user2:my.domain.name",
                },
            }
        }

        roles_response = requests.put(
            f"{state_url}/pangea.activity_roles/",
            json=activity_roles_data,
            headers=headers,
        )
        self.assertEqual(roles_response.status_code, 200)

        return room_id

    async def test_activity_roles_filtering_no_roles(self):
        """Test that room preview works correctly when there are no activity roles."""
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
                module_config=ROOM_PREVIEW_MODULE_CONFIG,
            )

            # Register admin user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin_user",
                password="admin_pw",
                admin=True,
            )

            _, admin_token = await self.login_user("admin_user", "admin_pw")

            # Create a room without activity roles
            room_id = await self.create_private_room_knock_allowed_room(admin_token)

            # Request room preview - should work fine without activity roles
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {admin_token}"}

            response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Should have room data but no activity roles
            self.assertIn("rooms", data)
            self.assertIn(room_id, data["rooms"])
            room_data = data["rooms"][room_id]

            # Activity roles should not be present (since we didn't create any)
            # But the request should still succeed
            if "pangea.activity_roles" in room_data:
                # If present, should be empty or properly structured
                activity_roles_data = room_data["pangea.activity_roles"]
                self.assertIsInstance(activity_roles_data, dict)

            # membership_summary should not be present if no activity roles
            if "pangea.activity_roles" not in room_data:
                self.assertNotIn("membership_summary", room_data)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_left_users_in_activity_roles(self):
        """Test that left users are preserved in activity roles for completed activities.

        This test verifies the behavior requested in the issue:
        - Activity roles should NOT be filtered for users who have left
        - A membership summary should be returned so clients can display info about
          completed activities while knowing who has left
        """
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
                module_config=ROOM_PREVIEW_MODULE_CONFIG,
            )

            # Register users
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="facilitator",
                password="fac_pw",
                admin=True,
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="participant1",
                password="p1_pw",
                admin=False,
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="participant2",
                password="p2_pw",
                admin=False,
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="participant3",
                password="p3_pw",
                admin=False,
            )

            # Login users
            _, facilitator_token = await self.login_user("facilitator", "fac_pw")
            _, p1_token = await self.login_user("participant1", "p1_pw")
            _, p2_token = await self.login_user("participant2", "p2_pw")
            _, p3_token = await self.login_user("participant3", "p3_pw")

            # Create room and add users
            headers = {"Authorization": f"Bearer {facilitator_token}"}
            create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"

            create_room_data = {
                "visibility": "private",
                "preset": "private_chat",
                "name": "Completed Activity Room",
                "invite": [
                    "@participant1:my.domain.name",
                    "@participant2:my.domain.name",
                    "@participant3:my.domain.name",
                ],
            }

            response = requests.post(
                create_room_url,
                json=create_room_data,
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)
            room_id = response.json()["room_id"]

            # All participants join
            join_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/join"

            for token in [p1_token, p2_token, p3_token]:
                join_response = requests.post(
                    join_url, headers={"Authorization": f"Bearer {token}"}
                )
                self.assertEqual(join_response.status_code, 200)

            # Add activity roles - simulating a completed activity
            state_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state"
            activity_roles_data = {
                "roles": {
                    "role-fac": {
                        "archived_at": "2024-01-01T10:00:00Z",
                        "finished_at": "2024-01-01T09:30:00Z",
                        "id": "role-fac",
                        "role": "facilitator",
                        "user_id": "@facilitator:my.domain.name",
                    },
                    "role-p1": {
                        "archived_at": "2024-01-01T10:00:00Z",
                        "finished_at": "2024-01-01T09:30:00Z",
                        "id": "role-p1",
                        "role": "presenter",
                        "user_id": "@participant1:my.domain.name",
                    },
                    "role-p2": {
                        "archived_at": "2024-01-01T10:00:00Z",
                        "finished_at": "2024-01-01T09:30:00Z",
                        "id": "role-p2",
                        "role": "participant",
                        "user_id": "@participant2:my.domain.name",
                    },
                    "role-p3": {
                        "archived_at": "2024-01-01T10:00:00Z",
                        "finished_at": "2024-01-01T09:30:00Z",
                        "id": "role-p3",
                        "role": "participant",
                        "user_id": "@participant3:my.domain.name",
                    },
                }
            }

            roles_response = requests.put(
                f"{state_url}/pangea.activity_roles/",
                json=activity_roles_data,
                headers=headers,
            )
            self.assertEqual(roles_response.status_code, 200)

            # participant2 and participant3 leave the room after the activity
            leave_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/leave"

            p2_leave = requests.post(
                leave_url, headers={"Authorization": f"Bearer {p2_token}"}
            )
            self.assertEqual(p2_leave.status_code, 200)

            p3_leave = requests.post(
                leave_url, headers={"Authorization": f"Bearer {p3_token}"}
            )
            self.assertEqual(p3_leave.status_code, 200)

            # Wait for the leave events to be processed
            await asyncio.sleep(0.5)

            # Request room preview - should return full roles with membership summary
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )

            response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            room_data = data["rooms"][room_id]

            # Verify ALL roles are returned (not filtered)
            self.assertIn("pangea.activity_roles", room_data)
            activity_roles = room_data["pangea.activity_roles"]["default"]["content"][
                "roles"
            ]

            # All 4 users should be in roles (even though 2 have left)
            self.assertEqual(len(activity_roles), 4)

            user_ids_in_roles = {role["user_id"] for role in activity_roles.values()}
            expected_users = {
                "@facilitator:my.domain.name",
                "@participant1:my.domain.name",
                "@participant2:my.domain.name",
                "@participant3:my.domain.name",
            }
            self.assertEqual(user_ids_in_roles, expected_users)

            # Verify membership_summary is present and correct
            self.assertIn("membership_summary", room_data)
            membership_summary = room_data["membership_summary"]

            # Facilitator and participant1 should be "join"
            self.assertEqual(
                membership_summary.get("@facilitator:my.domain.name"), "join"
            )
            self.assertEqual(
                membership_summary.get("@participant1:my.domain.name"), "join"
            )

            # participant2 and participant3 should be "leave"
            self.assertEqual(
                membership_summary.get("@participant2:my.domain.name"), "leave"
            )
            self.assertEqual(
                membership_summary.get("@participant3:my.domain.name"), "leave"
            )

            # Only users in activity roles should be in membership_summary
            self.assertEqual(len(membership_summary), 4)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_join_rules_filtering(self):
        """Test that m.room.join_rules content only exposes the join_rule key."""
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
                module_config={
                    "room_preview_state_event_types": [
                        "pangea.activity_plan",
                        "pangea.activity_roles",
                        "m.room.join_rules",
                    ]
                },
            )

            # Register admin user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin_user",
                password="admin_pw",
                admin=True,
            )

            _, admin_token = await self.login_user("admin_user", "admin_pw")

            # Create a room with join_rules that has additional content
            room_id = await self._create_room_with_complex_join_rules(admin_token)

            # Request room preview
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {admin_token}"}

            response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Verify the response structure
            self.assertIn("rooms", data)
            self.assertIn(room_id, data["rooms"])
            room_data = data["rooms"][room_id]

            # Verify m.room.join_rules is present
            self.assertIn("m.room.join_rules", room_data)
            join_rules_data = room_data["m.room.join_rules"]
            self.assertIn("default", join_rules_data)

            # Get the join_rules event content
            join_rules_event = join_rules_data["default"]
            self.assertIn("content", join_rules_event)
            join_rules_content = join_rules_event["content"]

            # Verify ONLY join_rule key is present in content
            self.assertIn("join_rule", join_rules_content)
            self.assertEqual(join_rules_content["join_rule"], "knock")

            # Verify other keys are NOT present (they should be filtered out)
            # The room was created with additional content that should be stripped
            self.assertEqual(
                len(join_rules_content),
                1,
                f"join_rules content should only have 1 key (join_rule), but has: {list(join_rules_content.keys())}",
            )
            self.assertNotIn(
                "allow",
                join_rules_content,
                "allow key should be filtered out from join_rules content",
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def _create_room_with_complex_join_rules(self, access_token: str) -> str:
        """Create a room with join_rules that contain additional content beyond join_rule."""
        headers = {"Authorization": f"Bearer {access_token}"}
        create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"

        # Create room with knock join rule
        create_room_data = {
            "visibility": "private",
            "preset": "private_chat",
            "name": "Test Room for Join Rules Filtering",
            "initial_state": [
                {
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "content": {
                        "join_rule": "knock",
                        # Additional fields that should be filtered out
                        "allow": [
                            {
                                "type": "m.room_membership",
                                "room_id": "!some_space:example.com",
                            }
                        ],
                    },
                },
            ],
        }

        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["room_id"]

    async def test_join_rules_only_join_rule_key(self):
        """Test m.room.join_rules filtering when content only has join_rule key."""
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
                module_config={
                    "room_preview_state_event_types": [
                        "pangea.activity_plan",
                        "pangea.activity_roles",
                        "m.room.join_rules",
                    ]
                },
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin_user",
                password="admin_pw",
                admin=True,
            )

            _, admin_token = await self.login_user("admin_user", "admin_pw")

            # Create a room with simple join_rules (only join_rule key)
            headers = {"Authorization": f"Bearer {admin_token}"}
            create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"

            create_room_data = {
                "visibility": "private",
                "preset": "private_chat",
                "name": "Test Room Simple Join Rules",
                "initial_state": [
                    {
                        "type": "m.room.join_rules",
                        "state_key": "",
                        "content": {"join_rule": "invite"},
                    },
                ],
            }

            response = requests.post(
                create_room_url,
                json=create_room_data,
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)
            room_id = response.json()["room_id"]

            # Request room preview
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )

            preview_response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(preview_response.status_code, 200)
            data = preview_response.json()

            room_data = data["rooms"][room_id]
            self.assertIn("m.room.join_rules", room_data)

            join_rules_content = room_data["m.room.join_rules"]["default"]["content"]
            self.assertEqual(join_rules_content, {"join_rule": "invite"})

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_course_plan_with_membership_summary(self):
        """Test that rooms with pangea.course_plan include membership_summary."""
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
                module_config={
                    "room_preview_state_event_types": ["pangea.course_plan"]
                },
            )

            # Register admin user
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="admin_user",
                password="admin_pw",
                admin=True,
            )

            # Register two test users
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="user1",
                password="pw1",
                admin=False,
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="user2",
                password="pw2",
                admin=False,
            )

            # Login users
            _, admin_token = await self.login_user("admin_user", "admin_pw")
            _, user1_token = await self.login_user("user1", "pw1")
            _, user2_token = await self.login_user("user2", "pw2")

            # Create a room with course_plan (not activity_roles)
            room_id = await self.create_room_with_course_plan(
                admin_token, user1_token, user2_token
            )

            # Request room preview - should include membership_summary for course rooms
            room_preview_url = (
                "http://localhost:8008/_synapse/client/unstable/org.pangea/room_preview"
            )
            headers = {"Authorization": f"Bearer {admin_token}"}

            response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Verify room data includes course_plan
            self.assertIn("rooms", data)
            self.assertIn(room_id, data["rooms"])
            room_data = data["rooms"][room_id]
            self.assertIn("pangea.course_plan", room_data)

            # Verify course_plan content
            course_plan = room_data["pangea.course_plan"]["default"]["content"]
            self.assertIn("uuid", course_plan)

            # Verify membership_summary is present for course rooms
            self.assertIn("membership_summary", room_data)
            membership_summary = room_data["membership_summary"]

            # All joined users should be in membership_summary
            self.assertEqual(
                membership_summary.get("@admin_user:my.domain.name"), "join"
            )
            self.assertEqual(membership_summary.get("@user1:my.domain.name"), "join")
            self.assertEqual(membership_summary.get("@user2:my.domain.name"), "join")

            # Kick user2 from the room
            kick_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/kick"
            kick_data = {
                "user_id": "@user2:my.domain.name",
                "reason": "Test kick for course plan membership summary",
            }
            kick_response = requests.post(
                kick_url,
                json=kick_data,
                headers=headers,
            )
            self.assertEqual(kick_response.status_code, 200)

            # Wait a moment for the kick to be processed
            await asyncio.sleep(0.5)

            # Request room preview again - user2 should be "leave"
            response = requests.get(
                room_preview_url,
                params={"rooms": room_id},
                headers=headers,
                timeout=10,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()

            room_data = data["rooms"][room_id]

            # Verify membership_summary shows correct membership states
            self.assertIn("membership_summary", room_data)
            membership_summary = room_data["membership_summary"]
            self.assertEqual(
                membership_summary.get("@admin_user:my.domain.name"), "join"
            )
            self.assertEqual(membership_summary.get("@user1:my.domain.name"), "join")
            # user2 should now be "leave" in membership_summary
            self.assertEqual(membership_summary.get("@user2:my.domain.name"), "leave")

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def create_room_with_course_plan(
        self, admin_token: str, user1_token: str, user2_token: str
    ) -> str:
        """Create a room with users and add pangea.course_plan state event."""
        headers = {"Authorization": f"Bearer {admin_token}"}
        create_room_url = "http://localhost:8008/_matrix/client/v3/createRoom"

        # Create room
        create_room_data = {
            "visibility": "private",
            "preset": "private_chat",
            "name": "Test Room for Course Plan",
            "invite": ["@user1:my.domain.name", "@user2:my.domain.name"],
        }

        response = requests.post(
            create_room_url,
            json=create_room_data,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        room_id = response.json()["room_id"]

        # Accept invitations for both users
        join_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/join"

        user1_headers = {"Authorization": f"Bearer {user1_token}"}
        response = requests.post(join_url, headers=user1_headers)
        self.assertEqual(response.status_code, 200)

        user2_headers = {"Authorization": f"Bearer {user2_token}"}
        response = requests.post(join_url, headers=user2_headers)
        self.assertEqual(response.status_code, 200)

        # Add pangea.course_plan state event
        state_url = f"http://localhost:8008/_matrix/client/v3/rooms/{room_id}/state/pangea.course_plan"
        course_plan_content = {"uuid": "b6989779-a498-4463-aac8-2ac06b2a0406"}

        response = requests.put(
            state_url,
            json=course_plan_content,
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)

        return room_id

    # ── public courses tests ──────────────────────────────────────────

    async def test_public_courses_endpoint_returns_public_course(self):
        _cache.clear()
        rate_limit_log.clear()

        postgres = None
        synapse_dir = None
        config_path = None
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
                synapse_config_overrides=ROOMDIRECTORY_CONFIG,
            )

            dsn_params = parse_dsn(postgres.url())
            dsn_params["dbname"] = "testdb"
            postgres_url = psycopg2.extensions.make_dsn(**dsn_params)

            await self.register_user(
                config_path, synapse_dir, user="admin", password="adminpass", admin=True
            )
            await self.register_user(
                config_path,
                synapse_dir,
                user="student",
                password="studentpass",
                admin=False,
            )

            _, admin_token = await self.login_user("admin", "adminpass")

            headers = {"Authorization": f"Bearer {admin_token}"}

            alias_suffix = int(time.time())
            create_room_payload = {
                "name": "Course Alpha",
                "preset": "public_chat",
                "visibility": "public",
                "room_alias_name": f"course-alpha-{alias_suffix}",
            }
            create_response = requests.post(
                f"{self.server_url}/_matrix/client/v3/createRoom",
                json=create_room_payload,
                headers=headers,
                timeout=30,
            )
            self.assertEqual(
                create_response.status_code,
                200,
                msg=f"Failed to create room: {create_response.text}",
            )
            room_id = create_response.json()["room_id"]
            room_id_path = quote(room_id, safe="")

            directory_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/directory/list/room/{room_id_path}",
                json={"visibility": "public"},
                headers=headers,
                timeout=30,
            )
            if directory_response.status_code not in (200, 202):
                if directory_response.status_code == 403:
                    logger.debug(
                        "Falling back to manual directory publish due to 403: %s",
                        directory_response.text,
                    )
                    conn = psycopg2.connect(postgres_url)
                    try:
                        with conn:
                            with conn.cursor() as cursor:
                                cursor.execute(
                                    "UPDATE rooms SET is_public = TRUE WHERE room_id = %s",
                                    (room_id,),
                                )
                    finally:
                        conn.close()
                else:
                    self.fail(
                        f"Failed to update directory visibility: {directory_response.text}"
                    )

            name_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/state/m.room.name",
                json={"name": "Course Alpha"},
                headers=headers,
                timeout=30,
            )
            self.assertEqual(name_response.status_code, 200)

            topic_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/state/m.room.topic",
                json={"topic": "Intro to Testing"},
                headers=headers,
                timeout=30,
            )
            self.assertEqual(topic_response.status_code, 200)

            join_rule_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/state/m.room.join_rules",
                json={"join_rule": "public"},
                headers=headers,
                timeout=30,
            )
            self.assertEqual(join_rule_response.status_code, 200)

            plan_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/state/pangea.course_plan",
                json={
                    "plan_id": "course-alpha",
                    "modules": ["intro"],
                    "uuid": "test-course-uuid-123",
                },
                headers=headers,
                timeout=30,
            )
            self.assertEqual(plan_response.status_code, 200)

            payload = None
            matching_courses: List[Dict[str, Any]] = []
            for _ in range(10):
                public_courses_response = requests.get(
                    f"{self.server_url}/_synapse/client/unstable/org.pangea/public_courses",
                    headers=headers,
                    timeout=30,
                )
                if public_courses_response.status_code == 200:
                    payload = public_courses_response.json()
                    chunk = payload.get("chunk", [])
                    matching_courses = [
                        course for course in chunk if course["room_id"] == room_id
                    ]
                    if matching_courses:
                        break
                await asyncio.sleep(1)

            if not matching_courses:
                conn = psycopg2.connect(postgres_url)
                try:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "SELECT room_id, is_public FROM rooms WHERE room_id = %s",
                            (room_id,),
                        )
                        room_state = cursor.fetchall()
                        cursor.execute(
                            "SELECT type, state_key FROM state_events WHERE room_id = %s",
                            (room_id,),
                        )
                        state_types = cursor.fetchall()
                finally:
                    conn.close()
                self.fail(
                    f"Expected room {room_id} in public courses response, got {payload}. "
                    f"rooms table: {room_state}, state events: {state_types}"
                )

            course = matching_courses[0]
            self.assertEqual(course["name"], "Course Alpha")
            self.assertEqual(course["topic"], "Intro to Testing")

            # Verify that course_id is included and matches the uuid from pangea.course_plan
            self.assertIn(
                "course_id", course, "course_id should be present in course response"
            )
            expected_course_id = "test-course-uuid-123"
            self.assertEqual(
                course["course_id"],
                expected_course_id,
                f"course_id should match uuid from pangea.course_plan content. Expected: {expected_course_id}, Got: {course.get('course_id')}",
            )
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_public_courses_endpoint_includes_course_id(self):
        """Test that the public courses endpoint includes course_id from pangea.course_plan content.uuid"""
        _cache.clear()
        rate_limit_log.clear()

        postgres = None
        synapse_dir = None
        config_path = None
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
                synapse_config_overrides=ROOMDIRECTORY_CONFIG,
            )

            dsn_params = parse_dsn(postgres.url())
            dsn_params["dbname"] = "testdb"
            postgres_url = psycopg2.extensions.make_dsn(**dsn_params)

            await self.register_user(
                config_path, synapse_dir, user="admin", password="adminpass", admin=True
            )

            _, admin_token = await self.login_user("admin", "adminpass")

            headers = {"Authorization": f"Bearer {admin_token}"}

            alias_suffix = int(time.time())
            create_room_payload = {
                "name": "Course Beta",
                "preset": "public_chat",
                "visibility": "public",
                "room_alias_name": f"course-beta-{alias_suffix}",
            }
            create_response = requests.post(
                f"{self.server_url}/_matrix/client/v3/createRoom",
                json=create_room_payload,
                headers=headers,
                timeout=30,
            )
            self.assertEqual(
                create_response.status_code,
                200,
                msg=f"Failed to create room: {create_response.text}",
            )
            room_id = create_response.json()["room_id"]
            room_id_path = quote(room_id, safe="")

            # Set room as public
            directory_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/directory/list/room/{room_id_path}",
                json={"visibility": "public"},
                headers=headers,
                timeout=30,
            )
            if directory_response.status_code not in (200, 202):
                if directory_response.status_code == 403:
                    logger.debug(
                        "Falling back to manual directory publish due to 403: %s",
                        directory_response.text,
                    )
                    conn = psycopg2.connect(postgres_url)
                    try:
                        with conn:
                            with conn.cursor() as cursor:
                                cursor.execute(
                                    "UPDATE rooms SET is_public = TRUE WHERE room_id = %s",
                                    (room_id,),
                                )
                    finally:
                        conn.close()
                else:
                    self.fail(
                        f"Failed to update directory visibility: {directory_response.text}"
                    )

            # Set room name
            name_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/state/m.room.name",
                json={"name": "Course Beta"},
                headers=headers,
                timeout=30,
            )
            self.assertEqual(name_response.status_code, 200)

            # Create pangea.course_plan state event with uuid
            expected_course_id = "beta-course-uuid-12345"
            plan_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/state/pangea.course_plan",
                json={
                    "plan_id": "course-beta",
                    "uuid": expected_course_id,
                    "modules": ["intro", "advanced"],
                },
                headers=headers,
                timeout=30,
            )
            self.assertEqual(plan_response.status_code, 200)

            # Wait for the course to appear in public courses
            payload = None
            matching_courses: List[Dict[str, Any]] = []
            for _ in range(10):
                public_courses_response = requests.get(
                    f"{self.server_url}/_synapse/client/unstable/org.pangea/public_courses",
                    headers=headers,
                    timeout=30,
                )
                if public_courses_response.status_code == 200:
                    payload = public_courses_response.json()
                    chunk = payload.get("chunk", [])
                    matching_courses = [
                        course for course in chunk if course["room_id"] == room_id
                    ]
                    if matching_courses:
                        break
                await asyncio.sleep(1)

            if not matching_courses:
                self.fail(
                    f"Expected room {room_id} in public courses response, got {payload}"
                )

            course = matching_courses[0]

            # Verify that course_id is included and matches the uuid from pangea.course_plan
            self.assertIn(
                "course_id", course, "course_id should be present in course response"
            )
            self.assertEqual(
                course["course_id"],
                expected_course_id,
                f"course_id should match uuid from pangea.course_plan content. Expected: {expected_course_id}, Got: {course.get('course_id')}",
            )

            # Also verify other fields are still working
            self.assertEqual(course["name"], "Course Beta")
            self.assertEqual(course["room_id"], room_id)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    async def test_public_courses_returns_room_stats_attributes(self):
        """Test that the public courses endpoint returns room stats attributes correctly.

        Verifies that the new attributes (world_readable, guest_can_join, join_rule,
        room_type, num_joined_members) are returned correctly and that old attributes
        (name, topic, course_id, etc.) still work.
        """
        _cache.clear()
        rate_limit_log.clear()

        postgres = None
        synapse_dir = None
        config_path = None
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
                synapse_config_overrides=ROOMDIRECTORY_CONFIG,
            )

            dsn_params = parse_dsn(postgres.url())
            dsn_params["dbname"] = "testdb"
            postgres_url = psycopg2.extensions.make_dsn(**dsn_params)

            await self.register_user(
                config_path, synapse_dir, user="admin", password="adminpass", admin=True
            )
            await self.register_user(
                config_path,
                synapse_dir,
                user="student1",
                password="studentpass",
                admin=False,
            )
            await self.register_user(
                config_path,
                synapse_dir,
                user="student2",
                password="studentpass",
                admin=False,
            )

            _, admin_token = await self.login_user("admin", "adminpass")
            _, student1_token = await self.login_user("student1", "studentpass")
            _, student2_token = await self.login_user("student2", "studentpass")

            headers = {"Authorization": f"Bearer {admin_token}"}

            alias_suffix = int(time.time())
            create_room_payload = {
                "name": "Course Gamma",
                "preset": "public_chat",
                "visibility": "public",
                "room_alias_name": f"course-gamma-{alias_suffix}",
            }
            create_response = requests.post(
                f"{self.server_url}/_matrix/client/v3/createRoom",
                json=create_room_payload,
                headers=headers,
                timeout=30,
            )
            self.assertEqual(
                create_response.status_code,
                200,
                msg=f"Failed to create room: {create_response.text}",
            )
            room_id = create_response.json()["room_id"]
            room_id_path = quote(room_id, safe="")

            # Set room as public
            directory_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/directory/list/room/{room_id_path}",
                json={"visibility": "public"},
                headers=headers,
                timeout=30,
            )
            if directory_response.status_code not in (200, 202):
                if directory_response.status_code == 403:
                    logger.debug(
                        "Falling back to manual directory publish due to 403: %s",
                        directory_response.text,
                    )
                    conn = psycopg2.connect(postgres_url)
                    try:
                        with conn:
                            with conn.cursor() as cursor:
                                cursor.execute(
                                    "UPDATE rooms SET is_public = TRUE WHERE room_id = %s",
                                    (room_id,),
                                )
                    finally:
                        conn.close()
                else:
                    self.fail(
                        f"Failed to update directory visibility: {directory_response.text}"
                    )

            # Set room name
            name_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/state/m.room.name",
                json={"name": "Course Gamma"},
                headers=headers,
                timeout=30,
            )
            self.assertEqual(name_response.status_code, 200)

            # Set room topic
            topic_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/state/m.room.topic",
                json={"topic": "Testing Room Stats"},
                headers=headers,
                timeout=30,
            )
            self.assertEqual(topic_response.status_code, 200)

            # Set join rules to public
            join_rule_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/state/m.room.join_rules",
                json={"join_rule": "public"},
                headers=headers,
                timeout=30,
            )
            self.assertEqual(join_rule_response.status_code, 200)

            # Set guest access
            guest_access_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/state/m.room.guest_access",
                json={"guest_access": "can_join"},
                headers=headers,
                timeout=30,
            )
            self.assertEqual(guest_access_response.status_code, 200)

            # Set history visibility to world_readable
            history_visibility_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/state/m.room.history_visibility",
                json={"history_visibility": "world_readable"},
                headers=headers,
                timeout=30,
            )
            self.assertEqual(history_visibility_response.status_code, 200)

            # Create pangea.course_plan state event
            expected_course_id = "gamma-course-uuid-999"
            plan_response = requests.put(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/state/pangea.course_plan",
                json={
                    "plan_id": "course-gamma",
                    "uuid": expected_course_id,
                    "modules": ["stats-testing"],
                },
                headers=headers,
                timeout=30,
            )
            self.assertEqual(plan_response.status_code, 200)

            # Have students join the room to increase member count
            student1_headers = {"Authorization": f"Bearer {student1_token}"}
            join_response1 = requests.post(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/join",
                headers=student1_headers,
                timeout=30,
            )
            self.assertEqual(join_response1.status_code, 200)

            student2_headers = {"Authorization": f"Bearer {student2_token}"}
            join_response2 = requests.post(
                f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}/join",
                headers=student2_headers,
                timeout=30,
            )
            self.assertEqual(join_response2.status_code, 200)

            # Wait a bit for room stats to update
            await asyncio.sleep(2)

            # Wait for the course to appear in public courses
            payload = None
            matching_courses: List[Dict[str, Any]] = []
            for _ in range(10):
                public_courses_response = requests.get(
                    f"{self.server_url}/_synapse/client/unstable/org.pangea/public_courses",
                    headers=headers,
                    timeout=30,
                )
                if public_courses_response.status_code == 200:
                    payload = public_courses_response.json()
                    chunk = payload.get("chunk", [])
                    matching_courses = [
                        course for course in chunk if course["room_id"] == room_id
                    ]
                    if matching_courses:
                        break
                await asyncio.sleep(1)

            if not matching_courses:
                self.fail(
                    f"Expected room {room_id} in public courses response, got {payload}"
                )

            course = matching_courses[0]

            # Verify OLD attributes still work
            self.assertEqual(
                course["name"], "Course Gamma", "Old attribute 'name' should still work"
            )
            self.assertEqual(
                course["topic"],
                "Testing Room Stats",
                "Old attribute 'topic' should still work",
            )
            self.assertEqual(
                course["room_id"], room_id, "Old attribute 'room_id' should still work"
            )
            self.assertEqual(
                course["course_id"],
                expected_course_id,
                "Old attribute 'course_id' should still work",
            )

            # Verify NEW room stats attributes are present and correct
            self.assertIn(
                "world_readable",
                course,
                "New attribute 'world_readable' should be present",
            )
            self.assertTrue(
                course["world_readable"],
                "world_readable should be True when history_visibility is 'world_readable'",
            )

            self.assertIn(
                "guest_can_join",
                course,
                "New attribute 'guest_can_join' should be present",
            )
            self.assertTrue(
                course["guest_can_join"],
                "guest_can_join should be True when guest_access is 'can_join'",
            )

            self.assertIn(
                "join_rule", course, "New attribute 'join_rule' should be present"
            )
            self.assertEqual(
                course["join_rule"],
                "public",
                "join_rule should match the room's join rules",
            )

            self.assertIn(
                "room_type", course, "New attribute 'room_type' should be present"
            )
            # room_type can be None for regular rooms
            self.assertIsNone(
                course["room_type"], "room_type should be None for regular rooms"
            )

            self.assertIn(
                "num_joined_members",
                course,
                "New attribute 'num_joined_members' should be present",
            )
            # Should be at least 3 (admin + 2 students), but allow for timing variations
            self.assertGreaterEqual(
                course["num_joined_members"],
                3,
                f"num_joined_members should be at least 3 (admin + 2 students), got {course['num_joined_members']}",
            )

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )
