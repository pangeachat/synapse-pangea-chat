"""Unit tests for the course copy a server-created course starts with.

The copy is a snapshot of the shared course list: the quest's learning
objectives in order, each with the visible library activities at the course's
target language that reference it (CONTRACT "Building a copy"). A CMS failure
leaves the course without a copy instead of failing creation.
"""

import json
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock
from urllib.parse import parse_qsl, urlparse

from twisted.internet import defer

from synapse_pangea_chat.email_invite.course_missions import (
    COURSE_MISSIONS_STATE_EVENT_TYPE,
    CourseMissionsError,
    CourseMissionsReader,
    build_course_missions_content,
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
        # Readers reject a copy that names the same Mission twice.
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

    def test_quest_without_target_language_cannot_be_copied(self) -> None:
        with self.assertRaises(CourseMissionsError):
            build_course_missions_content(_quest(["lo-a"], l2=None), [])


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
            "synapse_pangea_chat.email_invite.course_missions.read_body_with_max_size",
            _read_body,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_builds_the_copy_from_the_quest_and_its_activities(self) -> None:
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

        content = await reader.read("quest-1")

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

    async def test_cms_error_yields_no_copy_and_is_logged(self) -> None:
        reader = _reader({"/api/quest-plans/": _Response(500, {"errors": []})}, [])

        with self.assertLogs(
            "synapse.module.synapse_pangea_chat.email_invite.course_missions",
            "WARNING",
        ) as logs:
            content = await reader.read("quest-1")

        self.assertIsNone(content)
        self.assertIn("No course copy written for quest quest-1", logs.output[0])

    async def test_unreachable_cms_yields_no_copy(self) -> None:
        with self.assertLogs(
            "synapse.module.synapse_pangea_chat.email_invite.course_missions",
            "WARNING",
        ):
            self.assertIsNone(await _reader({}, []).read("quest-1"))

    async def test_failed_activity_read_yields_no_copy(self) -> None:
        """A partial read is not a copy: an empty course would replace the
        shared list for every member."""
        reader = _reader(
            {
                "/api/quest-plans/": _Response(200, _quest(["lo-a"])),
                "/api/activities-v2": _Response(503, {}),
            },
            [],
        )

        with self.assertLogs(
            "synapse.module.synapse_pangea_chat.email_invite.course_missions",
            "WARNING",
        ):
            self.assertIsNone(await reader.read("quest-1"))

    async def test_no_quest_id_reads_nothing(self) -> None:
        calls: List[str] = []
        with self.assertLogs(
            "synapse.module.synapse_pangea_chat.email_invite.course_missions",
            "WARNING",
        ):
            self.assertIsNone(await _reader({}, calls).read(""))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
