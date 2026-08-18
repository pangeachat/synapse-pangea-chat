"""Blocked join gate — refuse knocks and joins from a user every admin of the
room has blocked.

Design: .github/instructions/blocked-join-gate.instructions.md
"""

import logging
from typing import Any, Literal, Mapping, Optional, Tuple, Union

from synapse.api.errors import Codes
from synapse.events import EventBase
from synapse.module_api import NOT_SPAM, ModuleApi

from synapse_pangea_chat.blocked_join_gate.constants import (
    EVENT_TYPE_M_ROOM_MEMBER,
    MEMBERSHIP_KNOCK,
)
from synapse_pangea_chat.blocked_join_gate.is_blocked_by_room_admin import (
    is_blocked_by_room_admin,
)

logger = logging.getLogger("synapse.module.synapse_pangea_chat.blocked_join_gate")

__all__ = ["BlockedJoinGate", "is_blocked_by_room_admin"]

# Both refusals surface as a bare M_FORBIDDEN on purpose: Matrix ignore
# semantics keep the ignored user unaware they are ignored, so no reason and
# no custom errcode.
_JOIN_REFUSED = Codes.FORBIDDEN
_KNOCK_REFUSED: Tuple[bool, Optional[dict]] = (False, None)
_KNOCK_ALLOWED: Tuple[bool, Optional[dict]] = (True, None)


class BlockedJoinGate:
    def __init__(self, config: Any, api: ModuleApi):
        self._api = api
        self._api.register_spam_checker_callbacks(
            user_may_join_room=self.user_may_join_room,
        )
        # Knocks never reach the spam checker (Synapse only spam-checks
        # non-membership events), so they are vetoed via third-party rules,
        # which sees every event before it is stored.
        self._api.register_third_party_rules_callbacks(
            check_event_allowed=self.check_event_allowed,
        )

    async def user_may_join_room(
        self, user_id: str, room_id: str, is_invited: bool
    ) -> Union[Codes, Literal["NOT_SPAM"]]:
        """Joins — direct, restricted, or accepting an invite. ``is_invited``
        is deliberately ignored: a pending invite does not override a block
        by every admin."""
        # Synapse skips this callback for server admins and for the room
        # creator's initial join, which is fine: neither is a join request.
        if await is_blocked_by_room_admin(self._api, room_id, user_id):
            return _JOIN_REFUSED
        return NOT_SPAM

    async def check_event_allowed(
        self,
        event: EventBase,
        state_events: Mapping[Tuple[str, str], EventBase],
    ) -> Tuple[bool, Optional[dict]]:
        """Knocks — vetoed before the event is stored, so admins never see
        the knock in their queue. Every other event passes untouched."""
        if not _is_knock_request(event):
            return _KNOCK_ALLOWED
        if await is_blocked_by_room_admin(self._api, event.room_id, event.sender):
            return _KNOCK_REFUSED
        return _KNOCK_ALLOWED


def _is_knock_request(event: EventBase) -> bool:
    return (
        event.type == EVENT_TYPE_M_ROOM_MEMBER
        and event.is_state()
        and event.membership == MEMBERSHIP_KNOCK
        # A knock event whose sender is not its target is not a request from
        # the target; only self-knocks are gated.
        and event.sender == event.state_key
    )
