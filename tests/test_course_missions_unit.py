"""Unit tests for the course space's Can-Do list a server-created course starts
with.

The list is a snapshot of the shared course list: the quest's learning
objectives in order, each with the visible library activities at the course's
target language that reference it (create-course-space.instructions.md). What
the snapshot cannot carry over is recovered and captured; a list that cannot be
built raises with a code the caller handles.
"""

import json
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qsl, urlparse

from twisted.internet import defer

from synapse_pangea_chat.email_invite import course_missions as module
from synapse_pangea_chat.email_invite.course_missions import (
    CMS_REFUSED,
    CMS_UNAVAILABLE,
    COURSE_MISSIONS_STATE_EVENT_TYPE,
    NO_QUEST,
    NOTE_TOO_LONG,
    QUEST_MALFORMED,
    QUEST_NOT_FOUND,
    SELECTION_ALL_GONE,
    SELECTION_PARTLY_GONE,
    SELECTION_UNSET,
    TIMEOUT,
    CourseMissionsError,
    CourseMissionsReader,
    Problem,
    build_course_missions,
    build_course_missions_content,
    rank_activities,
    shorten_note,
    utf16_length,
)


def _quest(lo_ids: List[Any], l2: Optional[str] = "es") -> Dict[str, Any]:
    return {
        "id": "quest-1",
        "req": {"target_language": l2},
        "res": {
            "learning_objective_sequence": [
                {"learning_objective": lo, "was_minted": False} for lo in lo_ids
            ]
        },
    }


def _activity(
    activity_id: str,
    refs: List[Any],
    *,
    l2: str = "es",
    row_id: Optional[str] = None,
    **top: Any,
) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "id": row_id or f"row-{activity_id}",
        "req": {},
        "res": {"plan": {"activity_id": activity_id, "l2": l2}},
        "learningObjectiveRefs": refs,
        "hidden": False,
        "visibility": "public",
    }
    doc.update(top)
    return doc


class TestBuildCourseMissionsContent(unittest.TestCase):
    def test_shape_matches_the_contract(self) -> None:
        """version 1, revision 1, missions of {lo_id, activity_ids}, and no
        time_zone: the Studio sets the zone on first publish."""
        content = build_course_missions_content(
            _quest(["lo-a"]), [_activity("act-1", ["lo-a"])]
        )

        self.assertEqual(
            content,
            {
                "version": 1,
                "revision": 1,
                "missions": [{"lo_id": "lo-a", "activity_ids": ["act-1"]}],
            },
        )
        self.assertNotIn("time_zone", content)
        self.assertEqual(COURSE_MISSIONS_STATE_EVENT_TYPE, "pangea.course_missions")

    def test_missions_follow_the_quest_order_not_the_activity_order(self) -> None:
        content = build_course_missions_content(
            _quest(["lo-c", "lo-a", "lo-b"]),
            [
                _activity("act-a", ["lo-a"]),
                _activity("act-b", ["lo-b"]),
                _activity("act-c", ["lo-c"]),
            ],
        )

        self.assertEqual(
            [m["lo_id"] for m in content["missions"]], ["lo-c", "lo-a", "lo-b"]
        )
        self.assertEqual(
            [m["activity_ids"] for m in content["missions"]],
            [["act-c"], ["act-a"], ["act-b"]],
        )

    def test_activities_keep_the_listing_order_within_a_mission(self) -> None:
        content = build_course_missions_content(
            _quest(["lo-a"]),
            [_activity("act-2", ["lo-a"]), _activity("act-1", ["lo-a"])],
        )

        self.assertEqual(content["missions"][0]["activity_ids"], ["act-2", "act-1"])

    def test_an_activity_serving_two_objectives_is_in_both(self) -> None:
        content = build_course_missions_content(
            _quest(["lo-a", "lo-b"]), [_activity("act-1", ["lo-a", "lo-b"])]
        )

        self.assertEqual(
            [m["activity_ids"] for m in content["missions"]], [["act-1"], ["act-1"]]
        )

    def test_only_visible_canonical_rows_at_the_course_l2(self) -> None:
        content = build_course_missions_content(
            _quest(["lo-a"]),
            [
                _activity("keep", ["lo-a"]),
                _activity("hidden", ["lo-a"], hidden=True),
                _activity("private", ["lo-a"], visibility="private"),
                _activity("french", ["lo-a"], l2="fr"),
                _activity("translation", ["lo-a"], req={"source_request_hash": "h"}),
                _activity("other-lo", ["lo-z"]),
            ],
        )

        self.assertEqual(content["missions"][0]["activity_ids"], ["keep"])

    def test_activity_content_ids_never_row_ids(self) -> None:
        content = build_course_missions_content(
            _quest(["lo-a"]), [_activity("act-1", ["lo-a"], row_id="row-xyz")]
        )

        self.assertEqual(content["missions"][0]["activity_ids"], ["act-1"])

    def test_first_row_per_activity_wins(self) -> None:
        content = build_course_missions_content(
            _quest(["lo-a", "lo-b"]),
            [_activity("act-1", ["lo-a"]), _activity("act-1", ["lo-b"])],
        )

        self.assertEqual(
            [m["activity_ids"] for m in content["missions"]], [["act-1"], []]
        )

    def test_objective_without_activities_is_kept_empty(self) -> None:
        content = build_course_missions_content(_quest(["lo-a", "lo-b"]), [])

        self.assertEqual(
            content["missions"],
            [
                {"lo_id": "lo-a", "activity_ids": []},
                {"lo_id": "lo-b", "activity_ids": []},
            ],
        )

    def test_a_mission_listed_twice_keeps_its_first_place(self) -> None:
        # Readers reject a Can-Do list that names the same Can-Do twice.
        content = build_course_missions_content(
            _quest(["lo-a", "lo-b", "lo-a"]), [_activity("act-1", ["lo-a"])]
        )

        self.assertEqual(
            content["missions"],
            [
                {"lo_id": "lo-a", "activity_ids": ["act-1"]},
                {"lo_id": "lo-b", "activity_ids": []},
            ],
        )

    def test_populated_relationships_are_read_by_id(self) -> None:
        content = build_course_missions_content(
            _quest([{"id": "lo-a", "text": "x"}]),
            [_activity("act-1", [{"id": "lo-a"}])],
        )

        self.assertEqual(
            content["missions"], [{"lo_id": "lo-a", "activity_ids": ["act-1"]}]
        )

    def test_quest_without_target_language_cannot_be_copied_and_says_why(
        self,
    ) -> None:
        with self.assertRaises(CourseMissionsError) as raised:
            build_course_missions_content(_quest(["lo-a"], l2=None), [])
        self.assertEqual(raised.exception.code, QUEST_MALFORMED)


def _entry_quest(*entries: Dict[str, Any]) -> Dict[str, Any]:
    quest = _quest([])
    quest["res"]["learning_objective_sequence"] = list(entries)
    return quest


LISTED = [_activity(a, ["lo-a"]) for a in ("act-1", "act-2", "act-3")]


class TestPlanEntrySelectionAndNote(unittest.TestCase):
    """A new Can-Do list starts from the plan entry's selected activities and note
    (org quests.instructions.md, "Each entry in the Can-Do sequence")."""

    def _mission(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        content = build_course_missions_content(
            _entry_quest({"learning_objective": "lo-a", **entry}), LISTED
        )
        return content["missions"][0]

    def test_the_selection_is_copied_in_its_order(self) -> None:
        mission = self._mission({"activity_ids": ["act-3", "act-1"]})
        self.assertEqual(mission["activity_ids"], ["act-3", "act-1"])

    def _built(self, entry: Dict[str, Any], **kwargs: Any) -> Any:
        return build_course_missions(
            _entry_quest({"learning_objective": "lo-a", **entry}), LISTED, **kwargs
        )

    def test_a_selection_fully_listed_has_no_problem(self) -> None:
        self.assertEqual(self._built({"activity_ids": ["act-3", "act-1"]}).problems, [])

    def test_part_of_a_selection_gone_keeps_the_rest_and_says_so(self) -> None:
        built = self._built(
            {"activity_ids": ["gone", "act-2", "hidden"]}, existing={"hidden"}
        )

        self.assertEqual(built.content["missions"][0]["activity_ids"], ["act-2"])
        self.assertEqual(
            built.problems,
            [
                Problem(
                    SELECTION_PARTLY_GONE,
                    "lo-a",
                    {"selected": 3, "kept": 1, "unavailable": 1, "not_found": 1},
                )
            ],
        )

    def test_no_selection_or_an_empty_one_pins_the_ranked_list(self) -> None:
        # A teacher's own quest selects nothing: each entry shows what its
        # quest entry would, the ranked ten, never every listed activity.
        rows = [
            _activity("low", ["lo-a"], score=2),
            _activity("good", ["lo-a"], score=7),
            *[_activity(f"more-{i}", ["lo-a"], score=5) for i in range(12)],
        ]
        selections: Tuple[Optional[List[str]], ...] = (None, [])
        for selected in selections:
            built = build_course_missions(
                _entry_quest({"learning_objective": "lo-a", "activity_ids": selected}),
                rows,
            )
            ids = built.content["missions"][0]["activity_ids"]
            self.assertEqual(len(ids), 10)
            self.assertEqual(ids[0], "good")
            self.assertNotIn("low", ids)
            self.assertEqual(
                built.problems, [Problem(SELECTION_UNSET, "lo-a", {"ranked": 10})]
            )

    def test_a_selection_with_nothing_listed_pins_the_ranked_list(self) -> None:
        # Not every listed activity: on production one Can-Do lists 123, most
        # scored 2 by a reviewer. The quests doc's short ranked list instead.
        built = build_course_missions(
            _entry_quest(
                {"learning_objective": "lo-a", "activity_ids": ["gone", "hidden"]}
            ),
            [
                _activity("act-1", ["lo-a"], score=4),
                _activity("act-2", ["lo-a"], score=1),
                _activity("act-3", ["lo-a"], score=8),
            ],
            existing={"hidden"},
        )

        self.assertEqual(
            built.content["missions"][0]["activity_ids"], ["act-3", "act-1"]
        )
        self.assertEqual(
            built.problems,
            [
                Problem(
                    SELECTION_ALL_GONE,
                    "lo-a",
                    {
                        "selected": 2,
                        "kept": 0,
                        "unavailable": 1,
                        "not_found": 1,
                        "ranked": 2,
                    },
                )
            ],
        )

    def test_a_selection_with_nothing_listed_and_no_activities_is_empty(
        self,
    ) -> None:
        built = build_course_missions(
            _entry_quest({"learning_objective": "lo-a", "activity_ids": ["gone"]}),
            [_activity("other", ["lo-b"])],
        )

        self.assertEqual(built.content["missions"][0]["activity_ids"], [])
        self.assertEqual(
            built.problems[0].counts,
            {"selected": 1, "kept": 0, "unclassified": 1, "ranked": 0},
        )

    def test_the_note_is_copied(self) -> None:
        mission = self._mission({"note": "Textbook unit 3"})
        self.assertEqual(mission["note"], "Textbook unit 3")

    def test_no_note_or_a_blank_one_is_left_out(self) -> None:
        for note in (None, "", "   "):
            self.assertNotIn("note", self._mission({"note": note}))

    def test_a_note_readers_would_refuse_is_shortened_and_says_so(self) -> None:
        # Readers refuse a whole list with a longer note; the CMS limits it,
        # so a longer one means something wrote around the limit.
        note = "Practice ordering food " * 15
        built = self._built({"note": note})

        shortened = built.content["missions"][0]["note"]
        self.assertLessEqual(utf16_length(shortened), 280)
        self.assertTrue(shortened.endswith("\u2026"))
        self.assertTrue(note.startswith(shortened[:-1]))
        self.assertNotIn(" \u2026", shortened)
        # At a word boundary: the cut ends a whole word of the note.
        self.assertIn(shortened[:-1].split()[-1], ("Practice", "ordering", "food"))
        self.assertIn(
            Problem(NOTE_TOO_LONG, "lo-a", {"length": len(note)}), built.problems
        )
        self.assertEqual(self._mission({"note": "x" * 280})["note"], "x" * 280)

    def test_shortening_counts_utf16_units_and_keeps_whole_characters(
        self,
    ) -> None:
        # 141 emoji are 141 characters but 282 UTF-16 units, the length the
        # readers count. With no word boundary the cut is mid-run.
        shortened = shorten_note("\U0001f600" * 141)

        self.assertEqual(shortened, "\U0001f600" * 139 + "\u2026")
        self.assertEqual(utf16_length(shortened), 279)

    def test_a_mission_listed_twice_keeps_its_first_entry(self) -> None:
        content = build_course_missions_content(
            _entry_quest(
                {"learning_objective": "lo-a", "activity_ids": ["act-2"], "note": "1"},
                {"learning_objective": "lo-a", "activity_ids": ["act-3"], "note": "2"},
            ),
            LISTED,
        )
        self.assertEqual(
            content["missions"],
            [{"lo_id": "lo-a", "activity_ids": ["act-2"], "note": "1"}],
        )


class TestRankActivities(unittest.TestCase):
    """The short ranked list an entry with nothing to show pins (org quests
    doc: at most about ten, best-scored first)."""

    def test_best_scored_first_then_unaudited_at_most_ten(self) -> None:
        rows = [
            _activity("seven", ["lo-a"], score=7),
            _activity("unaudited", ["lo-a"]),
            _activity("nine", ["lo-a"], score=9),
            _activity("two", ["lo-a"], score=2),
            *[_activity(f"filler-{i}", ["lo-a"], score=5) for i in range(12)],
        ]

        ranked = rank_activities(rows, "es", "lo-a")

        self.assertEqual(len(ranked), 10)
        self.assertEqual(ranked[:2], ["nine", "seven"])
        self.assertNotIn("two", ranked)

    def test_equal_scores_put_the_quest_s_level_first(self) -> None:
        rows = [
            _activity("b1", ["lo-a"], score=6),
            _activity("a2", ["lo-a"], score=6),
            _activity("a2-best", ["lo-a"], score=9),
            _activity("unaudited-b1", ["lo-a"]),
            _activity("unaudited-a2", ["lo-a"]),
        ]
        for doc, level in zip(rows, ("B1", "A2", "A2", "B1", "A2")):
            doc["res"]["plan"]["cefr_level"] = level

        self.assertEqual(
            rank_activities(rows, "es", "lo-a", "A2"),
            ["a2-best", "a2", "b1", "unaudited-a2", "unaudited-b1"],
        )

    def test_unaudited_come_after_every_scored_one(self) -> None:
        rows = [_activity("unaudited", ["lo-a"]), _activity("six", ["lo-a"], score=6)]
        self.assertEqual(rank_activities(rows, "es", "lo-a"), ["six", "unaudited"])

    def test_low_scores_only_when_nothing_else_exists(self) -> None:
        rows = [
            _activity("one", ["lo-a"], score=1),
            _activity("two", ["lo-a"], score=2),
        ]
        self.assertEqual(rank_activities(rows, "es", "lo-a"), ["two", "one"])

    def test_rows_readers_would_not_list_stay_out(self) -> None:
        rows = [
            _activity("hidden", ["lo-a"], score=9, hidden=True),
            _activity("french", ["lo-a"], score=9, l2="fr"),
            _activity("other-lo", ["lo-b"], score=9),
            _activity("kept", ["lo-a"], score=3),
        ]
        self.assertEqual(rank_activities(rows, "es", "lo-a"), ["kept"])


class _Response:
    def __init__(self, code: int, body: Any) -> None:
        self.code = code
        self.body = json.dumps(body).encode()


def _reader(responses: Dict[str, _Response], calls: List[str]) -> CourseMissionsReader:
    from synapse.util.clock import Clock
    from twisted.internet import reactor

    api = MagicMock()
    api._hs.get_clock.return_value = Clock(reactor, server_name="test")  # type: ignore[arg-type]

    async def request(method: str, uri: str, headers: Any = None) -> _Response:
        calls.append(uri)
        assert method == "GET"
        assert headers.getRawHeaders("Authorization") == ["service-users API-Key key"]
        path = urlparse(uri).path
        for prefix, response in responses.items():
            if path.startswith(prefix):
                return response
        raise ConnectionRefusedError(uri)

    api.http_client.request = request
    return CourseMissionsReader(api, "http://cms.test/", "key")


def _read_body(response: _Response, buffer: Any, max_size: int) -> Any:
    buffer.write(response.body)
    return defer.succeed(len(response.body))


class TestCourseMissionsReader(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from unittest.mock import patch

        patcher = patch(
            "synapse_pangea_chat.cms_http.read_body_with_max_size",
            _read_body,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_builds_the_can_do_list_from_the_quest_and_its_activities(
        self,
    ) -> None:
        calls: List[str] = []
        reader = _reader(
            {
                "/api/quest-plans/quest-1": _Response(200, _quest(["lo-b", "lo-a"])),
                "/api/activities-v2": _Response(
                    200,
                    {
                        "docs": [
                            _activity("act-a", ["lo-a"]),
                            _activity("act-b", ["lo-b"]),
                            _activity("act-hidden", ["lo-a"], hidden=True),
                        ]
                    },
                ),
            },
            calls,
        )

        content = (await reader.read("quest-1")).content

        self.assertEqual(
            content,
            {
                "version": 1,
                "revision": 1,
                "missions": [
                    {"lo_id": "lo-b", "activity_ids": ["act-b"]},
                    {"lo_id": "lo-a", "activity_ids": ["act-a"]},
                ],
            },
        )
        query = dict(parse_qsl(urlparse(calls[1]).query))
        self.assertEqual(
            query["where[and][0][req.source_request_hash][exists]"], "false"
        )
        self.assertEqual(query["where[and][1][hidden][not_equals]"], "true")
        self.assertEqual(query["where[and][2][visibility][not_equals]"], "private")
        self.assertEqual(query["where[and][3][res.plan.l2][equals]"], "es")
        self.assertEqual(
            query["where[and][4][or][0][learningObjectiveRefs][contains]"], "lo-b"
        )
        self.assertEqual(
            query["where[and][4][or][1][learningObjectiveRefs][contains]"], "lo-a"
        )
        self.assertEqual(query["sort"], "-updatedAt")
        self.assertEqual(query["depth"], "0")

    async def _code(
        self, reader: CourseMissionsReader, quest_id: str = "quest-1"
    ) -> str:
        with self.assertRaises(CourseMissionsError) as raised:
            await reader.read(quest_id)
        return raised.exception.code

    async def test_a_cms_that_keeps_failing_says_it_is_unavailable(self) -> None:
        calls: List[str] = []
        reader = _reader({"/api/quest-plans/": _Response(500, {"errors": []})}, calls)

        with self.assertLogs(module.logger, "WARNING") as logs:
            self.assertEqual(await self._code(reader), CMS_UNAVAILABLE)

        # Tried once more inside the budget, then raised for the caller.
        self.assertEqual(len(calls), 2)
        self.assertIn("trying once more", logs.output[0])

    async def test_a_cms_failure_that_passes_is_retried_once(self) -> None:
        calls: List[str] = []
        answers = [_Response(503, {}), _Response(200, _quest(["lo-a"]))]
        reader = _reader({}, calls)

        async def request(method: str, uri: str, headers: Any = None) -> _Response:
            calls.append(uri)
            if "quest-plans" in uri:
                return answers.pop(0)
            return _Response(200, {"docs": [_activity("act-a", ["lo-a"])]})

        reader._http.request = request  # type: ignore[method-assign, assignment]

        with self.assertLogs(module.logger, "WARNING"):
            built = await reader.read("quest-1")

        self.assertEqual(built.content["missions"][0]["activity_ids"], ["act-a"])

    async def test_unreachable_cms_says_it_is_unavailable(self) -> None:
        with self.assertLogs(module.logger, "WARNING"):
            self.assertEqual(await self._code(_reader({}, [])), CMS_UNAVAILABLE)

    async def test_a_quest_the_cms_does_not_have_is_not_retried(self) -> None:
        calls: List[str] = []
        reader = _reader({"/api/quest-plans/": _Response(404, {})}, calls)

        self.assertEqual(await self._code(reader), QUEST_NOT_FOUND)
        self.assertEqual(len(calls), 1)

    async def test_a_refused_key_is_not_retried(self) -> None:
        calls: List[str] = []
        reader = _reader({"/api/quest-plans/": _Response(403, {})}, calls)

        self.assertEqual(await self._code(reader), CMS_REFUSED)
        self.assertEqual(len(calls), 1)

    async def test_a_quest_without_a_language_is_malformed(self) -> None:
        reader = _reader(
            {"/api/quest-plans/": _Response(200, _quest(["lo-a"], l2=None))}, []
        )
        self.assertEqual(await self._code(reader), QUEST_MALFORMED)

    async def test_failed_activity_read_builds_no_can_do_list(self) -> None:
        """A partial read is not a Can-Do list: an empty course would replace the
        shared list for every member."""
        reader = _reader(
            {
                "/api/quest-plans/": _Response(200, _quest(["lo-a"])),
                "/api/activities-v2": _Response(503, {}),
            },
            [],
        )

        with self.assertLogs(module.logger, "WARNING"):
            self.assertEqual(await self._code(reader), CMS_UNAVAILABLE)

    async def test_a_cms_slower_than_the_budget_times_out(self) -> None:
        # Synapse's timeout_deferred raises twisted's TimeoutError when the
        # budget runs out; the whole budget is spent, so no second attempt.
        def timed_out(*, deferred: Any, timeout: float, clock: Any) -> Any:
            deferred.addBoth(lambda _: None)
            return defer.fail(defer.TimeoutError())

        calls: List[str] = []
        reader = _reader(
            {
                "/api/quest-plans/": _Response(200, _quest(["lo-a"])),
                "/api/activities-v2": _Response(200, {"docs": []}),
            },
            calls,
        )
        with patch.object(module, "timeout_deferred", timed_out):
            self.assertEqual(await self._code(reader), TIMEOUT)

    async def test_no_quest_id_reads_nothing(self) -> None:
        calls: List[str] = []
        self.assertEqual(await self._code(_reader({}, calls), ""), NO_QUEST)
        self.assertEqual(calls, [])


def _store_reader(
    quest: Dict[str, Any],
    window: List[Dict[str, Any]],
    library: List[Dict[str, Any]],
    calls: List[Dict[str, str]],
    by_id_code: int = 200,
    window_total: Optional[int] = None,
) -> CourseMissionsReader:
    """A CMS whose newest-rows read answers ``window`` and whose read by id
    answers the ``library`` rows the query names."""
    from synapse.util.clock import Clock
    from twisted.internet import reactor

    api = MagicMock()
    api._hs.get_clock.return_value = Clock(reactor, server_name="test")  # type: ignore[arg-type]

    async def request(method: str, uri: str, headers: Any = None) -> _Response:
        parsed = urlparse(uri)
        query = dict(parse_qsl(parsed.query))
        calls.append(query)
        if parsed.path.startswith("/api/quest-plans/"):
            return _Response(200, quest)
        if "where[res.plan.activity_id][in]" in query:
            # Every row of the ids, listed or not.
            wanted = set(query["where[res.plan.activity_id][in]"].split(","))
            docs = [d for d in library if d["res"]["plan"]["activity_id"] in wanted]
            return _Response(200, {"docs": docs, "totalDocs": len(docs)})
        lo_key = "where[and][4][learningObjectiveRefs][contains]"
        if lo_key in query:
            # One Can-Do's ranked tier, under the listing's clauses.
            docs = [
                d
                for d in library
                if query[lo_key] in d["learningObjectiveRefs"] and not d["hidden"]
            ]
            if "where[and][5][score][greater_than]" in query:
                docs = [d for d in docs if (d.get("score") or 0) > 2]
            elif "where[and][5][score][exists]" in query:
                docs = [d for d in docs if d.get("score") is None]
            else:
                docs = [d for d in docs if d.get("score") is not None]
            docs.sort(key=lambda d: -(d.get("score") or 0))
            return _Response(200, {"docs": docs[:10], "totalDocs": len(docs)})
        ids_key = next((k for k in query if "res.plan.activity_id" in k), None)
        if ids_key is None:
            total = len(window) if window_total is None else window_total
            return _Response(200, {"docs": window, "totalDocs": total})
        if by_id_code != 200:
            return _Response(by_id_code, {})
        wanted = set(query[ids_key].split(","))
        docs = [
            d
            for d in library
            if d["res"]["plan"]["activity_id"] in wanted and not d["hidden"]
        ]
        return _Response(200, {"docs": docs, "totalDocs": len(docs)})

    api.http_client.request = request
    return CourseMissionsReader(api, "http://cms.test/", "key")


def _selecting(entries: List[Tuple[str, Optional[List[str]]]]) -> Dict[str, Any]:
    quest = _quest([lo for lo, _ in entries])
    for step, (_, ids) in zip(quest["res"]["learning_objective_sequence"], entries):
        step["activity_ids"] = ids
    return quest


class TestSelectedActivitiesReadById(unittest.IsolatedAsyncioTestCase):
    """A merged Can-Do carries hundreds of activities, so the newest 200 rows
    can miss a quest's selection entirely (pangeachat/.github#832). Selected
    activities are read by id; the newest rows only fill an entry that
    selects nothing."""

    def setUp(self) -> None:
        from unittest.mock import patch

        patcher = patch(
            "synapse_pangea_chat.cms_http.read_body_with_max_size", _read_body
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_selection_older_than_the_newest_rows_is_kept(self) -> None:
        window = [_activity(f"new-{i}", ["lo-a"]) for i in range(200)]
        library = [_activity("old-2", ["lo-a"]), _activity("old-1", ["lo-a"])]
        quest = _selecting([("lo-a", ["old-1", "old-2"])])
        calls: List[Dict[str, str]] = []

        content = (
            await _store_reader(quest, window, library, calls).read("quest-1")
        ).content
        self.assertEqual(content["missions"][0]["activity_ids"], ["old-1", "old-2"])

    async def test_part_in_the_window_and_part_read_by_id_keeps_the_order(
        self,
    ) -> None:
        window = [_activity("sel-2", ["lo-a"]), _activity("other", ["lo-a"])]
        library = [_activity("sel-1", ["lo-a"]), _activity("sel-3", ["lo-a"])]
        quest = _selecting([("lo-a", ["sel-1", "sel-2", "sel-3"])])
        calls: List[Dict[str, str]] = []

        content = (
            await _store_reader(quest, window, library, calls).read("quest-1")
        ).content
        self.assertEqual(
            content["missions"][0]["activity_ids"], ["sel-1", "sel-2", "sel-3"]
        )
        # Only what the window missed is read by id, with the listing's clauses.
        [by_id] = [q for q in calls if any("activity_id" in k for k in q)]
        ids_key = next(k for k in by_id if "res.plan.activity_id" in k)
        self.assertEqual(by_id[ids_key].split(","), ["sel-1", "sel-3"])
        self.assertEqual(by_id["where[and][1][hidden][not_equals]"], "true")
        self.assertEqual(by_id["where[and][2][visibility][not_equals]"], "private")
        self.assertEqual(by_id["where[and][3][res.plan.l2][equals]"], "es")

    async def test_hidden_missing_or_other_can_do_activities_stay_out(self) -> None:
        window = [_activity("new-1", ["lo-a"])]
        library = [
            _activity("kept", ["lo-a"]),
            _activity("hidden", ["lo-a"], hidden=True),
            _activity("elsewhere", ["lo-b"]),
            _activity("other-l2", ["lo-a"], l2="fr"),
        ]
        quest = _selecting(
            [("lo-a", ["kept", "hidden", "elsewhere", "other-l2", "missing"])]
        )

        with patch.object(module, "capture_message") as capture:
            content = (
                await _store_reader(quest, window, library, []).read("quest-1")
            ).content

        self.assertEqual(content["missions"][0]["activity_ids"], ["kept"])
        # One warning for the build, hidden told apart from never there.
        capture.assert_called_once_with(
            "Can-Do list: selection_partly_gone",
            level="warning",
            tags={"course_missions.problem": "selection_partly_gone"},
            context={
                "quest_id": "quest-1",
                "count": 1,
                "entries": [
                    {
                        "lo_id": "lo-a",
                        "selected": 5,
                        "kept": 1,
                        "unavailable": 3,
                        "not_found": 1,
                    }
                ],
            },
        )

    async def test_a_selection_naming_only_unavailable_activities_pins_the_ranked_ten(
        self,
    ) -> None:
        # org quests.instructions.md: an entry with no selected activity to
        # show shows a short ranked list of its Can-Do's activities.
        window = [_activity("new-1", ["lo-a"]), _activity("new-2", ["lo-a"])]
        library = [
            _activity("hidden", ["lo-a"], hidden=True),
            _activity("best", ["lo-a"], score=9),
            _activity("good", ["lo-a"], score=6),
            _activity("failed", ["lo-a"], score=2),
            _activity("fresh", ["lo-a"]),
        ]
        quest = _selecting([("lo-a", ["missing", "hidden"]), ("lo-b", None)])
        calls: List[Dict[str, str]] = []

        with patch.object(module, "capture_message") as capture:
            content = (
                await _store_reader(quest, window, library, calls).read("quest-1")
            ).content

        self.assertEqual(
            content["missions"][0]["activity_ids"],
            # The page's unaudited rows rank with the read's, in row order.
            ["best", "good", "new-1", "new-2", "fresh"],
        )
        self.assertEqual(content["missions"][1]["activity_ids"], [])
        by_message = {c.args[0]: c.kwargs for c in capture.call_args_list}
        self.assertEqual(
            sorted(by_message),
            ["Can-Do list: selection_all_gone", "Can-Do list: selection_unset"],
        )
        self.assertEqual(by_message["Can-Do list: selection_unset"]["level"], "warning")
        all_gone = by_message["Can-Do list: selection_all_gone"]
        self.assertEqual(all_gone["level"], "error")
        self.assertEqual(
            all_gone["context"]["entries"],
            [
                {
                    "lo_id": "lo-a",
                    "selected": 2,
                    "kept": 0,
                    "unavailable": 1,
                    "not_found": 1,
                    "ranked": 5,
                }
            ],
        )
        # The ranked read keeps the listing's clauses.
        ranked = [q for q in calls if "where[and][5][score][greater_than]" in q]
        self.assertEqual(ranked[0]["where[and][1][hidden][not_equals]"], "true")
        self.assertEqual(ranked[0]["sort"], "-score")

    async def test_low_scores_are_pinned_only_when_nothing_else_exists(
        self,
    ) -> None:
        library = [
            _activity("one", ["lo-a"], score=1),
            _activity("two", ["lo-a"], score=2),
        ]
        quest = _selecting([("lo-a", ["missing"])])

        with patch.object(module, "capture_message"):
            content = (
                await _store_reader(quest, [], library, []).read("quest-1")
            ).content

        self.assertEqual(content["missions"][0]["activity_ids"], ["two", "one"])

    async def test_a_build_that_carries_everything_over_reads_nothing_more(
        self,
    ) -> None:
        quest = _selecting([("lo-a", ["new-1"])])
        calls: List[Dict[str, str]] = []

        with patch.object(module, "capture_message") as capture:
            await _store_reader(quest, [_activity("new-1", ["lo-a"])], [], calls).read(
                "quest-1"
            )

        self.assertEqual(len(calls), 2)
        capture.assert_not_called()

    async def test_a_large_selection_is_read_in_batches(self) -> None:
        from synapse_pangea_chat.email_invite.course_missions import (
            SELECTED_CHUNK,
        )

        selected = [f"sel-{i:03d}" for i in range(SELECTED_CHUNK * 2 + 5)]
        library = [_activity(a, ["lo-a"]) for a in selected]
        quest = _selecting([("lo-a", selected)])
        calls: List[Dict[str, str]] = []

        content = (
            await _store_reader(quest, [], library, calls).read("quest-1")
        ).content
        self.assertEqual(content["missions"][0]["activity_ids"], selected)
        sizes = [
            len(q[k].split(","))
            for q in calls
            for k in q
            if "res.plan.activity_id" in k
        ]
        self.assertEqual(sizes, [SELECTED_CHUNK, SELECTED_CHUNK, 5])

    async def test_a_failed_read_by_id_builds_no_can_do_list(self) -> None:
        # Never part of a selection: the caller decides what the course does.
        quest = _selecting([("lo-a", ["old-1"])])
        reader = _store_reader(quest, [], [], [], by_id_code=503)

        with self.assertLogs(module.logger, "WARNING"):
            with self.assertRaises(CourseMissionsError) as raised:
                await reader.read("quest-1")
        self.assertEqual(raised.exception.code, CMS_UNAVAILABLE)

    async def test_a_full_window_is_logged(self) -> None:
        window = [_activity(f"new-{i}", ["lo-a"]) for i in range(200)]
        quest = _selecting([("lo-a", None)])

        with self.assertLogs(
            "synapse.module.synapse_pangea_chat.email_invite.course_missions",
            "INFO",
        ) as logs:
            content = (
                await _store_reader(quest, window, [], [], window_total=650).read(
                    "quest-1"
                )
            ).content
        # An entry that selects nothing pins the ranked ten, not the page.
        self.assertEqual(len(content["missions"][0]["activity_ids"]), 10)
        self.assertTrue(any("newest 200" in line for line in logs.output))


class _SteppingClock:
    """A clock each CMS request moves on by ``step`` seconds, so a slow CMS
    can be played out without waiting. Timers never fire."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def time(self) -> float:
        return self.now

    def call_later(self, *args: Any, **kwargs: Any) -> Any:
        return MagicMock()


class TestRankedReadsAreBounded(unittest.IsolatedAsyncioTestCase):
    """A quest that selects nothing needs ranked rows for every Can-Do; the
    CMS gets at most RANKED_CONCURRENCY Can-Dos at once, and what is not read
    within the budget is left for a later build."""

    def setUp(self) -> None:
        patcher = patch(
            "synapse_pangea_chat.cms_http.read_body_with_max_size", _read_body
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _reader(self, lo_ids: List[str], step: float) -> Any:
        clock = _SteppingClock(step)
        page = [_activity(f"page-{lo}", [lo]) for lo in lo_ids]
        ranked = {lo: [_activity(f"best-{lo}", [lo], score=8)] for lo in lo_ids}
        self.ranked_reads: List[str] = []

        async def request(method: str, uri: str, headers: Any = None) -> _Response:
            clock.now += clock.step
            query = dict(parse_qsl(urlparse(uri).query))
            if "quest-plans" in uri:
                return _Response(200, _quest(lo_ids))
            lo = query.get("where[and][4][learningObjectiveRefs][contains]")
            if lo is None:
                return _Response(200, {"docs": page, "totalDocs": len(page)})
            self.ranked_reads.append(lo)
            docs = ranked[lo] if "where[and][5][score][greater_than]" in query else []
            return _Response(200, {"docs": docs, "totalDocs": len(docs)})

        reader = _reader({}, [])
        reader._clock = clock  # type: ignore[assignment]
        reader._http.request = request  # type: ignore[method-assign, assignment]
        return reader

    async def test_ranked_reads_run_a_few_can_dos_at_a_time(self) -> None:
        lo_ids = [f"lo-{i}" for i in range(30)]
        reader = self._reader(lo_ids, step=0.0)
        limits: List[int] = []
        original = module.concurrently_execute

        def recording(func: Any, args: Any, limit: int) -> Any:
            limits.append(limit)
            return original(func, args, limit)

        with patch.object(module, "concurrently_execute", recording), patch.object(
            module, "capture_message"
        ):
            built = await reader.read("quest-1")

        self.assertEqual(limits, [module.RANKED_CONCURRENCY])
        self.assertEqual(built.unfinished, [])
        self.assertEqual(
            built.content["missions"][0]["activity_ids"], ["best-lo-0", "page-lo-0"]
        )

    async def test_a_slow_existence_read_never_costs_the_list(self) -> None:
        # Whether a missing selection is hidden or gone only labels the
        # capture: when time is up it is left unclassified, and the list is
        # built from what was read.
        clock = _SteppingClock(1.0)
        quest = _selecting([("lo-a", ["kept", "missing"])])
        reads: List[Dict[str, str]] = []

        async def request(method: str, uri: str, headers: Any = None) -> _Response:
            clock.now += clock.step
            query = dict(parse_qsl(urlparse(uri).query))
            reads.append(query)
            if "quest-plans" in uri:
                return _Response(200, quest)
            if any("res.plan.activity_id" in k for k in query):
                return _Response(200, {"docs": [], "totalDocs": 0})
            page = [_activity("kept", ["lo-a"])]
            return _Response(200, {"docs": page, "totalDocs": 1})

        reader = _reader({}, [])
        reader._clock = clock  # type: ignore[assignment]
        reader._http.request = request  # type: ignore[method-assign, assignment]

        with patch.object(module, "capture_message") as capture:
            built = await reader.read("quest-1")

        self.assertEqual(built.content["missions"][0]["activity_ids"], ["kept"])
        self.assertFalse(
            any("where[res.plan.activity_id][in]" in q for q in reads),
            "the existence read started after time was up",
        )
        self.assertEqual(
            capture.call_args.kwargs["context"]["entries"][0]["unclassified"], 1
        )

    async def test_can_dos_not_read_in_time_are_ranked_from_the_page_and_left(
        self,
    ) -> None:
        # Each request takes 0.1s of a 3s budget: the quest and the page, then
        # two reads per Can-Do. Thirty Can-Dos need six seconds.
        lo_ids = [f"lo-{i}" for i in range(30)]
        reader = self._reader(lo_ids, step=0.1)

        with patch.object(module, "capture_message") as capture:
            built = await reader.read("quest-1")

        self.assertTrue(0 < len(built.unfinished) < 30)
        self.assertEqual(len(set(self.ranked_reads)), 30 - len(built.unfinished))
        missions = {m["lo_id"]: m["activity_ids"] for m in built.content["missions"]}
        done = [lo for lo in lo_ids if lo not in built.unfinished]
        # What finished pins its best-scored rows; what did not still pins
        # its Can-Do's activities from the newest page, never nothing.
        self.assertEqual(missions[done[0]][0], f"best-{done[0]}")
        for lo in built.unfinished:
            self.assertEqual(missions[lo], [f"page-{lo}"])
        unfinished = [
            c.kwargs
            for c in capture.call_args_list
            if c.args[0] == "Can-Do list: ranked_unfinished"
        ]
        self.assertEqual(len(unfinished), 1)
        self.assertEqual(unfinished[0]["level"], "error")
        self.assertEqual(unfinished[0]["context"]["count"], len(built.unfinished))


if __name__ == "__main__":
    unittest.main()
