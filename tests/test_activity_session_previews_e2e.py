import logging
from typing import Any, Dict, Optional

import requests

from .base_e2e import BaseSynapseE2ETest

logger = logging.getLogger(__name__)

MODULE_CONFIG = {
    "room_preview_state_event_types": [
        "pangea.activity_plan",
        "pangea.activity_roles",
    ],
    # The suite makes more GETs than the default 10-per-burst allows.
    "room_preview_requests_per_burst": 1000,
}

# The many-children scenario creates well over the old client-side hierarchy
# page size in rooms; lift the message ratelimits so setup isn't throttled.
SYNAPSE_CONFIG_OVERRIDES = {
    "rc_message": {"per_second": 1000, "burst_count": 100000},
    "rc_joins": {
        "local": {"per_second": 1000, "burst_count": 100000},
        "remote": {"per_second": 1000, "burst_count": 100000},
    },
}

# Comfortably past the 100-room hierarchy page the old client-side discovery
# was limited to (the pangeachat/client#7982 miss).
MANY_CHILDREN_COUNT = 110


class TestActivitySessionPreviewsE2E(BaseSynapseE2ETest):
    async def test_activity_session_previews(self) -> None:
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
            ) = await self.start_test_synapse(
                module_config=MODULE_CONFIG,
                synapse_config_overrides=SYNAPSE_CONFIG_OVERRIDES,
            )

            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="member",
                password="pw1",
                admin=False,
            )
            await self.register_user(
                config_path=config_path,
                dir=synapse_dir,
                user="outsider",
                password="pw2",
                admin=False,
            )
            member_id, member_token = await self.login_user("member", "pw1")
            outsider_id, outsider_token = await self.login_user("outsider", "pw2")
            self._server_name = member_id.split(":", 1)[1]

            self.previews_url = (
                f"{self.server_url}"
                "/_synapse/client/pangea/v1/activity_session_previews"
            )

            # One space with a mixed set of children: two sessions for two
            # different activities, a plain chat, and a removed session.
            space_id = await self._create_space(member_token)
            session_1 = await self._create_session_room(member_token, "act-1")
            session_2 = await self._create_session_room(member_token, "act-2")
            chat = await self.create_private_room(member_token)
            removed = await self._create_session_room(member_token, "act-removed")
            for child in (session_1, session_2, chat, removed):
                self._add_child(member_token, space_id, child)
            self._remove_child(member_token, space_id, removed)

            await self._test_returns_only_session_children(
                member_token, space_id, session_1, session_2, chat, removed
            )
            await self._test_activity_scope(
                member_token, space_id, session_1, session_2
            )
            await self._test_preview_shape(member_token, space_id, session_1)
            await self._test_non_member_space_dropped(outsider_token, space_id)
            await self._test_mixed_membership_batch(
                member_token, outsider_id, outsider_token, space_id, session_1
            )
            await self._test_no_rooms_param(member_token)
            await self._test_many_children_all_found(member_token)

        finally:
            self.stop_synapse(
                server_process=server_process,
                stdout_thread=stdout_thread,
                stderr_thread=stderr_thread,
                synapse_dir=synapse_dir,
                postgres=postgres,
            )

    # --- scenario helpers -------------------------------------------------

    async def _test_returns_only_session_children(
        self,
        token: str,
        space_id: str,
        session_1: str,
        session_2: str,
        chat: str,
        removed: str,
    ) -> None:
        """Sessions come back; the chat child, the removed session child, and
        the space itself do not."""
        rooms = self._get_previews(token, rooms=space_id)
        self.assertEqual(set(rooms.keys()), {session_1, session_2})
        self.assertNotIn(chat, rooms)
        self.assertNotIn(removed, rooms)
        self.assertNotIn(space_id, rooms)

    async def _test_activity_scope(
        self, token: str, space_id: str, session_1: str, session_2: str
    ) -> None:
        """?activity narrows to that activity's sessions only."""
        rooms = self._get_previews(token, rooms=space_id, activity="act-1")
        self.assertEqual(set(rooms.keys()), {session_1})

        rooms = self._get_previews(token, rooms=space_id, activity="act-none")
        self.assertEqual(rooms, {})

    async def _test_preview_shape(
        self, token: str, space_id: str, session_1: str
    ) -> None:
        """The per-room payload is room_preview's shape: event type ->
        state key -> event JSON, with the activity plan projected to its
        reference keys."""
        rooms = self._get_previews(token, rooms=space_id)
        plan_event = rooms[session_1]["pangea.activity_plan"]["default"]
        self.assertEqual(plan_event["content"], {"activity_id": "act-1"})

    async def _test_non_member_space_dropped(
        self, outsider_token: str, space_id: str
    ) -> None:
        """A caller not joined to the space gets nothing for it — silently,
        not an error."""
        rooms = self._get_previews(outsider_token, rooms=space_id)
        self.assertEqual(rooms, {})

    async def _test_mixed_membership_batch(
        self,
        member_token: str,
        outsider_id: str,
        outsider_token: str,
        member_only_space: str,
        member_only_session: str,
    ) -> None:
        """In a batch mixing member and non-member spaces, only the member
        spaces answer."""
        shared_space = await self._create_space(member_token)
        shared_session = await self._create_session_room(member_token, "act-shared")
        self._add_child(member_token, shared_space, shared_session)
        self.assertTrue(
            await self.invite_user_to_room(shared_space, outsider_id, member_token)
        )
        self.assertTrue(await self.accept_room_invitation(shared_space, outsider_token))

        rooms = self._get_previews(
            outsider_token, rooms=f"{member_only_space},{shared_space}"
        )
        self.assertEqual(set(rooms.keys()), {shared_session})
        self.assertNotIn(member_only_session, rooms)

    async def _test_no_rooms_param(self, token: str) -> None:
        response = requests.get(
            self.previews_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"rooms": {}})

    async def _test_many_children_all_found(self, token: str) -> None:
        """A space with more children than the old client-side hierarchy page
        limit still returns every session — the #7982 regression, server-side.
        The session is added last so it sits past any first 'page'."""
        big_space = await self._create_space(token)
        for _ in range(MANY_CHILDREN_COUNT):
            chat = await self.create_private_room(token)
            self._add_child(token, big_space, chat)
        late_session = await self._create_session_room(token, "act-late")
        self._add_child(token, big_space, late_session)

        rooms = self._get_previews(token, rooms=big_space)
        self.assertEqual(set(rooms.keys()), {late_session})

    # --- request helpers --------------------------------------------------

    def _get_previews(
        self, token: str, *, rooms: str, activity: Optional[str] = None
    ) -> Dict[str, Any]:
        params: Dict[str, str] = {"rooms": rooms}
        if activity is not None:
            params["activity"] = activity
        response = requests.get(
            self.previews_url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["rooms"]

    async def _create_space(self, token: str) -> str:
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "visibility": "private",
                "preset": "private_chat",
                "creation_content": {"type": "m.space"},
            },
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["room_id"]

    async def _create_session_room(self, token: str, activity_id: str) -> str:
        response = requests.post(
            f"{self.server_url}/_matrix/client/v3/createRoom",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "visibility": "private",
                "preset": "private_chat",
                "initial_state": [
                    {
                        "type": "pangea.activity_plan",
                        "state_key": "",
                        "content": {"activity_id": activity_id},
                    }
                ],
            },
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()["room_id"]

    def _put_child_state(
        self, token: str, space_id: str, child_id: str, content: Dict[str, Any]
    ) -> None:
        response = requests.put(
            f"{self.server_url}/_matrix/client/v3/rooms/{space_id}"
            f"/state/m.space.child/{child_id}",
            headers={"Authorization": f"Bearer {token}"},
            json=content,
            timeout=10,
        )
        self.assertEqual(response.status_code, 200)

    def _add_child(self, token: str, space_id: str, child_id: str) -> None:
        self._put_child_state(token, space_id, child_id, {"via": [self._server_name]})

    def _remove_child(self, token: str, space_id: str, child_id: str) -> None:
        # Removing a space child is writing an empty m.space.child event.
        self._put_child_state(token, space_id, child_id, {})
