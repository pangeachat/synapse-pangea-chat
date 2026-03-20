"""Unit tests for public course query fallback and edge-case behavior."""

import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from synapse_pangea_chat.config import PangeaChatConfig
from synapse_pangea_chat.public_courses.course_metadata_cache import (
    FilteredCourseMetadataLookupError,
)
from synapse_pangea_chat.public_courses.types import PublicCoursesResponse

get_public_courses_module = importlib.import_module(
    "synapse_pangea_chat.public_courses.get_public_courses"
)


class TestGetPublicCourses(unittest.IsolatedAsyncioTestCase):
    def _make_room_store(self, execute_side_effect):
        db_pool = SimpleNamespace(execute=AsyncMock(side_effect=execute_side_effect))
        return SimpleNamespace(db_pool=db_pool)

    async def test_invalid_since_and_non_positive_limit_are_normalized(self) -> None:
        async def execute_side_effect(name, *_args):
            if name == "get_public_courses_total_count":
                return [(3,)]
            raise AssertionError(f"Unexpected query name: {name}")

        room_store = self._make_room_store(execute_side_effect)
        config = PangeaChatConfig()

        sentinel = PublicCoursesResponse(
            chunk=[],
            filtering_warning="",
            next_batch=None,
            prev_batch=None,
            total_room_count_estimate=3,
        )

        with patch.object(
            get_public_courses_module,
            "_get_unfiltered_public_courses",
            new=AsyncMock(return_value=sentinel),
        ) as mock_unfiltered:
            result = await get_public_courses_module.get_public_courses(
                room_store=room_store,
                config=config,
                limit=0,
                since="not-an-int",
                filters=None,
            )

        self.assertEqual(result, sentinel)
        await_args = mock_unfiltered.await_args
        assert await_args is not None
        # args: room_store, config, limit, start_index, total_room_count, ...
        self.assertEqual(await_args.args[2], 10)
        self.assertEqual(await_args.args[3], 0)
        self.assertEqual(await_args.args[4], 3)
        self.assertEqual(result["filtering_warning"], "")

    async def test_filtered_path_falls_back_when_cms_not_configured(self) -> None:
        async def execute_side_effect(name, *_args):
            if name == "get_public_courses_all_room_ids":
                return [("!room1:test",)]
            raise AssertionError(f"Unexpected query name: {name}")

        room_store = self._make_room_store(execute_side_effect)
        config = PangeaChatConfig(cms_base_url="", cms_service_api_key="")

        sentinel = PublicCoursesResponse(
            chunk=[],
            filtering_warning="",
            next_batch="10",
            prev_batch=None,
            total_room_count_estimate=1,
        )

        rooms_data = {
            "!room1:test": {
                "pangea.course_plan": {
                    "": {
                        "content": {
                            "uuid": "uuid-1",
                        }
                    }
                }
            }
        }

        with patch.object(
            get_public_courses_module,
            "_fetch_room_state",
            new=AsyncMock(return_value=rooms_data),
        ), patch.object(
            get_public_courses_module,
            "_get_unfiltered_public_courses",
            new=AsyncMock(return_value=sentinel),
        ) as mock_unfiltered:
            result = await get_public_courses_module._get_filtered_public_courses(
                room_store=room_store,
                config=config,
                limit=10,
                start_index=0,
                total_room_count=1,
                required_course_event_type="pangea.course_plan",
                event_types_with_required=["pangea.course_plan"],
                filters={"target_language": "es"},
            )

        self.assertEqual(result["chunk"], sentinel["chunk"])
        self.assertEqual(result["next_batch"], sentinel["next_batch"])
        self.assertEqual(result["prev_batch"], sentinel["prev_batch"])
        self.assertEqual(
            result["total_room_count_estimate"],
            sentinel["total_room_count_estimate"],
        )
        mock_unfiltered.assert_awaited_once()
        self.assertIn(
            "Language filters could not be applied", result["filtering_warning"]
        )
        self.assertIn("not configured", result["filtering_warning"])

    async def test_filtered_path_falls_back_when_cms_lookup_raises(self) -> None:
        async def execute_side_effect(name, *_args):
            if name == "get_public_courses_all_room_ids":
                return [("!room1:test",)]
            raise AssertionError(f"Unexpected query name: {name}")

        room_store = self._make_room_store(execute_side_effect)
        config = PangeaChatConfig(
            cms_base_url="https://cms.test",
            cms_service_api_key="key",
        )

        sentinel = PublicCoursesResponse(
            chunk=[],
            filtering_warning="",
            next_batch=None,
            prev_batch=None,
            total_room_count_estimate=1,
        )

        rooms_data = {
            "!room1:test": {
                "pangea.course_plan": {
                    "": {
                        "content": {
                            "uuid": "uuid-1",
                        }
                    }
                }
            }
        }

        with patch.object(
            get_public_courses_module,
            "_fetch_room_state",
            new=AsyncMock(return_value=rooms_data),
        ), patch.object(
            get_public_courses_module,
            "get_filtered_course_ids",
            new=AsyncMock(side_effect=FilteredCourseMetadataLookupError("boom")),
        ), patch.object(
            get_public_courses_module,
            "_get_unfiltered_public_courses",
            new=AsyncMock(return_value=sentinel),
        ) as mock_unfiltered:
            result = await get_public_courses_module._get_filtered_public_courses(
                room_store=room_store,
                config=config,
                limit=10,
                start_index=0,
                total_room_count=1,
                required_course_event_type="pangea.course_plan",
                event_types_with_required=["pangea.course_plan"],
                filters={"target_language": "es"},
            )

        self.assertEqual(result["chunk"], sentinel["chunk"])
        self.assertEqual(result["next_batch"], sentinel["next_batch"])
        self.assertEqual(result["prev_batch"], sentinel["prev_batch"])
        self.assertEqual(
            result["total_room_count_estimate"],
            sentinel["total_room_count_estimate"],
        )
        mock_unfiltered.assert_awaited_once()
        self.assertIn(
            "Language filters could not be applied", result["filtering_warning"]
        )
        self.assertIn("lookup failed", result["filtering_warning"])

    async def test_filtered_results_are_sorted_and_paginated_stably(self) -> None:
        async def execute_side_effect(name, *_args):
            if name == "get_public_courses_all_room_ids":
                # Deliberately unsorted to prove stable sorting in response path.
                return [("!roomB:test",), ("!roomA:test",)]
            raise AssertionError(f"Unexpected query name: {name}")

        room_store = self._make_room_store(execute_side_effect)
        config = PangeaChatConfig(
            cms_base_url="https://cms.test",
            cms_service_api_key="key",
        )

        rooms_data = {
            "!roomA:test": {
                "pangea.course_plan": {"": {"content": {"uuid": "uuid-a"}}}
            },
            "!roomB:test": {
                "pangea.course_plan": {"": {"content": {"uuid": "uuid-b"}}}
            },
        }

        matched_meta = {
            # Deliberately reversed key order compared with sorted room IDs.
            "uuid-b": {"l2": "es", "l1": "en", "cefr_level": "A1"},
            "uuid-a": {"l2": "es", "l1": "en", "cefr_level": "A1"},
        }

        with patch.object(
            get_public_courses_module,
            "_fetch_room_state",
            new=AsyncMock(return_value=rooms_data),
        ), patch.object(
            get_public_courses_module,
            "get_filtered_course_ids",
            new=AsyncMock(return_value=matched_meta),
        ), patch.object(
            get_public_courses_module,
            "_fetch_room_stats",
            new=AsyncMock(return_value={}),
        ):
            first_page = await get_public_courses_module._get_filtered_public_courses(
                room_store=room_store,
                config=config,
                limit=1,
                start_index=0,
                total_room_count=2,
                required_course_event_type="pangea.course_plan",
                event_types_with_required=["pangea.course_plan"],
                filters={"target_language": "es"},
            )

            second_page = await get_public_courses_module._get_filtered_public_courses(
                room_store=room_store,
                config=config,
                limit=1,
                start_index=1,
                total_room_count=2,
                required_course_event_type="pangea.course_plan",
                event_types_with_required=["pangea.course_plan"],
                filters={"target_language": "es"},
            )

        self.assertEqual(first_page["total_room_count_estimate"], 2)
        self.assertEqual(first_page["filtering_warning"], "")
        self.assertEqual(first_page["next_batch"], "1")
        self.assertIsNone(first_page["prev_batch"])
        self.assertEqual(first_page["chunk"][0]["room_id"], "!roomA:test")

        self.assertEqual(second_page["total_room_count_estimate"], 2)
        self.assertEqual(second_page["filtering_warning"], "")
        self.assertIsNone(second_page["next_batch"])
        self.assertEqual(second_page["prev_batch"], "0")
        self.assertEqual(second_page["chunk"][0]["room_id"], "!roomB:test")


if __name__ == "__main__":
    unittest.main()
