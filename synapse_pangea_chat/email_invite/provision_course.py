"""First instructor creates a course; retries only recover its original room."""
from __future__ import annotations

import json
from copy import deepcopy

from synapse.types import create_requester

from synapse_pangea_chat.email_invite.course_invitations import (
    recovery_required,
    unavailable,
)
from synapse_pangea_chat.email_invite.create_course_space import (
    DEFAULT_SPACE_POWER_LEVELS,
)
from synapse_pangea_chat.room_code.code_lookup import new_unique_code
from synapse_pangea_chat.room_code.instructor_access import is_joined_instructor

MARKER = "org.pangea.course_invitation"


class CourseProvisioner:
    def __init__(self, api, invitations, claims, notifier):
        self.api = api
        self.invitations = invitations
        self.claims = claims
        self.notifier = notifier
        self.store = api._hs.get_datastores().main

    async def authorized(self, invitation, user):
        if (
            invitation["status"] == "revoked"
            or invitation["claimant"] != user
            or not invitation["room_id"]
        ):
            return False
        state = await self.api.get_room_state(invitation["room_id"])
        return is_joined_instructor(state, user)

    async def recover_room(self, invitation):
        if invitation["room_id"]:
            return invitation["room_id"]

        def find(txn):
            txn.execute(
                """SELECT j.room_id, j.json FROM event_json j
                JOIN state_events s ON s.event_id = j.event_id
                WHERE s.type = 'm.room.create' AND j.json LIKE ?""",
                ("%" + invitation["invitation_id"] + "%",),
            )
            rooms = []
            for room, raw in txn.fetchall():
                event = json.loads(raw)
                if (
                    event.get("content", {}).get(MARKER) == invitation["invitation_id"]
                    and event.get("sender") == invitation["claimant"]
                ):
                    rooms.append(room)
            if len(set(rooms)) != 1:
                raise recovery_required()
            return rooms[0]

        return await self.store.db_pool.runInteraction(
            "pangea_invitation_recover_room", find
        )

    async def claim(self, invitation, user):
        await self.claims._ensure_table()
        # Generate before reserving: failure to find a free class code cannot
        # strand a creation operation that has not yet started.
        if invitation["status"] == "revoked" or invitation["claimant"] not in (
            None,
            user,
        ):
            raise unavailable()
        class_code = None
        if invitation["status"] == "prepared":
            class_code = await new_unique_code(self.store, self.claims)
            if not class_code:
                raise recovery_required()
        invitation, create = await self.invitations.reserve_creation(
            invitation["invitation_id"], user
        )
        if invitation["status"] == "completed":
            if not await self.authorized(invitation, user):
                raise unavailable()
            return invitation["room_id"]
        if create:
            spec = invitation["specification"]
            power = deepcopy(DEFAULT_SPACE_POWER_LEVELS)
            power["users"] = {user: 100}
            initial = [
                {
                    "type": "m.room.join_rules",
                    "state_key": "",
                    "content": {"join_rule": "knock", "access_code": class_code},
                },
                {"type": "m.room.power_levels", "state_key": "", "content": power},
                {
                    "type": "pangea.course_plan",
                    "state_key": "",
                    "content": {
                        "uuid": spec["course_plan_id"],
                        "l2": spec["target_language"],
                    },
                },
                {
                    "type": "pangea.course_settings",
                    "state_key": "",
                    "content": {"require_analytics_access": True},
                },
            ]
            if spec.get("image_url"):
                initial.append(
                    {
                        "type": "m.room.avatar",
                        "state_key": "",
                        "content": {"url": spec["image_url"]},
                    }
                )
            # The marker is part of the immutable first event, not a state
            # write after create_room returns (which would leave a crash gap).
            room, _, _ = await self.api._hs.get_room_creation_handler().create_room(
                requester=create_requester(
                    user, authenticated_entity=self.api.server_name
                ),
                config={
                    "preset": "private_chat",
                    "name": spec["title"],
                    "topic": spec.get("description", ""),
                    "visibility": "private",
                    "creation_content": {
                        "type": "m.space",
                        MARKER: invitation["invitation_id"],
                    },
                    "initial_state": initial,
                },
                ratelimit=False,
            )
        else:
            room = await self.recover_room(invitation)
        await self.invitations.associate(invitation["invitation_id"], user, room)
        invitation = await self.invitations.get(invitation["invitation_id"])
        if not await self.authorized(invitation, user):
            # Never invite/join/promote in recovery. A leave or demotion must
            # win even if completion was interrupted before it was recorded.
            raise recovery_required()
        state = await self.api.get_room_state(room)
        spec = invitation["specification"]
        required = {
            "pangea.course_plan": {
                "uuid": spec["course_plan_id"],
                "l2": spec["target_language"],
            },
            "pangea.course_settings": {"require_analytics_access": True},
            "m.room.join_rules": {"join_rule": "knock"},
        }
        for kind, content in required.items():
            event = state.get((kind, ""))
            if not event or any(event.content.get(k) != v for k, v in content.items()):
                raise recovery_required()
        join = state[("m.room.join_rules", "")].content
        power = state[("m.room.power_levels", "")].content
        if (
            not join.get("access_code")
            or power.get("events", {}).get("m.space.child") != 0
        ):
            raise recovery_required()
        await self.invitations.complete(
            invitation["invitation_id"],
            user,
            room,
            self.api._hs.get_clock().time_msec(),
        )
        # The durable debt is already committed. Background loop also finds it.
        from synapse.logging.context import run_in_background

        run_in_background(self.notifier.notify, room, user)
        return room
