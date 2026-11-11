"""E2E test for what the public course catalog says of each course beyond
its listing: the public part of its trip, its school, and who runs it, read
from real room state (public-courses.instructions.md, "Trips" and "Who runs a
course")."""

from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from tests.base_e2e import BaseSynapseE2ETest

PUBLIC_COURSES_PATH = "/_synapse/client/pangea/v1/public_courses"

COPY = {
    "version": 2,
    "revision": 2,
    "missions": [
        {
            "lo_id": "lo1",
            "activity_ids": ["a1"],
            "note": "for members only",
            "city": {"name": "Oaxaca", "lat_e6": 17065400, "lng_e6": -96726600},
        }
    ],
}


class TestPublicCoursesTripE2E(BaseSynapseE2ETest):
    def _create_course(
        self,
        token: str,
        plan_id: str,
        state: List[Dict[str, Any]],
        power: Optional[Dict[str, Any]] = None,
    ) -> str:
        body: Dict[str, Any] = {
            "name": plan_id,
            "visibility": "public",
            "preset": "public_chat",
            "initial_state": [
                {
                    "type": "pangea.course_plan",
                    "state_key": "",
                    "content": {"uuid": plan_id, "l2": "es"},
                },
                *state,
            ],
        }
        if power is not None:
            body["power_level_content_override"] = power
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["room_id"]

    def _join(self, token: str, room_id: str) -> None:
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/join/{quote(room_id, safe='')}",
            json={},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _catalog(self, token: str) -> Dict[str, Dict[str, Any]]:
        response = requests.get(
            f"{self.server_url}{PUBLIC_COURSES_PATH}",
            params={"limit": 100},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {c["room_id"]: c for c in response.json()["chunk"]}

    async def test_a_course_carries_its_trip_school_and_admins(self) -> None:
        await self.homeserver()
        teacher_id, teacher_token = await self.new_user("trip_teacher")
        aide_id, aide_token = await self.new_user("trip_aide")

        room = self._create_course(
            teacher_token,
            "plan-trip",
            [
                {"type": "pangea.course_missions", "state_key": "", "content": COPY},
                {
                    "type": "pangea.course_institution",
                    "state_key": "",
                    "content": {"name": "Escuela Uno", "id": "ror:abc123"},
                },
            ],
        )
        self._join(aide_token, room)

        everyone = self._create_course(
            teacher_token, "plan-everyone", [], power={"users_default": 100}
        )
        self._join(aide_token, everyone)

        catalog = self._catalog(aide_token)
        course = catalog[room]
        # The trip without the teacher's note; the school; the creator alone
        # runs it, since the aide joined at the default level.
        self.assertEqual(
            course["course_missions"]["missions"][0],
            {
                "lo_id": "lo1",
                "activity_ids": ["a1"],
                "city": {"name": "Oaxaca", "lat_e6": 17065400, "lng_e6": -96726600},
            },
        )
        self.assertEqual(
            course["institution"], {"name": "Escuela Uno", "id": "ror:abc123"}
        )
        self.assertEqual([a["user_id"] for a in course["admins"]], [teacher_id])

        # Where the default level makes every member an admin, every joined
        # member runs the course.
        self.assertEqual(
            sorted(a["user_id"] for a in catalog[everyone]["admins"]),
            sorted([teacher_id, aide_id]),
        )
