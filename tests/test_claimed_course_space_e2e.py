"""E2E: the course space a claim creates, read back from a real Synapse.

A requested course's space is created when its first instructor claims the
invitation (create-course-space.instructions.md). These tests make one the
way production does, through the v2 preparation and the emailed claim link,
and read the resulting state over the client-server API, so the assertions are
about what landed in room state, not about what the handler meant to write.

The catalog half publishes the space and browses for it: every other catalog
test builds its rooms with a local helper, so nothing else checks that a space
the claim created is one the catalog finds.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Collection, Dict, List, Tuple
from urllib.parse import parse_qsl, quote, urlparse

import requests

from synapse_pangea_chat.public_courses.get_public_courses import (
    extract_l2,
    extract_plan_id,
)
from tests.base_e2e import BaseSynapseE2ETest
from tests.requested_course import PREPARE_PATH, claimed_course
from tests.smtp_sink import SmtpSink

COURSE_PLAN_EVENT_TYPE = "pangea.course_plan"
COURSE_MISSIONS_EVENT_TYPE = "pangea.course_missions"
PUBLIC_COURSES_PATH = "/_synapse/client/pangea/v1/public_courses"
APP_BASE_URL = "https://app.example.test"


class _QuestCmsHandler(BaseHTTPRequestHandler):
    """Answers the two reads a course space's Can-Do list is built from."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:
        auth = self.headers.get("Authorization")
        path = urlparse(self.path).path
        query = dict(parse_qsl(urlparse(self.path).query))
        code: int
        body: Dict[str, Any]
        if auth != "service-users API-Key test-cms-api-key":
            code, body = 401, {"errors": [{"message": "unauthorized"}]}
        elif path == "/api/quest-plans/quest-1":
            code, body = 200, {
                "id": "quest-1",
                "req": {"target_language": "es"},
                "res": {
                    "learning_objective_sequence": [
                        # The plan's selection and note start the Can-Do list.
                        {
                            "learning_objective": "lo-b",
                            "activity_ids": ["act-b2"],
                            "note": "Unit 3",
                        },
                        {"learning_objective": "lo-a", "activity_ids": None},
                    ]
                },
            }
        elif path == "/api/quest-plans/quest-gone":
            code, body = 200, {
                "id": "quest-gone",
                "req": {"target_language": "es"},
                "res": {
                    "learning_objective_sequence": [
                        {
                            "learning_objective": "lo-a",
                            "activity_ids": ["act-gone"],
                            "note": "Order food " * 40,
                        },
                    ]
                },
            }
        elif (
            path == "/api/activities-v2" and "where[res.plan.activity_id][in]" in query
        ):
            # The selected activity is no row at all.
            code, body = 200, {"docs": [], "totalDocs": 0}
        elif path == "/api/activities-v2" and "where[and][5][score][exists]" in query:
            code, body = 200, {"docs": [], "totalDocs": 0}
        elif (
            path == "/api/activities-v2"
            and "where[and][5][score][greater_than]" in query
        ):
            code, body = 200, {
                "docs": [
                    {
                        "id": "row-1",
                        "req": {},
                        "res": {"plan": {"activity_id": "act-a", "l2": "es"}},
                        "learningObjectiveRefs": ["lo-a"],
                        "hidden": False,
                        "visibility": "public",
                        "score": 8,
                    },
                ],
                "totalDocs": 1,
            }
        elif path == "/api/activities-v2":
            code, body = 200, {
                "docs": [
                    {
                        "id": "row-1",
                        "req": {},
                        "res": {"plan": {"activity_id": "act-a", "l2": "es"}},
                        "learningObjectiveRefs": ["lo-a"],
                        "hidden": False,
                        "visibility": "public",
                    },
                    {
                        "id": "row-2",
                        "req": {},
                        "res": {"plan": {"activity_id": "act-b", "l2": "es"}},
                        "learningObjectiveRefs": ["lo-b"],
                        "hidden": False,
                        "visibility": "public",
                    },
                    {
                        "id": "row-3",
                        "req": {},
                        "res": {"plan": {"activity_id": "act-b2", "l2": "es"}},
                        "learningObjectiveRefs": ["lo-b"],
                        "hidden": False,
                        "visibility": "public",
                    },
                ]
            }
        else:
            code, body = 404, {"errors": [{"message": "not found"}]}
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class TestClaimedCourseSpaceE2E(BaseSynapseE2ETest):
    sink: SmtpSink
    _cms: HTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.sink = SmtpSink().start()
        cls.addClassCleanup(cls.sink.stop)
        cls._cms = HTTPServer(("127.0.0.1", 0), _QuestCmsHandler)
        threading.Thread(target=cls._cms.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._cms.shutdown()
        cls._cms.server_close()
        super().tearDownClass()

    async def _setup(self) -> Tuple[str, str, str]:
        """The class's homeserver, an operator and a teacher. Returns the
        operator's token, the teacher's id and the teacher's token."""
        await self.homeserver(
            module_config={
                "app_base_url": APP_BASE_URL,
                "cms_base_url": f"http://127.0.0.1:{self._cms.server_address[1]}",
            },
            synapse_config_overrides={"email": self.sink.synapse_email_config()},
        )
        _, operator = await self.new_user("operator", admin=True)
        teacher_id, teacher = await self.new_user("teacher")
        return operator, teacher_id, teacher

    def _claimed(self, operator: str, teacher: str, name: str, **spec: Any) -> str:
        return claimed_course(
            self.server_url,
            self.sink,
            APP_BASE_URL,
            operator,
            teacher,
            f"{name}+{self.tag}@school.example",
            f"{name}-{self.tag}",
            **spec,
        )

    def _get(self, token: str, path: str, **params: str) -> Any:
        response = requests.get(
            f"{self.server_url}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _state(self, token: str, room_id: str, kind: str) -> Dict[str, Any]:
        return self._get(
            token, f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/{kind}"
        )

    def _state_events(self, token: str, room_id: str) -> Dict[Tuple[str, str], Any]:
        events = self._get(
            token, f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state"
        )
        return {(e["type"], e["state_key"]): e for e in events}

    def _publish_to_directory(self, token: str, room_id: str) -> None:
        """Publish in the public room directory, half of the catalog's rule.
        The claim creates the space unpublished: a course becomes browsable
        when someone publishes it, not at creation."""
        response = requests.put(
            f"{self.server_url}/_matrix/client/v3/directory/list/room/"
            f"{quote(room_id, safe='')}",
            json={"visibility": "public"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)

    @staticmethod
    def _room_ids(body: Dict[str, Any], mine: Collection[str]) -> List[str]:
        """The catalog's room ids among *mine*: the class's tests share a
        homeserver, so the catalog also lists what the others published."""
        return sorted(c["room_id"] for c in body["chunk"] if c["room_id"] in mine)

    async def test_course_plan_state_event_and_catalog_visibility(self) -> None:
        operator, _, teacher = await self._setup()

        room_id = self._claimed(
            operator,
            teacher,
            "catalog",
            title="Spanish for Travel",
            course_plan_id="plan-es-mx",
            target_language="es-MX",
        )
        content = self._state(teacher, room_id, COURSE_PLAN_EVENT_TYPE)
        # The plan id lands under ``uuid``, preserved exactly. Under
        # ``course_plan_id`` the catalog's fallback would still read it, so
        # nothing downstream would fail; this is asserted on the key.
        self.assertEqual(content.get("uuid"), "plan-es-mx")
        self.assertNotIn("course_plan_id", content)
        self.assertEqual(content.get("l2"), "es-MX")

        # A preparation without a language code is refused, so no claimed
        # course lacks one.
        for label, target_language in (("missing", None), ("name", "Spanish")):
            with self.subTest(target_language=label):
                body = {
                    "request_key": f"no-language-{label}-{self.tag}",
                    "teacher_email": f"nolang+{self.tag}@school.example",
                    "title": "No language",
                    "course_plan_id": "plan-nolang",
                }
                if target_language is not None:
                    body["target_language"] = target_language
                refused = requests.post(
                    f"{self.server_url}{PREPARE_PATH}",
                    json=body,
                    headers={"Authorization": f"Bearer {operator}"},
                    timeout=30,
                )
                self.assertEqual(refused.status_code, 400, refused.text)

        self._publish_to_directory(teacher, room_id)
        unfiltered = self._get(teacher, PUBLIC_COURSES_PATH, limit="20")
        self.assertEqual(self._room_ids(unfiltered, {room_id}), [room_id])
        # What the catalog serves is what its own extractors read off the
        # stored state: restating the key names here would let the write and
        # the read drift apart while both tests kept passing.
        served = {c["room_id"]: c for c in unfiltered["chunk"]}[room_id]
        self.assertEqual(served["course_id"], extract_plan_id(content))
        self.assertEqual(served["target_language"], extract_l2(content))

        # A base-code filter matches the regionally tagged course (issue #53).
        for language, expected in (("es", [room_id]), ("es-ES", [room_id]), ("fr", [])):
            with self.subTest(filter=language):
                filtered = self._get(
                    teacher, PUBLIC_COURSES_PATH, limit="20", target_language=language
                )
                self.assertEqual(self._room_ids(filtered, {room_id}), expected)

    async def test_power_levels_and_course_settings(self) -> None:
        """The claimant is the course's admin; a regular member can attach a
        room (learners' activity sessions fan out into their courses as
        ``m.space.child``, client activities doc); and the course requires
        instructor analytics access to join, as a course created in the client
        does. The settings event must exist and be true: an absent one reads
        as off (course-analytics-access)."""
        operator, teacher_id, teacher = await self._setup()
        room_id = self._claimed(operator, teacher, "power")

        power = self._state(teacher, room_id, "m.room.power_levels")
        self.assertEqual(power["users"].get(teacher_id), 100)
        self.assertEqual(power["events"]["m.space.child"], 0)
        # Only the child write is opened up; other state stays admin-gated.
        self.assertEqual(power["state_default"], 50)
        self.assertEqual(power["users_default"], 0)
        self.assertEqual(power["events"][COURSE_MISSIONS_EVENT_TYPE], 100)

        settings = self._state(teacher, room_id, "pangea.course_settings")
        self.assertIs(settings.get("require_analytics_access"), True)

    async def test_can_do_list_written_at_the_claim_and_absent_when_cms_fails(
        self,
    ) -> None:
        """The course space's Can-Do list lands in room state, sent by the
        claimant, built from the quest's selections and notes; a quest the CMS
        cannot answer still gets its course, without a list."""
        operator, teacher_id, teacher = await self._setup()

        room_id = self._claimed(operator, teacher, "list", course_plan_id="quest-1")
        state = self._state_events(teacher, room_id)
        event = state[(COURSE_MISSIONS_EVENT_TYPE, "")]
        self.assertEqual(event["sender"], teacher_id)
        self.assertEqual(
            event["content"],
            {
                "version": 1,
                "revision": 1,
                "missions": [
                    {"lo_id": "lo-b", "activity_ids": ["act-b2"], "note": "Unit 3"},
                    {"lo_id": "lo-a", "activity_ids": ["act-a"]},
                ],
            },
        )

        room_id = self._claimed(
            operator, teacher, "missing", course_plan_id="missing-quest"
        )
        state = self._state_events(teacher, room_id)
        self.assertNotIn((COURSE_MISSIONS_EVENT_TYPE, ""), state)
        self.assertEqual(
            state[(COURSE_PLAN_EVENT_TYPE, "")]["content"],
            {"uuid": "missing-quest", "l2": "es"},
        )

    async def test_a_selection_that_is_gone_pins_the_ranked_list(self) -> None:
        """An entry whose selected activities are all gone pins its Can-Do's
        best-scored activities, never every listed one, and an over-long note
        is shortened rather than dropped."""
        operator, _, teacher = await self._setup()

        room_id = self._claimed(operator, teacher, "gone", course_plan_id="quest-gone")
        [mission] = self._state_events(teacher, room_id)[
            (COURSE_MISSIONS_EVENT_TYPE, "")
        ]["content"]["missions"]

        self.assertEqual(mission["lo_id"], "lo-a")
        self.assertEqual(mission["activity_ids"], ["act-a"])
        self.assertLessEqual(len(mission["note"]), 280)
        self.assertTrue(mission["note"].startswith("Order food Order food"))
        self.assertTrue(mission["note"].endswith("\u2026"))


if __name__ == "__main__":
    unittest.main()
