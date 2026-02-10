"""
Staging smoke-tests for the unified ``synapse_pangea_chat.PangeaChat`` module.

Run:
    python -m unittest tests.staging_tests.staging_tests

Prerequisites:
    pip install aiohttp
    cp tests/staging_tests/.env.example tests/staging_tests/.env
    # fill in SYNAPSE_BASE_URL and SYNAPSE_AUTH_TOKEN
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Dict, List, Optional

import aiohttp

# ---------------------------------------------------------------------------
# .env loader — reads from the staging_tests directory
# ---------------------------------------------------------------------------

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_env_file() -> None:
    if not os.path.isfile(_ENV_PATH):
        return
    with open(_ENV_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if sep:
                os.environ.setdefault(key.strip(), value.strip())


_load_env_file()

SYNAPSE_BASE_URL: str = os.environ.get("SYNAPSE_BASE_URL", "").rstrip("/")
SYNAPSE_AUTH_TOKEN: str = os.environ.get("SYNAPSE_AUTH_TOKEN", "")

if not SYNAPSE_AUTH_TOKEN:
    sys.exit(
        "FATAL: SYNAPSE_AUTH_TOKEN not set. "
        "Add it to tests/staging_tests/.env or export it."
    )

if not SYNAPSE_BASE_URL:
    sys.exit(
        "FATAL: SYNAPSE_BASE_URL not set. "
        "Add it to tests/staging_tests/.env or export it."
    )


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------


class StagingSmokeTests(unittest.IsolatedAsyncioTestCase):
    """Non-destructive smoke-tests against a live staging Synapse server.

    Validates every endpoint registered by ``synapse_pangea_chat.PangeaChat``:
      1. Public Courses
      2. Room Preview
      3. Room Code (knock_with_code / request_room_code)
      4. Delete Room (auth-only; no actual deletion)
      5. Limit User Directory
      6. General health
    """

    # ── lifecycle ──────────────────────────────────────────────────

    async def asyncSetUp(self) -> None:
        self.base_url = SYNAPSE_BASE_URL
        self.auth_headers = {"Authorization": f"Bearer {SYNAPSE_AUTH_TOKEN}"}
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self) -> None:
        await self.session.close()

    # ── helpers ────────────────────────────────────────────────────

    async def _get(
        self,
        path: str,
        *,
        auth: bool = True,
        params: Optional[Dict[str, str]] = None,
    ) -> aiohttp.ClientResponse:
        headers = dict(self.auth_headers) if auth else {}
        return await self.session.get(
            f"{self.base_url}{path}", headers=headers, params=params
        )

    async def _post(
        self,
        path: str,
        *,
        auth: bool = True,
        json: Optional[Dict[str, Any]] = None,
    ) -> aiohttp.ClientResponse:
        headers = dict(self.auth_headers) if auth else {}
        return await self.session.post(
            f"{self.base_url}{path}", headers=headers, json=json
        )

    # helper: fetch first public course room_id (or skip)
    async def _first_public_course_room_id(self) -> str:
        resp = await self._get(
            "/_synapse/client/unstable/org.pangea/public_courses",
            params={"limit": "1"},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        chunk: List[Dict[str, Any]] = data.get("chunk", [])
        if not chunk:
            self.skipTest("No public courses on staging — cannot run this test")
        return chunk[0]["room_id"]

    # ══════════════════════════════════════════════════════════════
    #  1. Public Courses
    # ══════════════════════════════════════════════════════════════

    async def test_public_courses_requires_auth(self) -> None:
        """Unauthenticated request → 401."""
        resp = await self._get(
            "/_synapse/client/unstable/org.pangea/public_courses", auth=False
        )
        self.assertEqual(resp.status, 401)

    async def test_public_courses_returns_valid_structure(self) -> None:
        """Response contains chunk, next_batch, prev_batch, total_room_count_estimate."""
        resp = await self._get("/_synapse/client/unstable/org.pangea/public_courses")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        for key in ("chunk", "next_batch", "prev_batch", "total_room_count_estimate"):
            self.assertIn(key, data, f"Missing top-level key: {key}")
        self.assertIsInstance(data["chunk"], list)

    async def test_public_courses_course_fields(self) -> None:
        """Every course has the full set of expected keys."""
        resp = await self._get(
            "/_synapse/client/unstable/org.pangea/public_courses",
            params={"limit": "5"},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        expected_keys = {
            "room_id",
            "name",
            "topic",
            "avatar_url",
            "canonical_alias",
            "course_id",
            "num_joined_members",
            "world_readable",
            "guest_can_join",
            "join_rule",
            "room_type",
        }
        for course in data["chunk"]:
            missing = expected_keys - set(course.keys())
            self.assertFalse(
                missing,
                f"Course {course.get('room_id')} missing keys: {missing}",
            )

    async def test_public_courses_pagination(self) -> None:
        """limit / since pagination returns successive pages."""
        resp = await self._get(
            "/_synapse/client/unstable/org.pangea/public_courses",
            params={"limit": "1"},
        )
        self.assertEqual(resp.status, 200)
        page1 = await resp.json()
        self.assertLessEqual(len(page1["chunk"]), 1)

        next_batch = page1.get("next_batch")
        if next_batch is None:
            self.skipTest("Only one page of courses — pagination not testable")

        resp2 = await self._get(
            "/_synapse/client/unstable/org.pangea/public_courses",
            params={"limit": "1", "since": str(next_batch)},
        )
        self.assertEqual(resp2.status, 200)
        page2 = await resp2.json()
        self.assertIsInstance(page2["chunk"], list)

    async def test_public_courses_course_id_matches_course_plan(self) -> None:
        """course_id matches the uuid from the room's pangea.course_plan state event."""
        room_id = await self._first_public_course_room_id()

        # Fetch the course_id from public_courses
        resp = await self._get(
            "/_synapse/client/unstable/org.pangea/public_courses",
            params={"limit": "50"},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        course = next((c for c in data["chunk"] if c["room_id"] == room_id), None)
        self.assertIsNotNone(course, "Room not found in public courses")
        assert course is not None  # type narrowing
        course_id = course.get("course_id")

        # Fetch course_plan from room_preview
        resp_preview = await self._get(
            "/_synapse/client/unstable/org.pangea/room_preview",
            params={"rooms": room_id},
        )
        self.assertEqual(resp_preview.status, 200)
        preview = await resp_preview.json()
        room_data = preview.get("rooms", {}).get(room_id, {})
        course_plan_events = room_data.get("pangea.course_plan", {})
        if not course_plan_events:
            self.skipTest("Room has no pangea.course_plan state event")
        # Get the first state key's content
        first_event = next(iter(course_plan_events.values()))
        content = (
            first_event.get("content", {}) if isinstance(first_event, dict) else {}
        )
        uuid_from_state = content.get("uuid")
        self.assertEqual(
            course_id,
            uuid_from_state,
            "course_id should match pangea.course_plan uuid",
        )

    # ══════════════════════════════════════════════════════════════
    #  2. Room Preview
    # ══════════════════════════════════════════════════════════════

    async def test_room_preview_requires_auth(self) -> None:
        """Unauthenticated request → 401."""
        resp = await self._get(
            "/_synapse/client/unstable/org.pangea/room_preview",
            auth=False,
            params={"rooms": "!fake:staging.pangea.chat"},
        )
        self.assertEqual(resp.status, 401)

    async def test_room_preview_empty_rooms_param(self) -> None:
        """Empty rooms parameter → {"rooms": {}}."""
        resp = await self._get(
            "/_synapse/client/unstable/org.pangea/room_preview",
            params={"rooms": ""},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data, {"rooms": {}})

    async def test_room_preview_nonexistent_room(self) -> None:
        """Non-existent room ID → empty entry (not an error)."""
        resp = await self._get(
            "/_synapse/client/unstable/org.pangea/room_preview",
            params={"rooms": "!nonexistent000:staging.pangea.chat"},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("rooms", data)

    async def test_room_preview_known_room(self) -> None:
        """Preview of a public-course room returns state event data."""
        room_id = await self._first_public_course_room_id()
        resp = await self._get(
            "/_synapse/client/unstable/org.pangea/room_preview",
            params={"rooms": room_id},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("rooms", data)
        room_data = data["rooms"].get(room_id, {})
        self.assertIsInstance(room_data, dict)
        # Should contain at least one configured state event type
        self.assertTrue(
            len(room_data) > 0,
            f"Expected state events for room {room_id}, got empty dict",
        )

    async def test_room_preview_join_rules_filtered(self) -> None:
        """m.room.join_rules content only exposes the 'join_rule' key."""
        room_id = await self._first_public_course_room_id()
        resp = await self._get(
            "/_synapse/client/unstable/org.pangea/room_preview",
            params={"rooms": room_id},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        room_data = data.get("rooms", {}).get(room_id, {})
        join_rules_events = room_data.get("m.room.join_rules", {})
        if not join_rules_events:
            self.skipTest("Room has no m.room.join_rules event")

        for _state_key, event in join_rules_events.items():
            content = event.get("content", {}) if isinstance(event, dict) else {}
            # Only 'join_rule' should be present (no access_code, etc.)
            self.assertIn("join_rule", content)
            disallowed = set(content.keys()) - {"join_rule"}
            self.assertFalse(
                disallowed,
                f"join_rules content leaks extra keys: {disallowed}",
            )

    async def test_room_preview_membership_summary(self) -> None:
        """Rooms with activity roles or course plan include membership_summary."""
        room_id = await self._first_public_course_room_id()
        resp = await self._get(
            "/_synapse/client/unstable/org.pangea/room_preview",
            params={"rooms": room_id},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        room_data = data.get("rooms", {}).get(room_id, {})
        has_activity_roles = "pangea.activity_roles" in room_data
        has_course_plan = "pangea.course_plan" in room_data
        if has_activity_roles or has_course_plan:
            self.assertIn(
                "membership_summary",
                room_data,
                "membership_summary should be present when activity_roles or course_plan exists",
            )
            self.assertIsInstance(room_data["membership_summary"], dict)

    # ══════════════════════════════════════════════════════════════
    #  3. Room Code
    # ══════════════════════════════════════════════════════════════

    async def test_knock_with_code_requires_auth(self) -> None:
        """Unauthenticated POST → 403."""
        resp = await self._post(
            "/_synapse/client/pangea/v1/knock_with_code",
            auth=False,
            json={"access_code": "abc123d"},
        )
        self.assertEqual(resp.status, 403)

    async def test_knock_with_code_invalid_format(self) -> None:
        """Invalid access-code format → 400."""
        resp = await self._post(
            "/_synapse/client/pangea/v1/knock_with_code",
            json={"access_code": "bad"},
        )
        self.assertEqual(resp.status, 400)

    async def test_knock_with_code_missing_body(self) -> None:
        """Missing access_code in body → 400."""
        resp = await self._post(
            "/_synapse/client/pangea/v1/knock_with_code",
            json={},
        )
        self.assertEqual(resp.status, 400)

    async def test_request_room_code_requires_auth(self) -> None:
        """Unauthenticated GET → 403."""
        resp = await self._get(
            "/_synapse/client/pangea/v1/request_room_code", auth=False
        )
        self.assertEqual(resp.status, 403)

    async def test_request_room_code_returns_code(self) -> None:
        """Authenticated GET returns a 7-char alphanumeric access_code."""
        resp = await self._get("/_synapse/client/pangea/v1/request_room_code")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("access_code", data)
        code = data["access_code"]
        self.assertEqual(len(code), 7, f"Expected 7-char code, got {code!r}")
        self.assertTrue(code.isalnum(), f"Code is not alphanumeric: {code!r}")

    # ══════════════════════════════════════════════════════════════
    #  4. Delete Room (auth smoke-test only — no actual deletion)
    # ══════════════════════════════════════════════════════════════

    async def test_delete_room_requires_auth(self) -> None:
        """Unauthenticated POST → 403."""
        resp = await self._post(
            "/_synapse/client/pangea/v1/delete_room",
            auth=False,
            json={"room_id": "!fake:staging.pangea.chat"},
        )
        self.assertEqual(resp.status, 403)

    async def test_delete_room_non_member(self) -> None:
        """Deleting a room user is not a member of → 400."""
        resp = await self._post(
            "/_synapse/client/pangea/v1/delete_room",
            json={"room_id": "!nonexistent000:staging.pangea.chat"},
        )
        self.assertEqual(resp.status, 400)

    async def test_delete_room_missing_room_id(self) -> None:
        """Missing room_id in body → 400."""
        resp = await self._post(
            "/_synapse/client/pangea/v1/delete_room",
            json={},
        )
        self.assertEqual(resp.status, 400)

    # ══════════════════════════════════════════════════════════════
    #  5. Limit User Directory
    # ══════════════════════════════════════════════════════════════

    async def test_user_directory_search_returns_valid_structure(self) -> None:
        """User directory search endpoint returns {results: [...]}."""
        resp = await self._post(
            "/_matrix/client/v3/user_directory/search",
            json={"limit": 5, "search_term": "bot"},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("results", data)
        self.assertIsInstance(data["results"], list)


"""
python -m unittest tests.staging_tests.staging_tests
"""
if __name__ == "__main__":
    unittest.main()
