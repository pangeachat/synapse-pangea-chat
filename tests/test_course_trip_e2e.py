"""Integration tests for the course trip's operator endpoints: slicing an
existing course into cities and placing an undated course's ship.

Uses ``BaseSynapseE2ETest`` to spin up a local Synapse + PostgreSQL instance
with the ``PangeaChat`` module loaded and a mock CMS serving the quest, its
activities (with positions) and the city list.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

from .base_e2e import BaseSynapseE2ETest

_COURSE_PLAN = "pangea.course_plan"
_COURSE_COPY = "pangea.course_missions"
_POSITION = "pangea.course_position"
_PROGRESS = "pangea.course_progress"
_KEY = "test-cms-api-key"


class _TripCmsHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        code: int
        body: Dict[str, Any]
        if self.headers.get("Authorization") != f"service-users API-Key {_KEY}":
            code, body = 401, {"errors": [{"message": "unauthorized"}]}
        elif path == "/api/quest-plans/quest-1":
            code, body = 200, {
                "id": "quest-1",
                "req": {"target_language": "es"},
                "res": {
                    "learning_objective_sequence": [
                        {"learning_objective": "lo-a"},
                        {"learning_objective": "lo-b"},
                        {"learning_objective": "lo-c"},
                    ]
                },
            }
        elif path == "/api/activities-v2" and "activity_id][in]" in unquote(self.path):
            # By id: a teacher's own copy the library listing never returns,
            # set in Madrid.
            asked = (
                parse_qs(urlparse(self.path).query)
                .get("where[res.plan.activity_id][in]", [""])[0]
                .split(",")
            )
            code, body = 200, {
                "docs": [
                    {
                        "id": "row-t1",
                        "res": {
                            "plan": {"activity_id": "t1", "coordinates": [-3.70, 40.42]}
                        },
                    }
                ]
                if "t1" in asked
                else [],
                "hasNextPage": False,
            }
        elif path == "/api/activities-v2":

            def doc(
                activity_id: str, lo: str, lng: float, lat: float
            ) -> Dict[str, Any]:
                return {
                    "id": f"row-{activity_id}",
                    "req": {},
                    "res": {
                        "plan": {
                            "activity_id": activity_id,
                            "l2": "es",
                            "coordinates": [lng, lat],
                        }
                    },
                    "learningObjectiveRefs": [lo],
                    "hidden": False,
                    "visibility": "public",
                }

            code, body = 200, {
                "docs": [
                    # lo-a: three in Mexico City, one in Madrid: viable at Mexico City.
                    doc("a1", "lo-a", -99.16, 19.35),
                    doc("a2", "lo-a", -99.10, 19.50),
                    doc("a3", "lo-a", -99.13, 19.43),
                    doc("a4", "lo-a", -3.70, 40.42),
                    # lo-b: one in Madrid: thin.
                    doc("b1", "lo-b", -3.70, 40.42),
                    # lo-c: beyond every listed city: empty... of cities, pinned as is.
                    doc("c1", "lo-c", 0.5, 0.5),
                ]
            }
        elif path == "/api/cities":
            code, body = 200, {
                "docs": [
                    {
                        "key": "mx",
                        "name": "Mexico City",
                        "country": "Mexico",
                        "lat": 19.4444,
                        "lng": -99.1329,
                    },
                    {
                        "key": "es",
                        "name": "Madrid",
                        "country": "Spain",
                        "lat": 40.4,
                        "lng": -3.6834,
                    },
                ],
                "hasNextPage": False,
            }
        else:
            code, body = 404, {"errors": [{"message": "not found"}]}
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class TestCourseTripEndpoints(BaseSynapseE2ETest):
    maxDiff = None
    _cms: HTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._cms = HTTPServer(("127.0.0.1", 0), _TripCmsHandler)
        threading.Thread(target=cls._cms.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._cms.shutdown()
        cls._cms.server_close()
        super().tearDownClass()

    def _post(
        self, endpoint: str, body: Any, token: Optional[str]
    ) -> requests.Response:
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return requests.post(
            f"{self.server_url}/_synapse/client/pangea/v1/{endpoint}",
            json=body,
            headers=headers,
        )

    def _create_space(self, token: str, initial_state: list) -> str:
        resp = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json={
                "visibility": "private",
                "preset": "private_chat",
                "name": "Spanish 101",
                "creation_content": {"type": "m.space"},
                "initial_state": initial_state,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["room_id"]

    def _state(self, admin_token: str, room_id: str) -> Dict[tuple, Dict]:
        resp = requests.get(
            f"{self.server_url}/_synapse/admin/v1/rooms/{quote(room_id, safe='')}/state",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return {(e["type"], e["state_key"]): e for e in resp.json()["state"]}

    def _put_state(
        self, token: str, room_id: str, event_type: str, state_key: str, content: Any
    ) -> None:
        resp = requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/{quote(room_id, safe='')}/state/{event_type}/{quote(state_key, safe='')}",
            json=content,
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

    async def _server(self) -> None:
        port = self._cms.server_address[1]
        await self.homeserver(
            module_config={
                "cms_base_url": f"http://127.0.0.1:{port}",
                "cms_service_api_key": _KEY,
                "course_position_active_window_seconds": 7 * 86400,
            }
        )

    async def test_slicing_an_existing_course_pins_each_can_do_in_its_city(
        self,
    ) -> None:
        await self._server()
        (operator_id, admin_token) = await self.new_user("op", admin=True)
        (teacher_id, teacher_token) = await self.new_user("teacher")
        existing = {
            "version": 1,
            "revision": 2,
            "time_zone": "America/Mexico_City",
            "missions": [
                # Three in Mexico City outvote one in Madrid.
                {
                    "lo_id": "lo-a",
                    "activity_ids": ["a1", "a2", "a3", "a4", "t1"],
                    "note": "Week 1",
                },
                {
                    "lo_id": "lo-b",
                    "activity_ids": ["b1"],
                    "due_at": "2026-11-09T05:59:00Z",
                },
                {"lo_id": "lo-c", "activity_ids": ["c1"]},
            ],
        }
        space = self._create_space(
            teacher_token,
            [
                {
                    "type": _COURSE_PLAN,
                    "state_key": "",
                    "content": {"uuid": "quest-1", "l2": "es"},
                },
                {"type": _COURSE_COPY, "state_key": "", "content": existing},
            ],
        )
        body = {"room_id": space, "dry_run": True}

        self.assertEqual(self._post("slice_course", body, None).status_code, 401)
        self.assertEqual(
            self._post("slice_course", body, teacher_token).status_code, 403
        )
        plain = await self.create_private_room(teacher_token)
        self.assertEqual(
            self._post("slice_course", {"room_id": plain}, admin_token).status_code, 404
        )

        # The dry run reports the slices and writes nothing.
        resp = self._post("slice_course", body, admin_token)
        self.assertEqual(resp.status_code, 200, resp.text)
        answer = resp.json()
        self.assertEqual(answer["sender"], teacher_id)
        self.assertFalse(answer["changed"])
        self.assertEqual(answer["previous_revision"], 2)
        self.assertEqual(answer["revision"], 3)
        self.assertEqual(
            answer["report"]["can_dos"],
            [
                {
                    "lo_id": "lo-a",
                    "city": "Mexico City",
                    "pinned": 3,
                    "status": "viable",
                },
                {"lo_id": "lo-b", "city": "Madrid", "pinned": 1, "status": "thin"},
                {"lo_id": "lo-c", "city": None, "pinned": 1, "status": "thin"},
            ],
        )
        self.assertEqual(
            self._state(admin_token, space)[(_COURSE_COPY, "")]["content"], existing
        )

        # The write: version 2, undated, each Can-Do at its city, notes and dates kept.
        resp = self._post("slice_course", {"room_id": space}, admin_token)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["changed"])
        copy = self._state(admin_token, space)[(_COURSE_COPY, "")]
        self.assertEqual(copy["sender"], teacher_id)
        self.assertEqual(
            copy["content"],
            {
                "version": 2,
                "revision": 3,
                "time_zone": "America/Mexico_City",
                "missions": [
                    {
                        "lo_id": "lo-a",
                        "activity_ids": ["a1", "a2", "a3"],
                        "city": {
                            "name": "Mexico City",
                            "lat_e6": 19444400,
                            "lng_e6": -99132900,
                        },
                        "note": "Week 1",
                        # Madrid's activities leave the pins, keeping their
                        # credit: the teacher's own "t1" is placed there by id.
                        "removed_activity_ids": ["a4", "t1"],
                    },
                    {
                        "lo_id": "lo-b",
                        "activity_ids": ["b1"],
                        "city": {
                            "name": "Madrid",
                            "lat_e6": 40400000,
                            "lng_e6": -3683400,
                        },
                        "due_at": "2026-11-09T05:59:00Z",
                    },
                    {"lo_id": "lo-c", "activity_ids": ["c1"]},
                ],
            },
        )
        # Nobody joined and nobody's power changed.
        state = self._state(admin_token, space)
        self.assertNotIn(("m.room.member", operator_id), state)

        # The pass is one-time: a trip is never sliced again unless forced.
        resp = self._post("slice_course", {"room_id": space}, admin_token)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertTrue(resp.json()["already_sliced"])
        resp = self._post(
            "slice_course",
            {"room_id": space, "dry_run": True, "force": True},
            admin_token,
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        # A space with no local instructor is refused and reported.
        (student_id, student_token) = await self.new_user("student")
        lonely = self._create_space(
            teacher_token,
            [
                {
                    "type": _COURSE_PLAN,
                    "state_key": "",
                    "content": {"uuid": "quest-1", "l2": "es"},
                }
            ],
        )
        # The teacher steps down to power level 50: nobody local at 100 is left.
        self._put_state(
            teacher_token,
            lonely,
            "m.room.power_levels",
            "",
            {"users": {teacher_id: 50}, "users_default": 0, "state_default": 50},
        )
        resp = self._post("slice_course", {"room_id": lonely}, admin_token)
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertIn("report", resp.json())

    async def test_the_ship_sits_at_the_busiest_stop_of_an_undated_course(self) -> None:
        await self._server()
        (operator_id, admin_token) = await self.new_user("op", admin=True)
        (teacher_id, teacher_token) = await self.new_user("teacher")
        (s1_id, s1_token) = await self.new_user("s1")
        (s2_id, s2_token) = await self.new_user("s2")
        (s3_id, s3_token) = await self.new_user("s3")
        trip = {
            "version": 2,
            "revision": 1,
            "missions": [
                {
                    "lo_id": "lo-a",
                    "activity_ids": ["a1"],
                    "city": {
                        "name": "Mexico City",
                        "lat_e6": 19444400,
                        "lng_e6": -99132900,
                    },
                },
                {
                    "lo_id": "lo-b",
                    "activity_ids": ["b1"],
                    "city": {"name": "Madrid", "lat_e6": 40400000, "lng_e6": -3683400},
                },
                {"lo_id": "lo-c", "activity_ids": ["c1"]},
            ],
        }
        space = self._create_space(
            teacher_token,
            [
                {
                    "type": _COURSE_PLAN,
                    "state_key": "",
                    "content": {"uuid": "quest-1", "l2": "es"},
                },
                {"type": _COURSE_COPY, "state_key": "", "content": trip},
            ],
        )
        # Nobody active yet: the first stop.
        resp = self._post("course_position", {"room_id": space}, admin_token)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["outcome"], "written")
        self.assertEqual(resp.json()["active_members"], 0)
        first = self._state(admin_token, space)[(_POSITION, "")]
        self.assertEqual(first["sender"], teacher_id)
        self.assertEqual(
            (first["content"]["stop"], first["content"]["from_stop"]), (0, 0)
        )

        # Three students join; two are active at Madrid (lo-a done), one at
        # Mexico City; a fourth record is stale and does not count.
        for uid, token in ((s1_id, s1_token), (s2_id, s2_token), (s3_id, s3_token)):
            await self.invite_user_to_room(space, uid, teacher_token)
            await self.accept_room_invitation(space, token)
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc)
        fresh = now.isoformat().replace("+00:00", "Z")
        stale = (now - datetime.timedelta(days=10)).isoformat().replace("+00:00", "Z")

        def progress(done: List[str], at: str) -> Dict[str, Any]:
            return {
                "computed_at": at,
                "app_version": "test",
                "xp_to_complete": 1000,
                "can_dos": {
                    lo: {"completed": lo in done} for lo in ("lo-a", "lo-b", "lo-c")
                },
            }

        members: List[Tuple[str, str, List[str], str]] = [
            (s1_id, s1_token, ["lo-a"], fresh),
            (s2_id, s2_token, ["lo-a"], fresh),
            (s3_id, s3_token, [], fresh),
        ]
        for uid, token, done, at in members:
            analytics = requests.post(
                f"{self.server_url}/_matrix/client/v3/createRoom",
                json={
                    "visibility": "private",
                    "creation_content": {"type": "p.analytics"},
                },
                headers={"Authorization": f"Bearer {token}"},
            ).json()["room_id"]
            requests.put(
                f"{self.server_url}/_matrix/client/v3/profile/{quote(uid, safe='')}/pangea.analytics_profile",
                json={
                    "pangea.analytics_profile": {
                        "analytics": {"es": {"analytics_room_id": analytics}}
                    }
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            self._put_state(token, analytics, _PROGRESS, space, progress(done, at))
        # The teacher's own record is stale: not active.
        teacher_analytics = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json={"visibility": "private", "creation_content": {"type": "p.analytics"}},
            headers={"Authorization": f"Bearer {teacher_token}"},
        ).json()["room_id"]
        requests.put(
            f"{self.server_url}/_matrix/client/v3/profile/{quote(teacher_id, safe='')}/pangea.analytics_profile",
            json={
                "pangea.analytics_profile": {
                    "analytics": {"es": {"analytics_room_id": teacher_analytics}}
                }
            },
            headers={"Authorization": f"Bearer {teacher_token}"},
        )
        self._put_state(
            teacher_token, teacher_analytics, _PROGRESS, space, progress([], stale)
        )

        resp = self._post(
            "course_position", {"room_id": space, "dry_run": True}, admin_token
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["outcome"], "would_write")
        self.assertEqual(resp.json()["active_members"], 3)
        self.assertEqual(resp.json()["position"]["stop"], 1)
        # Still the first write's position: a dry run writes nothing.
        self.assertEqual(
            self._state(admin_token, space)[(_POSITION, "")]["content"]["stop"], 0
        )

        resp = self._post("course_position", {"room_id": space}, admin_token)
        self.assertEqual(resp.json()["outcome"], "written")
        after = self._state(admin_token, space)[(_POSITION, "")]["content"]
        self.assertEqual((after["stop"], after["from_stop"]), (1, 0))
        self.assertGreater(after["since"], 0)
        # The space holds the stop and nothing about who is where.
        self.assertEqual(set(after), {"stop", "from_stop", "since", "revision"})
        self.assertEqual(after["revision"], 1)
        # Unchanged on the next pass.
        resp = self._post("course_position", {"room_id": space}, admin_token)
        self.assertEqual(resp.json()["outcome"], "unchanged")

        # A dated course's ship derives from its dates: the pass leaves it alone.
        dated = self._create_space(
            teacher_token,
            [
                {
                    "type": _COURSE_PLAN,
                    "state_key": "",
                    "content": {"uuid": "quest-1", "l2": "es"},
                },
                {
                    "type": _COURSE_COPY,
                    "state_key": "",
                    "content": {**trip, "start_date": "2026-11-02"},
                },
            ],
        )
        resp = self._post("course_position", {"room_id": dated}, admin_token)
        self.assertEqual(resp.json()["outcome"], "not_an_undated_trip")
        self.assertNotIn((_POSITION, ""), self._state(admin_token, dated))


class TestCourseTripAutoSlice(TestCourseTripEndpoints):
    """With version 2 on, a copy an app writes as version 1 is sliced by the
    server: the one slicer outside the dashboard's publish."""

    async def _server(self) -> None:
        port = self._cms.server_address[1]
        await self.homeserver(
            module_config={
                "cms_base_url": f"http://127.0.0.1:{port}",
                "cms_service_api_key": _KEY,
                "course_missions_v2_enabled": True,
            }
        )

    # The parent class's tests run under the parent's configuration only.
    test_slicing_an_existing_course_pins_each_can_do_in_its_city = None  # type: ignore[assignment]
    test_the_ship_sits_at_the_busiest_stop_of_an_undated_course = None  # type: ignore[assignment]

    async def test_a_copy_an_app_writes_is_sliced_into_cities(self) -> None:
        await self._server()
        (_, admin_token) = await self.new_user("op", admin=True)
        (teacher_id, teacher_token) = await self.new_user("teacher")
        space = self._create_space(
            teacher_token,
            [
                {
                    "type": _COURSE_PLAN,
                    "state_key": "",
                    "content": {"uuid": "quest-1", "l2": "es"},
                }
            ],
        )
        # What the app writes at course creation: its own list, unsliced.
        self._put_state(
            teacher_token,
            space,
            _COURSE_COPY,
            "",
            {
                "version": 1,
                "revision": 1,
                "missions": [
                    {"lo_id": "lo-a", "activity_ids": ["a1", "a2", "a3", "a4"]},
                    {"lo_id": "lo-b", "activity_ids": ["b1"]},
                ],
            },
        )
        copy: Dict[str, Any] = {}
        for _ in range(50):
            copy = self._state(admin_token, space)[(_COURSE_COPY, "")]
            if copy["content"].get("version") == 2:
                break
            await asyncio.sleep(0.1)
        self.assertEqual(copy["content"]["version"], 2, copy)
        self.assertEqual(copy["sender"], teacher_id)
        self.assertEqual(copy["content"]["revision"], 2)
        lo_a = copy["content"]["missions"][0]
        self.assertEqual(lo_a["activity_ids"], ["a1", "a2", "a3"])
        self.assertEqual(lo_a["city"]["name"], "Mexico City")
        self.assertEqual(lo_a["removed_activity_ids"], ["a4"])
        # The trip is never sliced again by the hook.
        await asyncio.sleep(0.5)
        self.assertEqual(
            self._state(admin_token, space)[(_COURSE_COPY, "")]["content"]["revision"],
            2,
        )
