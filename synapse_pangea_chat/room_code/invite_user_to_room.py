import logging
from typing import Any, Dict, Optional

from synapse.module_api import ModuleApi

from synapse_pangea_chat.joined_via import module_write, named
from synapse_pangea_chat.room_code.constants import (
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_INVITE,
)
from synapse_pangea_chat.room_code.get_inviter_user import get_inviter_user

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.room_code.invite_user_to_room"
)


class NoInviterAvailableError(Exception):
    """No local joined member could be found (or promoted) to issue the
    invite. Raised instead of returning silently so the caller counts the
    room as failed rather than invited (issue #197)."""


async def invite_user_to_room(
    api: ModuleApi, user_id: str, room_id: str, via: Optional[str] = None
) -> None:
    """Invite as a member who may; ``via`` names the module path in the
    invite (``joined_via``), which the member's join then inherits."""
    # Get a user with permission to invite
    logger.info(f"Getting inviter user for room {room_id}")
    inviter_user = await get_inviter_user(api=api, room_id=room_id)
    if inviter_user is None:
        raise NoInviterAvailableError(f"No inviter user found for room {room_id}")
    inviter_user_id = inviter_user.to_string()
    logger.info(f"Inviter user for room {room_id}: {inviter_user_id}")
    content: Dict[str, Any] = {MEMBERSHIP_CONTENT_KEY: MEMBERSHIP_INVITE}
    if via is not None:
        content.update(named(via))
    with module_write():
        await api.update_room_membership(
            sender=inviter_user_id,
            target=user_id,
            room_id=room_id,
            new_membership=MEMBERSHIP_INVITE,
            content=content,
        )
    logger.info(f"Invited {user_id} to room {room_id}")
