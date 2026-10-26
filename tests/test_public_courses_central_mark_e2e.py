"""E2E test for the central-course mark in the public course catalog.

A course is central when its ``pangea.course_settings`` carries
``"central": true`` written by a Pangea admin account. With no
``central_course_admin_user_ids`` configured, that account is
``@support:<server name>``. A course admin who marks their own course is
ignored, because any course admin can write their course's settings.
"""

import unittest
from typing import Any, Dict
from urllib.parse import quote

import requests

from tests.base_e2e import BaseSynapseE2ETest

PUBLIC_COURSES_PATH = "/_synapse/client/pangea/v1/public_courses"


class TestPublicCoursesCentralMarkE2E(BaseSynapseE2ETest):
    def _create_course(self, token: str, name: str, plan_id: str) -> str:
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json={
                "name": name,
                "visibility": "public",
                "preset": "public_chat",
                "initial_state": [
                    {
                        "type": "pangea.course_plan",
                        "state_key": "",
                        "content": {"uuid": plan_id, "l2": "es"},
                    }
                ],
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["room_id"]

    def _set_settings(self, token: str, room_id: str, content: Dict[str, Any]) -> None:
        response = requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/{quote(room_id, safe='')}"
            "/state/pangea.course_settings/",
            json=content,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _catalog_central(self, token: str) -> Dict[str, bool]:
        response = requests.get(
            f"{self.server_url}{PUBLIC_COURSES_PATH}",
            params={"limit": 100},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return {c["room_id"]: c["central"] for c in response.json()["chunk"]}

    async def test_only_a_mark_from_the_support_account_counts(self) -> None:
        await self.homeserver()
        # The default central admin; no other test registers this localpart.
        _, support_token = await self._register_localpart("support", "pw", False)
        _, teacher_token = await self.new_user("teacher")

        central = self._create_course(support_token, "Spanish Central", "plan-a")
        self._set_settings(support_token, central, {"central": True})

        self_marked = self._create_course(teacher_token, "My Spanish", "plan-b")
        self._set_settings(teacher_token, self_marked, {"central": True})

        unmarked = self._create_course(teacher_token, "Other Spanish", "plan-c")

        catalog = self._catalog_central(teacher_token)
        self.assertIs(catalog[central], True)
        self.assertIs(catalog[self_marked], False)
        self.assertIs(catalog[unmarked], False)


if __name__ == "__main__":
    unittest.main()
