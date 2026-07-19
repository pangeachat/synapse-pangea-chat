"""E2E tests for the course-plan state event create_course_space writes.

Runs a real local Synapse + PostgreSQL (BaseSynapseE2ETest), calls the real
endpoint, and reads the resulting state event back over the Matrix
client-server API — so the assertions are about what actually landed in room
state, not about what the handler intended to write.

The second half publishes those spaces and browses for them, closing the loop
the ``course_plan_id``-vs-``uuid`` mismatch slipped through: every existing
catalog test builds its rooms with a local helper, so nothing checked that a
space this endpoint created was one the catalog would find.
"""

import unittest
from typing import Any, Dict, List
from urllib.parse import quote

import requests

from synapse_pangea_chat.public_courses.get_public_courses import (
    extract_l2,
    extract_plan_id,
)
from tests.base_e2e import BaseSynapseE2ETest

COURSE_PLAN_EVENT_TYPE = "pangea.course_plan"
CREATE_COURSE_SPACE_PATH = "/_synapse/client/pangea/v1/create_course_space"
PUBLIC_COURSES_PATH = "/_synapse/client/pangea/v1/public_courses"


class TestCreateCourseSpaceE2E(BaseSynapseE2ETest):
    def _create_course_space(self, access_token: str, **body: Any) -> Dict[str, Any]:
        response = requests.post(
            f"{self.server_url}{CREATE_COURSE_SPACE_PATH}",
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _get_course_plan_content(
        self, access_token: str, room_id: str
    ) -> Dict[str, Any]:
        """The current ``pangea.course_plan`` content, straight from room state.

        Read over the client-server API rather than out of the handler, so an
        absent key is genuinely absent from the stored event — the distinction
        between "no l2" and "l2: null" only exists at this layer.
        """
        room_id_path = quote(room_id, safe="")
        response = requests.get(
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}"
            f"/state/{COURSE_PLAN_EVENT_TYPE}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _publish_to_directory(self, access_token: str, room_id: str) -> None:
        """Publish in the public room directory — half of the eligibility rule.

        The endpoint creates the space unpublished, which is correct: a course
        becomes browsable when someone publishes it, not at creation.
        """
        room_id_path = quote(room_id, safe="")
        response = requests.put(
            f"{self.server_url}/_matrix/client/v3/directory/list/room/{room_id_path}",
            json={"visibility": "public"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _get_courses(self, access_token: str, **params: str) -> Dict[str, Any]:
        response = requests.get(
            f"{self.server_url}{PUBLIC_COURSES_PATH}",
            params={"limit": "20", **params},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def _by_room_id(body: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {course["room_id"]: course for course in body["chunk"]}

    @staticmethod
    def _room_ids(body: Dict[str, Any]) -> List[str]:
        return sorted(course["room_id"] for course in body["chunk"])

    async def test_course_plan_state_event_and_catalog_visibility(self) -> None:
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None

        try:
            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse()

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="teacher",
                password="123123123",
                admin=True,
            )
            _, token = await self.login_user(user="teacher", password="123123123")

            # --- A space created with a target language ---------------------
            # The language is passed padded to prove trimming happens before
            # the value reaches room state, where the catalog matches on it.
            spanish = self._create_course_space(
                token,
                title="Spanish for Travel",
                teacher_email="teacher@example.com",
                description="A travel course",
                course_plan_id="plan-es-mx",
                target_language="  es-MX  ",
            )
            spanish_room_id = spanish["room_id"]

            spanish_content = self._get_course_plan_content(token, spanish_room_id)

            # The plan id lands under ``uuid``, preserved exactly. Writing it
            # under ``course_plan_id`` would still be read by the catalog's
            # fallback, so nothing downstream would fail — which is why this
            # is asserted on the key and not only on the behaviour.
            self.assertEqual(spanish_content.get("uuid"), "plan-es-mx")
            self.assertNotIn("course_plan_id", spanish_content)
            self.assertEqual(spanish_content.get("l2"), "es-MX")

            # --- A space created without one --------------------------------
            no_language = self._create_course_space(
                token,
                title="Unspecified Language",
                teacher_email="teacher@example.com",
                course_plan_id="plan-nolang",
            )
            no_language_room_id = no_language["room_id"]

            no_language_content = self._get_course_plan_content(
                token, no_language_room_id
            )

            self.assertEqual(no_language_content.get("uuid"), "plan-nolang")
            # Absent, not null and not empty: the key is simply not there.
            self.assertNotIn("l2", no_language_content)

            # --- Blank and non-string target languages ----------------------
            # Each is a distinct way a caller can fail to supply a language,
            # and all three must produce the same state as omitting it.
            for label, target_language in (
                ("empty", ""),
                ("whitespace", "   "),
                ("non-string", 42),
            ):
                with self.subTest(target_language=label):
                    blank = self._create_course_space(
                        token,
                        title=f"Blank {label}",
                        teacher_email="teacher@example.com",
                        course_plan_id=f"plan-blank-{label}",
                        target_language=target_language,
                    )
                    blank_content = self._get_course_plan_content(
                        token, blank["room_id"]
                    )
                    self.assertEqual(blank_content.get("uuid"), f"plan-blank-{label}")
                    self.assertNotIn("l2", blank_content)

            # --- Published, these spaces are courses the catalog finds -------
            self._publish_to_directory(token, spanish_room_id)
            self._publish_to_directory(token, no_language_room_id)

            unfiltered = self._get_courses(token)
            self.assertEqual(
                self._room_ids(unfiltered),
                sorted([spanish_room_id, no_language_room_id]),
                f"unexpected catalog: {unfiltered}",
            )

            # What the catalog serves is what the catalog's own extractors read
            # off the stored state. Restating the key names here would let the
            # write and the read drift apart while both tests kept passing.
            courses = self._by_room_id(unfiltered)
            self.assertEqual(
                courses[spanish_room_id]["course_id"],
                extract_plan_id(spanish_content),
            )
            self.assertEqual(
                courses[spanish_room_id]["target_language"],
                extract_l2(spanish_content),
            )
            self.assertEqual(
                courses[no_language_room_id]["course_id"],
                extract_plan_id(no_language_content),
            )
            self.assertIsNone(
                courses[no_language_room_id]["target_language"],
                "a space created without a target language has no l2 to serve",
            )

            # A base-code filter matches the regionally-tagged space this
            # endpoint wrote (issue #53), and excludes the one with no l2.
            filtered = self._get_courses(token, target_language="es")
            self.assertEqual(self._room_ids(filtered), [spanish_room_id])

            regional = self._get_courses(token, target_language="es-ES")
            self.assertEqual(self._room_ids(regional), [spanish_room_id])

            other = self._get_courses(token, target_language="fr")
            self.assertEqual(self._room_ids(other), [])
        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )


if __name__ == "__main__":
    unittest.main()
