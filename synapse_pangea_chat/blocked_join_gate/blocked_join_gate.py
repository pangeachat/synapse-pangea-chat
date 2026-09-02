"""Blocked join gate — refuse knocks and joins from a user every admin of the
room has blocked.

Design: .github/instructions/blocked-join-gate.instructions.md
"""

import contextvars
import logging
from contextlib import contextmanager
from typing import Any, Iterator, Literal, Mapping, Optional, Tuple, Union

from synapse.api.errors import Codes
from synapse.events import EventBase
from synapse.module_api import NOT_SPAM, ModuleApi

from synapse_pangea_chat.blocked_join_gate.constants import (
    EVENT_TYPE_M_ROOM_MEMBER,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_KNOCK,
)
from synapse_pangea_chat.blocked_join_gate.is_blocked_by_room_admin import (
    is_blocked_by_room_admin,
)

logger = logging.getLogger("synapse.module.synapse_pangea_chat.blocked_join_gate")

# Both refusals surface as a bare M_FORBIDDEN on purpose: Matrix ignore
# semantics keep the ignored user unaware they are ignored, so no reason and
# no custom errcode.
_JOIN_REFUSED = Codes.FORBIDDEN
_KNOCK_REFUSED: Tuple[bool, Optional[dict]] = (False, None)
_KNOCK_ALLOWED: Tuple[bool, Optional[dict]] = (True, None)

# Set while our own module code performs a membership operation on a user's
# behalf. The spam-checker callback cannot see the requester, so the flow
# itself declares that its joins are orchestration, not a membership request.
# Contextvars follow the await chain, so the value is visible inside the
# update_room_membership call and nowhere else.
_server_initiated = contextvars.ContextVar(
    "pangea_server_initiated_entry", default=False
)


@contextmanager
def server_initiated_entry() -> Iterator[None]:
    """Exempt the enclosed membership operations from the blocked join gate."""
    token = _server_initiated.set(True)
    try:
        yield
    finally:
        _server_initiated.reset(token)


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
        if _server_initiated.get():
            return NOT_SPAM
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
        # content.get, not event.membership: the property raises KeyError on
        # a member event without the key, and a 500 here rejects the event.
        and event.content.get(MEMBERSHIP_CONTENT_KEY) == MEMBERSHIP_KNOCK
        # A knock event whose sender is not its target is not a request from
        # the target; only self-knocks are gated.
        and event.sender == event.state_key
    )
