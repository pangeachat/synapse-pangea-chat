"""Unit tests for the public course catalog query.

These exercise the eligibility rule, the language filter, and pagination
against a fake ``db_pool.execute`` that runs the module's SQL shape against an
in-memory catalog. The SQL text itself is covered by the e2e tests, which run a
real Postgres; here we assert the module's own decisions — which cursor it
sends, how it slices the page, what it hands back.
"""

import importlib
import time
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

from synapse_pangea_chat.config import PangeaChatConfig

# The package re-exports the *function* ``get_public_courses``, which shadows
# the submodule of the same name; import the module explicitly.
mod = importlib.import_module("synapse_pangea_chat.public_courses.get_public_courses")

EVENT_TYPE = "pangea.course_plan"


class FakeCatalog:
    """Stands in for the DB: holds eligible rooms, answers the module's queries.

    Rooms are given as ``(room_id, plan_id, l2)``. A room whose ``plan_id`` is
    ``None`` is one whose course-plan state was removed or never carried an id —
    the SQL would not return it, so neither does this.

    Rooms named in *unpublished* are absent from the public room directory.
    They are withheld only when the SQL actually carries the ``is_public``
    predicate, so dropping that clause from the query fails the test rather
    than passing it: eligibility is a conjunction, and a fake that models only
    one conjunct cannot guard the other.
    """

    def __init__(
        self,
        rooms: List[Tuple[str, Optional[str], Optional[str]]],
        unpublished: Optional[Set[str]] = None,
    ) -> None:
        self.rooms = sorted(rooms, key=lambda r: r[0])
        self.unpublished = set(unpublished or ())
        self.calls: List[Tuple[str, Tuple[Any, ...]]] = []

    def _eligible(self, base_language: Optional[str], published_only: bool = True):
        rows = [r for r in self.rooms if r[1]]
        if published_only:
            rows = [r for r in rows if r[0] not in self.unpublished]
        if base_language:
            rows = [
                r
                for r in rows
                if r[2] and r[2].split("-")[0].lower() == base_language.lower()
            ]
        return rows

    async def execute(self, name: str, sql: str, *args: Any):
        self.calls.append((name, args))
        published_only = "r.is_public = TRUE" in sql
        if name == "get_public_courses_catalog_count":
            base = args[1] if len(args) > 1 else None
            return [(len(self._eligible(base, published_only)),)]
        if name == "get_public_courses_catalog_page":
            # args: event_type, [after_room_id], [base_language], offset, limit
            rest = list(args[1:])
            offset, limit = rest[-2], rest[-1]
            middle = rest[:-2]
            after_room_id = None
            base_language = None
            if "cse.room_id > ?" in sql:
                after_room_id = middle.pop(0)
            if "split_part" in sql:
                base_language = middle.pop(0)
            rows = self._eligible(base_language, published_only)
            if after_room_id is not None:
                rows = [r for r in rows if r[0] > after_room_id]
            return [tuple(r) for r in rows[offset : offset + limit]]
        if name == "get_public_courses_state_events":
            return []
        if name == "get_public_courses_room_stats":
            return []
        raise AssertionError(f"Unexpected query name: {name}")


def make_room_store(catalog: FakeCatalog) -> Any:
    return SimpleNamespace(db_pool=SimpleNamespace(execute=catalog.execute))


class TestBaseLanguage(unittest.TestCase):
    def test_base_language_strips_region(self) -> None:
        self.assertEqual(mod.base_language("es-ES"), "es")
        self.assertEqual(mod.base_language("ES-mx"), "es")
        self.assertEqual(mod.base_language("es"), "es")

    def test_base_language_of_empty_is_none(self) -> None:
        self.assertIsNone(mod.base_language(None))
        self.assertIsNone(mod.base_language(""))
        self.assertIsNone(mod.base_language("  "))


class TestParseSince(unittest.TestCase):
    def test_absent_since_starts_at_the_beginning(self) -> None:
        self.assertEqual(mod.parse_since(None), mod.Cursor(None, 0))
        self.assertEqual(mod.parse_since(""), mod.Cursor(None, 0))

    def test_room_id_since_is_a_keyset_cursor(self) -> None:
        self.assertEqual(mod.parse_since("!roomB:test"), mod.Cursor("!roomB:test", 0))

    def test_legacy_integer_since_is_accepted_as_an_offset(self) -> None:
        self.assertEqual(mod.parse_since("20"), mod.Cursor(None, 20))

    def test_malformed_since_is_rejected_not_treated_as_a_cursor(self) -> None:
        """A bad cursor must not present as an exhausted catalog.

        Room ids sort below nearly every printable character, so binding one of
        these into ``room_id > ?`` returns zero rows — a 200 with an empty
        chunk and a null next_batch, which reads as "there are no courses".
        """
        for bad in ("abc", "-5", "3.5", "1e3", " garbage "):
            with self.subTest(since=bad):
                with self.assertRaises(mod.InvalidCatalogParamError):
                    mod.parse_since(bad)


class TestGetPublicCourses(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        mod.reset_caches()

    async def _get(
        self,
        catalog: FakeCatalog,
        limit: int = 10,
        since: Optional[str] = None,
        filters: Optional[Dict[str, str]] = None,
    ):
        return await mod.get_public_courses(
            room_store=make_room_store(catalog),
            config=PangeaChatConfig(),
            limit=limit,
            since=since,
            filters=filters,
        )

    async def test_room_whose_plan_state_was_removed_does_not_appear(self) -> None:
        catalog = FakeCatalog(
            [
                ("!kept:test", "plan-1", "es"),
                ("!removed:test", None, None),
            ]
        )
        result = await self._get(catalog)
        self.assertEqual([c["room_id"] for c in result["chunk"]], ["!kept:test"])
        self.assertEqual(result["total_room_count_estimate"], 1)

    async def test_zero_mission_quest_appears(self) -> None:
        """Synapse never sees quest content; a plan id is the whole rule."""
        catalog = FakeCatalog([("!empty:test", "plan-with-no-missions", "de")])
        result = await self._get(catalog)
        self.assertEqual([c["room_id"] for c in result["chunk"]], ["!empty:test"])
        self.assertEqual(result["chunk"][0]["course_id"], "plan-with-no-missions")

    async def test_room_without_l2_appears_unfiltered_and_is_excluded_filtered(
        self,
    ) -> None:
        catalog = FakeCatalog(
            [
                ("!nolang:test", "plan-1", None),
                ("!spanish:test", "plan-2", "es"),
            ]
        )

        unfiltered = await self._get(catalog)
        self.assertEqual(
            sorted(c["room_id"] for c in unfiltered["chunk"]),
            ["!nolang:test", "!spanish:test"],
        )
        self.assertIsNone(unfiltered["chunk"][0]["target_language"])

        mod.reset_caches()
        filtered = await self._get(catalog, filters={"target_language": "es"})
        self.assertEqual([c["room_id"] for c in filtered["chunk"]], ["!spanish:test"])
        self.assertEqual(filtered["total_room_count_estimate"], 1)

    async def test_target_language_matches_on_base_language(self) -> None:
        catalog = FakeCatalog(
            [
                ("!a:test", "plan-a", "es"),
                ("!b:test", "plan-b", "es-ES"),
                ("!c:test", "plan-c", "es-MX"),
                ("!d:test", "plan-d", "fr"),
            ]
        )
        filtered = await self._get(catalog, filters={"target_language": "es"})
        self.assertEqual(
            [c["room_id"] for c in filtered["chunk"]],
            ["!a:test", "!b:test", "!c:test"],
        )

        mod.reset_caches()
        regional = await self._get(catalog, filters={"target_language": "es-419"})
        self.assertEqual(len(regional["chunk"]), 3)

    async def test_pages_are_full_while_the_catalog_has_more(self) -> None:
        """Filtering happens before paging, so ineligible rooms never thin a page."""
        rooms: List[Tuple[str, Optional[str], Optional[str]]] = []
        for i in range(10):
            rooms.append((f"!es{i:02d}:test", f"plan-es-{i}", "es"))
            # Interleave rooms the eligibility rule excludes.
            rooms.append((f"!fr{i:02d}:test", f"plan-fr-{i}", "fr"))
            rooms.append((f"!gone{i:02d}:test", None, None))

        catalog = FakeCatalog(rooms)

        seen: List[str] = []
        since: Optional[str] = None
        for _ in range(5):
            page = await self._get(
                catalog, limit=4, since=since, filters={"target_language": "es"}
            )
            seen.extend(c["room_id"] for c in page["chunk"])
            if page["next_batch"] is None:
                break
            # A non-null next_batch promises more results.
            self.assertEqual(len(page["chunk"]), 4)
            since = page["next_batch"]

        self.assertEqual(seen, [f"!es{i:02d}:test" for i in range(10)])
        self.assertEqual(len(seen), 10)

    async def test_next_batch_is_null_on_the_last_page(self) -> None:
        catalog = FakeCatalog(
            [("!a:test", "plan-a", "es"), ("!b:test", "plan-b", "es")]
        )
        page = await self._get(catalog, limit=2)
        self.assertEqual(len(page["chunk"]), 2)
        self.assertIsNone(page["next_batch"])

    async def test_next_batch_is_the_last_room_id_of_the_page(self) -> None:
        catalog = FakeCatalog(
            [("!a:test", "plan-a", "es"), ("!b:test", "plan-b", "es")]
        )
        page = await self._get(catalog, limit=1)
        self.assertEqual(page["next_batch"], "!a:test")

    async def test_legacy_integer_since_still_pages(self) -> None:
        catalog = FakeCatalog(
            [
                ("!a:test", "plan-a", "es"),
                ("!b:test", "plan-b", "es"),
                ("!c:test", "plan-c", "es"),
            ]
        )
        page = await self._get(catalog, limit=1, since="1")
        self.assertEqual([c["room_id"] for c in page["chunk"]], ["!b:test"])
        # The cursor it hands back is a keyset cursor, not another offset.
        self.assertEqual(page["next_batch"], "!b:test")

    async def test_non_positive_limit_is_normalized(self) -> None:
        catalog = FakeCatalog(
            [(f"!r{i:02d}:test", f"plan-{i}", "es") for i in range(12)]
        )
        page = await self._get(catalog, limit=0)
        self.assertEqual(len(page["chunk"]), mod.DEFAULT_LIMIT)

    async def test_empty_catalog_returns_no_cursor(self) -> None:
        catalog = FakeCatalog([])
        page = await self._get(catalog)
        self.assertEqual(page["chunk"], [])
        self.assertIsNone(page["next_batch"])
        self.assertEqual(page["total_room_count_estimate"], 0)

    async def test_catalog_count_is_cached_across_requests(self) -> None:
        catalog = FakeCatalog([("!a:test", "plan-a", "es")])
        await self._get(catalog)
        await self._get(catalog)
        counts = [
            c for c in catalog.calls if c[0] == "get_public_courses_catalog_count"
        ]
        self.assertEqual(len(counts), 1)

    async def test_unpublished_room_with_a_plan_is_not_a_course(self) -> None:
        """Eligibility is a conjunction: published AND carrying a plan id."""
        catalog = FakeCatalog(
            [
                ("!listed:test", "plan-listed", "es"),
                ("!hidden:test", "plan-hidden", "es"),
            ],
            unpublished={"!hidden:test"},
        )
        result = await self._get(catalog)
        self.assertEqual([c["room_id"] for c in result["chunk"]], ["!listed:test"])
        self.assertEqual(result["total_room_count_estimate"], 1)

    async def test_unhonorable_target_language_is_rejected_not_dropped(self) -> None:
        """Degrading to no filter would serve the whole unfiltered catalog."""
        catalog = FakeCatalog(
            [("!es:test", "plan-es", "es"), ("!fr:test", "plan-fr", "fr")]
        )
        with self.assertRaises(mod.InvalidCatalogParamError):
            await self._get(catalog, filters={"target_language": "-"})

    async def test_count_cache_is_bounded_and_expires(self) -> None:
        """The language half of the key is caller-supplied, so it must be capped."""
        catalog = FakeCatalog([("!a:test", "plan-a", "es")])

        # Cycling the filter the way a caller could must not grow the dict
        # without bound.
        for first in "abcdefghijklmnopqrst":
            for second in "abcdefghijklmnopqrst":
                await self._get(catalog, filters={"target_language": first + second})
        self.assertLessEqual(len(mod._count_cache), mod._COUNT_CACHE_MAX_ENTRIES)

        # And entries past their TTL are pruned, not merely overwritten.
        stale = time.time() - (mod._COUNT_CACHE_TTL_SECONDS + 1)
        mod._count_cache[(EVENT_TYPE, "zz")] = (7, stale)
        mod._cleanup_expired_cache()
        self.assertNotIn((EVENT_TYPE, "zz"), mod._count_cache)


if __name__ == "__main__":
    unittest.main()
