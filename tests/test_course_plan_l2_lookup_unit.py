"""Unit tests for the backfill's CMS language lookup.

These pin the two things the rest of the backfill's tests cannot see, because
they stub ``fetch_plan_languages`` wholesale: **which collection** is asked,
and **where in the document** the language is read from.

That blind spot is not hypothetical. The lookup originally asked only the
retired v1 ``course-plans`` collection and read a flat ``l2``. Course rooms
reference v3 ``quest-plans``, where the language lives at
``req.target_language`` — so the backfill resolved nothing, skipped every room,
and logged a clean-looking summary. Every existing test passed, because they
all mocked the response shape rather than the request.
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List, Sequence
from unittest.mock import patch

from synapse_pangea_chat.public_courses import course_plan_l2_lookup
from synapse_pangea_chat.public_courses.course_plan_l2_lookup import (
    CoursePlanLookupError,
    fetch_plan_languages,
)

QUEST_ID = "d68b36ca-4288-4312-9987-4ecb41cf8ff4"
LEGACY_ID = "legacy-v1-course-plan"


def _quest_doc(plan_id: str, language: Any) -> Dict[str, Any]:
    """A v3 quest-plans row: language nested under the generation request."""
    return {"id": plan_id, "req": {"target_language": language}}


class FakeCms:
    """Records which collection was asked for which ids, and answers canned."""

    def __init__(self, by_collection: Dict[str, List[Dict[str, Any]]]) -> None:
        self._by_collection = by_collection
        self.calls: List[tuple] = []

    async def __call__(
        self,
        collection: str,
        plan_ids: Sequence[str],
        cms_base_url: str,
        cms_api_key: str,
    ) -> List[Dict[str, Any]]:
        self.calls.append((collection, tuple(plan_ids)))
        wanted = set(plan_ids)
        return [
            doc
            for doc in self._by_collection.get(collection, [])
            if doc.get("id") in wanted
        ]

    @property
    def collections_asked(self) -> List[str]:
        return [collection for collection, _ in self.calls]


async def _fetch(fake: FakeCms, ids: Sequence[str]) -> Dict[str, str]:
    with patch.object(course_plan_l2_lookup, "_cms_get_plans", fake):
        return await fetch_plan_languages(list(ids), "http://cms", "key")


class CoursePlanL2LookupTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_asks_quest_plans(self) -> None:
        """v3 is the live content model; asking v1 resolves nothing."""
        fake = FakeCms({"quest-plans": [_quest_doc(QUEST_ID, "es")]})
        result = await _fetch(fake, [QUEST_ID])

        self.assertEqual(result, {QUEST_ID: "es"})
        self.assertEqual(fake.collections_asked[0], "quest-plans")

    async def test_reads_language_from_req_target_language(self) -> None:
        """A flat ``l2`` read would miss the v3 field entirely."""
        fake = FakeCms({"quest-plans": [_quest_doc(QUEST_ID, "de")]})
        self.assertEqual(await _fetch(fake, [QUEST_ID]), {QUEST_ID: "de"})

    async def test_legacy_v1_id_is_absent_and_no_other_collection_is_asked(
        self,
    ) -> None:
        """The retired v1 ``course-plans`` collection is never asked; a room
        still pinning a v1 id is skipped like any other unknown id."""
        fake = FakeCms({"quest-plans": [_quest_doc(QUEST_ID, "es")]})
        result = await _fetch(fake, [QUEST_ID, LEGACY_ID])

        self.assertEqual(result, {QUEST_ID: "es"})
        self.assertEqual(fake.calls, [("quest-plans", (QUEST_ID, LEGACY_ID))])

    async def test_unresolvable_id_is_absent_never_guessed(self) -> None:
        """Absent from the map means 'skip this room', never a default."""
        fake = FakeCms({"quest-plans": []})
        self.assertEqual(await _fetch(fake, ["no-such-plan"]), {})

    async def test_blank_or_non_string_language_does_not_resolve(self) -> None:
        fake = FakeCms(
            {
                "quest-plans": [
                    _quest_doc("blank", "   "),
                    _quest_doc("missing", None),
                    _quest_doc("numeric", 42),
                ],
            }
        )
        self.assertEqual(await _fetch(fake, ["blank", "missing", "numeric"]), {})

    async def test_language_is_trimmed(self) -> None:
        fake = FakeCms({"quest-plans": [_quest_doc(QUEST_ID, "  es-MX  ")]})
        self.assertEqual(await _fetch(fake, [QUEST_ID]), {QUEST_ID: "es-MX"})

    async def test_malformed_document_is_skipped_not_fatal(self) -> None:
        fake = FakeCms({"quest-plans": [_quest_doc(QUEST_ID, "es")]})
        fake._by_collection["quest-plans"].append({"id": "shaped-wrong"})
        self.assertEqual(
            await _fetch(fake, [QUEST_ID, "shaped-wrong"]), {QUEST_ID: "es"}
        )

    async def test_no_ids_asks_the_cms_nothing(self) -> None:
        fake = FakeCms({})
        self.assertEqual(await _fetch(fake, []), {})
        self.assertEqual(fake.calls, [])

    async def test_duplicate_ids_are_asked_once(self) -> None:
        fake = FakeCms({"quest-plans": [_quest_doc(QUEST_ID, "es")]})
        await _fetch(fake, [QUEST_ID, QUEST_ID, QUEST_ID])
        self.assertEqual(fake.calls[0], ("quest-plans", (QUEST_ID,)))

    async def test_transport_failure_propagates(self) -> None:
        """A lookup that could not be made must not read as 'no languages' —
        the caller skips the whole batch rather than treating it as resolved."""

        async def boom(*_args: Any, **_kwargs: Any) -> List[Dict[str, Any]]:
            raise CoursePlanLookupError("CMS quest-plans lookup failed (503)")

        with patch.object(course_plan_l2_lookup, "_cms_get_plans", boom):
            with self.assertRaises(CoursePlanLookupError):
                await fetch_plan_languages([QUEST_ID], "http://cms", "key")


if __name__ == "__main__":
    unittest.main()
