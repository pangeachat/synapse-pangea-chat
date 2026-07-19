"""E2E tests for public_courses eligibility, language filter, and paging.

Runs a real local Synapse + PostgreSQL (BaseSynapseE2ETest) so the catalog SQL
itself is exercised. No CMS is involved: a course's target language is read from
``l2`` on its own ``pangea.course_plan`` state event.

Covers the issue-#53 repro (a base-code filter must match regionally-tagged
courses) and the issue-#7542 rules: current state only, plan id from ``uuid``
with a ``course_plan_id`` fallback, and full pages.
"""

import unittest
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from tests.base_e2e import BaseSynapseE2ETest

COURSE_PLAN_EVENT_TYPE = "pangea.course_plan"
PUBLIC_COURSES_PATH = "/_synapse/client/pangea/v1/public_courses"


class TestPublicCoursesLanguageFilterE2E(BaseSynapseE2ETest):
    async def _create_public_course_room(
        self,
        access_token: str,
        name: str,
        plan_content: Optional[Dict[str, Any]],
        visibility: str = "public",
    ) -> str:
        """Create a course room. *visibility* is directory publication only.

        ``preset`` stays ``public_chat`` regardless, so a ``private``
        visibility room is still joinable — the only thing that changes is
        whether it is published in the public room directory. That isolates
        the publication half of the eligibility rule from the join rule, which
        the catalog does not check.
        """
        headers = {"Authorization": f"Bearer {access_token}"}
        initial_state: List[Dict[str, Any]] = []
        if plan_content is not None:
            initial_state.append(
                {
                    "type": COURSE_PLAN_EVENT_TYPE,
                    "state_key": "",
                    "content": plan_content,
                }
            )
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            json={
                "name": name,
                "visibility": visibility,
                "preset": "public_chat",
                "initial_state": initial_state,
            },
            headers=headers,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["room_id"]

    def _set_course_plan(
        self, access_token: str, room_id: str, content: Dict[str, Any]
    ) -> None:
        room_id_path = quote(room_id, safe="")
        response = requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/{room_id_path}"
            f"/state/{COURSE_PLAN_EVENT_TYPE}",
            json=content,
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

    def _get_courses_raw(self, access_token: str, **params: str):
        return requests.get(
            f"{self.server_url}{PUBLIC_COURSES_PATH}",
            params={"limit": "20", **params},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )

    @staticmethod
    def _names(body: Dict[str, Any]) -> List[str]:
        return sorted(c.get("name") or "" for c in body["chunk"])

    async def test_catalog_eligibility_language_filter_and_paging(self) -> None:
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

            await self._create_public_course_room(
                token, "Base Spanish", {"uuid": "plan-es", "l2": "es"}
            )
            await self._create_public_course_room(
                token, "Regional Spanish", {"uuid": "plan-es-es", "l2": "es-ES"}
            )
            await self._create_public_course_room(
                token, "French", {"uuid": "plan-fr", "l2": "fr"}
            )
            # A quest with zero missions is still a published course: Synapse
            # cannot see quest content and does not check it.
            await self._create_public_course_room(
                token, "Empty Quest", {"uuid": "plan-empty", "l2": "de"}
            )
            # Server-side course spaces write course_plan_id, not uuid.
            await self._create_public_course_room(
                token, "Fallback Id", {"course_plan_id": "plan-fallback", "l2": "it"}
            )
            # No l2 at all.
            await self._create_public_course_room(
                token, "No Language", {"uuid": "plan-nolang"}
            )
            # Public room with no course plan: not a course.
            await self._create_public_course_room(token, "Just A Room", None)
            # Carries a plan, but is not published in the room directory: the
            # other half of the conjunction. It shares an l2 with the Spanish
            # courses, so dropping the publication check would surface it in
            # both the unfiltered catalog and the ?target_language=es one.
            await self._create_public_course_room(
                token,
                "Unlisted Course",
                {"uuid": "plan-unlisted", "l2": "es"},
                visibility="private",
            )
            # Had a plan, then had it removed: no longer a course.
            removed_room = await self._create_public_course_room(
                token, "Removed Plan", {"uuid": "plan-removed", "l2": "es"}
            )
            self._set_course_plan(token, removed_room, {})

            unfiltered = self._get_courses(token)
            self.assertEqual(
                self._names(unfiltered),
                [
                    "Base Spanish",
                    "Empty Quest",
                    "Fallback Id",
                    "French",
                    "No Language",
                    "Regional Spanish",
                ],
                f"unexpected catalog: {unfiltered}",
            )

            course_ids = {c["name"]: c["course_id"] for c in unfiltered["chunk"]}
            self.assertEqual(course_ids["Base Spanish"], "plan-es")
            self.assertEqual(course_ids["Fallback Id"], "plan-fallback")

            self.assertIsNone(
                next(
                    c["target_language"]
                    for c in unfiltered["chunk"]
                    if c["name"] == "No Language"
                )
            )

            # issue #53: a base-code filter matches regionally-tagged courses.
            filtered = self._get_courses(token, target_language="es")
            self.assertEqual(
                self._names(filtered), ["Base Spanish", "Regional Spanish"]
            )
            # ...and a room with no l2 is excluded once a filter is passed.
            self.assertNotIn("No Language", self._names(filtered))

            regional = self._get_courses(token, target_language="es-MX")
            self.assertEqual(
                self._names(regional), ["Base Spanish", "Regional Spanish"]
            )

            french = self._get_courses(token, target_language="fr")
            self.assertEqual(self._names(french), ["French"])

            # Paging: filtering happens before the page is cut, so pages are
            # full while the catalog has more, and next_batch means more exist.
            seen: List[str] = []
            params: Dict[str, str] = {"limit": "2"}
            for _ in range(6):
                page = self._get_courses(token, **params)
                seen.extend(c["room_id"] for c in page["chunk"])
                if page["next_batch"] is None:
                    break
                self.assertEqual(len(page["chunk"]), 2, f"thin page: {page}")
                params = {"limit": "2", "since": page["next_batch"]}

            self.assertEqual(len(seen), 6)
            self.assertEqual(len(set(seen)), 6)
            self.assertEqual(
                seen,
                sorted(c["room_id"] for c in unfiltered["chunk"]),
                "paging must walk the catalog in room-id order without gaps",
            )

            # A cursor or filter the catalog cannot honor is refused. Serving
            # 200 with an empty chunk (bad cursor) or the whole catalog (bad
            # filter) would answer a question the caller did not ask.
            for bad_params in ({"since": "abc"}, {"since": "-5"}):
                rejected = self._get_courses_raw(token, **bad_params)
                self.assertEqual(rejected.status_code, 400, rejected.text)
                self.assertEqual(rejected.json()["errcode"], "M_INVALID_PARAM")

            bad_filter = self._get_courses_raw(token, target_language="-")
            self.assertEqual(bad_filter.status_code, 400, bad_filter.text)
            self.assertEqual(bad_filter.json()["errcode"], "M_INVALID_PARAM")
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
