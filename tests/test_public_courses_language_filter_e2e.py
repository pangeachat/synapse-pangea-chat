"""E2E test for the public_courses target-language filter (issue #53).

Reproduces the reported failure: courses whose CMS ``l2`` carries a
regional tag (``es-ES``) are invisible to a base-code filter
(``target_language=es``) when filtering uses exact CMS equality — the
course list comes back empty even though matching-language courses
exist. Language filtering must accept base-language matches (the same
rule the bot's supported-L2 checks follow).

Runs a real local Synapse + PostgreSQL (BaseSynapseE2ETest) against the
mock CMS server's Payload-compatible ``/api/course-plans`` route.
"""

import unittest
from typing import Any, Dict, List

import requests

from tests.base_e2e import BaseSynapseE2ETest
from tests.mock_cms_server import MockCmsServer

COURSE_PLAN_EVENT_TYPE = "pangea.course_plan"
PUBLIC_COURSES_URL = (
    "http://localhost:8008/_synapse/client/pangea/v1/public_courses"
)


class TestPublicCoursesLanguageFilterE2E(BaseSynapseE2ETest):
    async def _create_public_course_room(
        self, access_token: str, course_uuid: str
    ) -> str:
        headers = {"Authorization": f"Bearer {access_token}"}
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json={
                "visibility": "public",
                "preset": "public_chat",
                "initial_state": [
                    {
                        "type": COURSE_PLAN_EVENT_TYPE,
                        "state_key": "",
                        "content": {"uuid": course_uuid},
                    }
                ],
            },
            headers=headers,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["room_id"]

    def _get_courses(
        self, access_token: str, **params: str
    ) -> Dict[str, Any]:
        response = requests.get(
            PUBLIC_COURSES_URL,
            params={"limit": "20", **params},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_target_language_filter_matches_base_and_regional_l2(
        self,
    ) -> None:
        postgres = None
        synapse_dir = None
        server_process = None
        stdout_thread = None
        stderr_thread = None
        mock_cms = MockCmsServer()

        try:
            cms_url = mock_cms.start()
            base_course = mock_cms.seed_course_plan(l2="es", title="Base Spanish")
            regional_course = mock_cms.seed_course_plan(
                l2="es-ES", title="Regional Spanish"
            )
            other_course = mock_cms.seed_course_plan(l2="fr", title="French")

            (
                postgres,
                synapse_dir,
                config_path,
                server_process,
                stdout_thread,
                stderr_thread,
            ) = await self.start_test_synapse(
                module_config={
                    "cms_base_url": cms_url,
                    "cms_service_api_key": "test-cms-api-key",
                }
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="teacher",
                password="123123123",
                admin=True,
            )
            _, access_token = await self.login_user(
                user="teacher", password="123123123"
            )

            for course in (base_course, regional_course, other_course):
                await self._create_public_course_room(access_token, course["id"])

            def titles(body: Dict[str, Any]) -> List[str]:
                return sorted(c.get("name") or "" for c in body["chunk"])

            # Unfiltered: all three rooms are public courses.
            unfiltered = self._get_courses(access_token)
            self.assertEqual(unfiltered["filtering_warning"], "")
            self.assertEqual(len(unfiltered["chunk"]), 3)

            # The issue-#53 repro: a base-code filter must return BOTH the
            # base-l2 course and the regionally-tagged one. With exact CMS
            # equality the es-ES course vanishes (and on staging, where
            # courses carry regional tags, the whole list came back empty).
            filtered = self._get_courses(access_token, target_language="es")
            self.assertEqual(filtered["filtering_warning"], "")
            self.assertEqual(
                len(filtered["chunk"]),
                2,
                f"target_language=es should match l2 es AND es-ES; got "
                f"{titles(filtered)}",
            )
            languages = sorted(
                c["target_language"] for c in filtered["chunk"]
            )
            self.assertEqual(languages, ["es", "es-ES"])

            # Regional filter also matches across the base language.
            regional = self._get_courses(access_token, target_language="es-ES")
            self.assertEqual(len(regional["chunk"]), 2)

            # A different language stays excluded.
            french = self._get_courses(access_token, target_language="fr")
            self.assertEqual(len(french["chunk"]), 1)
            self.assertEqual(
                french["chunk"][0]["target_language"], "fr"
            )
        finally:
            mock_cms.stop()
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )


if __name__ == "__main__":
    unittest.main()
