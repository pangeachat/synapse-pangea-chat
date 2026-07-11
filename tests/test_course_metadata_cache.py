"""Unit tests for course_metadata_cache module."""

import time
import unittest
from unittest.mock import AsyncMock, patch

from synapse_pangea_chat.public_courses.course_metadata_cache import (
    CourseMeta,
    FilteredCourseMetadataLookupError,
    _meta_cache,
    _parse_docs,
    _store,
    get_course_metadata,
    get_filtered_course_ids,
)


class TestParseDocs(unittest.TestCase):
    def setUp(self) -> None:
        _meta_cache.clear()

    def test_parses_valid_docs(self) -> None:
        docs = [
            {"id": "uuid-1", "l2": "es", "originalL1": "en", "cefrLevel": "A1"},
            {"id": "uuid-2", "l2": "fr", "originalL1": "de", "cefrLevel": "B2"},
        ]
        result = _parse_docs(docs)
        self.assertEqual(len(result), 2)
        self.assertEqual(result["uuid-1"]["l2"], "es")
        self.assertEqual(result["uuid-1"]["l1"], "en")
        self.assertEqual(result["uuid-1"]["cefr_level"], "A1")
        self.assertEqual(result["uuid-2"]["l2"], "fr")

    def test_skips_docs_without_id(self) -> None:
        docs = [
            {"l2": "es", "originalL1": "en", "cefrLevel": "A1"},
            {"id": "uuid-1", "l2": "fr", "originalL1": "de", "cefrLevel": "B2"},
        ]
        result = _parse_docs(docs)
        self.assertEqual(len(result), 1)
        self.assertIn("uuid-1", result)

    def test_defaults_for_missing_fields(self) -> None:
        docs = [{"id": "uuid-1"}]
        result = _parse_docs(docs)
        self.assertEqual(result["uuid-1"]["l2"], "")
        self.assertEqual(result["uuid-1"]["l1"], "")
        self.assertEqual(result["uuid-1"]["cefr_level"], "")

    def test_caches_parsed_docs(self) -> None:
        docs = [{"id": "uuid-1", "l2": "es", "originalL1": "en", "cefrLevel": "A1"}]
        _parse_docs(docs)
        self.assertIn("uuid-1", _meta_cache)


class TestGetCourseMetadata(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _meta_cache.clear()

    @patch("synapse_pangea_chat.public_courses.course_metadata_cache._cms_get")
    async def test_fetches_from_cms(self, mock_cms_get: AsyncMock) -> None:
        mock_cms_get.return_value = [
            {"id": "uuid-1", "l2": "es", "originalL1": "en", "cefrLevel": "A1"},
        ]
        result = await get_course_metadata(
            ["uuid-1"], "https://cms.test", "api-key-123"
        )
        self.assertEqual(result["uuid-1"]["l2"], "es")
        mock_cms_get.assert_called_once()
        # Verify where[id][in] was used
        call_args = mock_cms_get.call_args
        params = call_args[1].get("query_params") or call_args[0][2]
        self.assertIn("where[id][in]", params)
        self.assertEqual(params["where[id][in]"], "uuid-1")
        self.assertEqual(params["limit"], "1")
        self.assertEqual(params["depth"], "0")

    @patch("synapse_pangea_chat.public_courses.course_metadata_cache._cms_get")
    async def test_uses_cache_for_fresh_entries(self, mock_cms_get: AsyncMock) -> None:
        # Pre-populate cache
        _store("uuid-1", CourseMeta(l2="es", l1="en", cefr_level="A1"))

        result = await get_course_metadata(
            ["uuid-1"], "https://cms.test", "api-key-123", cache_ttl=300
        )
        self.assertEqual(result["uuid-1"]["l2"], "es")
        mock_cms_get.assert_not_called()

    @patch("synapse_pangea_chat.public_courses.course_metadata_cache._cms_get")
    async def test_fetches_only_stale_uuids(self, mock_cms_get: AsyncMock) -> None:
        _store("uuid-1", CourseMeta(l2="es", l1="en", cefr_level="A1"))

        mock_cms_get.return_value = [
            {"id": "uuid-2", "l2": "fr", "originalL1": "de", "cefrLevel": "B2"},
        ]

        result = await get_course_metadata(
            ["uuid-1", "uuid-2"], "https://cms.test", "api-key-123", cache_ttl=300
        )
        self.assertEqual(len(result), 2)
        # Only uuid-2 should have been fetched
        call_args = mock_cms_get.call_args
        params = call_args[1].get("query_params") or call_args[0][2]
        self.assertEqual(params["where[id][in]"], "uuid-2")

    @patch("synapse_pangea_chat.public_courses.course_metadata_cache._cms_get")
    async def test_returns_empty_for_empty_input(self, mock_cms_get: AsyncMock) -> None:
        result = await get_course_metadata([], "https://cms.test", "api-key-123")
        self.assertEqual(result, {})
        mock_cms_get.assert_not_called()

    @patch("synapse_pangea_chat.public_courses.course_metadata_cache._cms_get")
    async def test_falls_back_to_stale_cache_on_error(
        self, mock_cms_get: AsyncMock
    ) -> None:
        # Store with old timestamp (expired)
        _meta_cache["uuid-1"] = (
            CourseMeta(l2="es", l1="en", cefr_level="A1"),
            time.time() - 999,
        )

        mock_cms_get.side_effect = RuntimeError("CMS down")

        result = await get_course_metadata(
            ["uuid-1"], "https://cms.test", "api-key-123", cache_ttl=300
        )
        # Should return stale data rather than empty
        self.assertEqual(result["uuid-1"]["l2"], "es")

    @patch("synapse_pangea_chat.public_courses.course_metadata_cache._cms_get")
    async def test_missing_metadata_is_not_fabricated(
        self, mock_cms_get: AsyncMock
    ) -> None:
        mock_cms_get.return_value = []

        result = await get_course_metadata(
            ["uuid-1"], "https://cms.test", "api-key-123", cache_ttl=300
        )

        self.assertEqual(result, {})


class TestGetFilteredCourseIds(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _meta_cache.clear()

    @patch("synapse_pangea_chat.public_courses.course_metadata_cache._cms_get")
    async def test_cefr_delegated_languages_filtered_module_side(
        self, mock_cms_get: AsyncMock
    ) -> None:
        # Language filters must NOT reach CMS as `equals` clauses — exact
        # equality drops regionally-tagged courses (issue #53). Only the
        # exact-valued CEFR filter is delegated; languages are matched
        # module-side by base language.
        mock_cms_get.return_value = [
            {"id": "uuid-1", "l2": "es", "originalL1": "en", "cefrLevel": "A1"},
            {"id": "uuid-2", "l2": "fr", "originalL1": "en", "cefrLevel": "A1"},
        ]
        result = await get_filtered_course_ids(
            ["uuid-1", "uuid-2"],
            "https://cms.test",
            "api-key-123",
            target_language="es",
            language_of_instructions="en",
            cefr_level="A1",
        )
        self.assertEqual(len(result), 1)
        self.assertIn("uuid-1", result)

        call_args = mock_cms_get.call_args
        params = call_args[1].get("query_params") or call_args[0][2]
        self.assertNotIn("where[l2][equals]", params)
        self.assertNotIn("where[originalL1][equals]", params)
        self.assertEqual(params["where[cefrLevel][equals]"], "A1")
        self.assertEqual(params["limit"], "2")
        self.assertEqual(params["depth"], "0")
        self.assertIn("uuid-1", params["where[id][in]"])
        self.assertIn("uuid-2", params["where[id][in]"])

    @patch("synapse_pangea_chat.public_courses.course_metadata_cache._cms_get")
    async def test_base_language_matches_regional_l2(
        self, mock_cms_get: AsyncMock
    ) -> None:
        # The issue-#53 repro at unit level: `es` must match `es-ES`, and
        # a regional filter must match across the base language.
        mock_cms_get.return_value = [
            {"id": "uuid-1", "l2": "es-ES", "originalL1": "en", "cefrLevel": "A1"},
            {"id": "uuid-2", "l2": "es", "originalL1": "en", "cefrLevel": "A1"},
            {"id": "uuid-3", "l2": "fr", "originalL1": "en", "cefrLevel": "A1"},
        ]
        result = await get_filtered_course_ids(
            ["uuid-1", "uuid-2", "uuid-3"],
            "https://cms.test",
            "api-key-123",
            target_language="es",
        )
        self.assertEqual(sorted(result), ["uuid-1", "uuid-2"])

        _meta_cache.clear()
        result = await get_filtered_course_ids(
            ["uuid-1", "uuid-2", "uuid-3"],
            "https://cms.test",
            "api-key-123",
            target_language="es-MX",
        )
        self.assertEqual(sorted(result), ["uuid-1", "uuid-2"])

    @patch("synapse_pangea_chat.public_courses.course_metadata_cache._cms_get")
    async def test_partial_filters(self, mock_cms_get: AsyncMock) -> None:
        mock_cms_get.return_value = [
            {"id": "uuid-1", "l2": "es", "originalL1": "en", "cefrLevel": "B1"},
            {"id": "uuid-2", "l2": "fr", "originalL1": "en", "cefrLevel": "B1"},
        ]
        result = await get_filtered_course_ids(
            ["uuid-1", "uuid-2"],
            "https://cms.test",
            "api-key-123",
            target_language="fr",
        )
        self.assertEqual(len(result), 1)
        self.assertIn("uuid-2", result)

        call_args = mock_cms_get.call_args
        params = call_args[1].get("query_params") or call_args[0][2]
        self.assertNotIn("where[l2][equals]", params)
        self.assertNotIn("where[originalL1][equals]", params)
        self.assertNotIn("where[cefrLevel][equals]", params)

    @patch("synapse_pangea_chat.public_courses.course_metadata_cache._cms_get")
    async def test_raises_on_cms_error(self, mock_cms_get: AsyncMock) -> None:
        mock_cms_get.side_effect = RuntimeError("CMS down")
        with self.assertRaises(FilteredCourseMetadataLookupError):
            await get_filtered_course_ids(
                ["uuid-1"],
                "https://cms.test",
                "api-key-123",
                target_language="es",
            )

    @patch("synapse_pangea_chat.public_courses.course_metadata_cache._cms_get")
    async def test_returns_empty_for_no_input(self, mock_cms_get: AsyncMock) -> None:
        result = await get_filtered_course_ids(
            [],
            "https://cms.test",
            "api-key-123",
            target_language="es",
        )
        self.assertEqual(result, {})
        mock_cms_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
