"""Keeps a course's admins at moderator power (50) in its discussion rooms.

A discussion room copies the course's admins at creation. Without this, a
co-teacher removed from the course would keep moderator power in every room
made while they were an admin, and a teacher added later would have none.
The bot, at 100, sends the change, so it can raise and lower admins alike.

Runs on the main process only: the hook fires on every process that sees an
event, and the endpoint that creates these rooms runs there too.

Design: discussion-room.instructions.md ("Power levels").
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from synapse.api.constants import EventTypes
from synapse.events import EventBase
from synapse.module_api import ModuleApi
from synapse.util.async_helpers import Linearizer
from synapse.util.duration import Duration

from synapse_pangea_chat.discussion_room.rooms import (
    course_discussion_rooms,
    mirrored_admin_levels,
)
from synapse_pangea_chat.room_code.instructor_access import (
    course_admins,
    user_power,
)
from synapse_pangea_chat.sentry_capture import capture_exception

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.discussion_room.admin_mirror"
)

COURSE_ADMIN_POWER = 100


class DiscussionAdminMirror:
    def __init__(self, api: ModuleApi, bot_user_id: str):
        self._api = api
        self._bot_user_id = bot_user_id
        self._store = api._hs.get_datastores().main
        self._clock = api._hs.get_clock()
        self._locks = Linearizer("discussion_admin_mirror", clock=self._clock)
        if api.worker_name is None:
            api.register_third_party_rules_callbacks(on_new_event=self.on_new_event)

    async def on_new_event(
        self, event: EventBase, _state: Mapping[Any, EventBase]
    ) -> None:
        """Awaited inline for every event, so this decides from the event
        alone and hands the work to a background task."""
        try:
            if not event.is_state():
                return
            if event.type == EventTypes.PowerLevels:
                self._in_background(self.sync_course, event.room_id)
            elif event.type == EventTypes.Member and isinstance(event.state_key, str):
                self._in_background(self.sync_member, event.room_id, event.state_key)
        except Exception as e:
            # A failure here must never reject or delay the event itself.
            logger.exception(
                "discussion admin mirror hook failed for %s", event.room_id
            )
            capture_exception(e)

    def _in_background(self, func: Any, *args: Any) -> None:
        self._clock.call_later(
            Duration(milliseconds=0),
            self._api.run_as_background_process,
            "discussion_admin_mirror",
            self._guarded,
            func,
            *args,
        )

    async def _guarded(self, func: Any, *args: Any) -> None:
        try:
            await func(*args)
        except Exception as e:
            logger.exception("discussion admin mirror failed for %s", args)
            capture_exception(e)

    async def sync_member(self, room_id: str, user_id: str) -> None:
        """A membership change matters only for someone at admin power in the
        room: an admin joining or leaving the course."""
        state = await self._api.get_room_state(
            room_id,
            event_filter=[(EventTypes.Create, ""), (EventTypes.PowerLevels, "")],
        )
        if user_power(state, user_id) >= COURSE_ADMIN_POWER:
            await self.sync_course(room_id)

    async def sync_course(self, course_id: str) -> None:
        rooms = await course_discussion_rooms(self._store, course_id, self._bot_user_id)
        if not rooms:
            return
        admins = await course_admins(self._api, course_id)
        for ref in rooms:
            async with self._locks.queue(ref.room_id):
                await self._sync_room(ref.room_id, admins)

    async def _sync_room(self, room_id: str, admins: Any) -> None:
        state = await self._api.get_room_state(
            room_id, event_filter=[(EventTypes.PowerLevels, "")]
        )
        power = state.get((EventTypes.PowerLevels, ""))
        if power is None:
            raise RuntimeError(f"discussion room {room_id} has no power levels")
        users = power.content.get("users")
        users = dict(users) if isinstance(users, Mapping) else {}
        updated = mirrored_admin_levels(users, self._bot_user_id, admins)
        if updated is None:
            return
        content = dict(power.content)
        content["users"] = updated
        await self._api.create_and_send_event_into_room(
            {
                "type": EventTypes.PowerLevels,
                "room_id": room_id,
                "sender": self._bot_user_id,
                "state_key": "",
                "content": content,
            }
        )
        logger.info("discussion admin mirror: updated admins in %s", room_id)
