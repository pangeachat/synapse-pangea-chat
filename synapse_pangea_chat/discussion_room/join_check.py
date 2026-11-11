"""Who may join a discussion room directly.

A discussion room admits course members by its join rule, so a student could
call ``/join`` on it and skip placement and its count. Placement writes a
seat before it joins anyone, so a join from anyone without a seat did not
come through placement and is refused. The bot, invited users, seat holders
who were not removed, and the course's admins (whom course_session_admins
joins to read along) are let in. An unban takes a removal back.

Design: discussion-room.instructions.md ("Direct joins").
"""

from __future__ import annotations

import logging
from typing import Literal, Union

from synapse.api.constants import EventTypes
from synapse.api.errors import Codes
from synapse.module_api import NOT_SPAM, ModuleApi

from synapse_pangea_chat.discussion_room.rooms import (
    ACTIVITY_ROLES_EVENT,
    discussion_marker,
    find_seat,
    is_removal,
    room_state_with_prev,
)
from synapse_pangea_chat.room_code.instructor_access import course_admins
from synapse_pangea_chat.sentry_capture import capture_exception

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.discussion_room.join_check"
)


class DiscussionJoinCheck:
    def __init__(self, api: ModuleApi, bot_user_id: str):
        self._api = api
        self._bot_user_id = bot_user_id
        self._state = api._hs.get_storage_controllers().state
        api.register_spam_checker_callbacks(user_may_join_room=self.user_may_join_room)

    async def user_may_join_room(
        self, user_id: str, room_id: str, is_invited: bool
    ) -> Union[Codes, Literal["NOT_SPAM"]]:
        try:
            create = await self._state.get_current_state_event(
                room_id, EventTypes.Create, ""
            )
            if create is None:
                return NOT_SPAM
            marker = discussion_marker(create.sender, create.content, self._bot_user_id)
            if marker is None or user_id == self._bot_user_id or is_invited:
                return NOT_SPAM
            # With the member event's previous membership: an unban is a
            # leave someone else sent, and takes a removal back.
            state = await room_state_with_prev(
                self._api._hs,
                room_id,
                [(ACTIVITY_ROLES_EVENT, ""), (EventTypes.Member, user_id)],
            )
            roles = state.get((ACTIVITY_ROLES_EVENT, ""))
            member = state.get((EventTypes.Member, user_id))
            removed = member is not None and is_removal(member)
            seated = find_seat(roles.content if roles else None, user_id) is not None
            if seated and not removed:
                return NOT_SPAM
            if user_id in await course_admins(self._api, marker.course_id):
                return NOT_SPAM
            logger.info(
                "discussion_room: refused a direct join of %s to %s (%s)",
                user_id,
                room_id,
                "removed" if removed else "no seat",
            )
            return Codes.FORBIDDEN
        except Exception as e:
            # Fails open, captured: a broken check must not block every join
            # on the homeserver.
            logger.exception("discussion_room join check failed for %s", room_id)
            capture_exception(e)
            return NOT_SPAM
