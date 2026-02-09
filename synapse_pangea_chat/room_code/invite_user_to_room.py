import logging

from synapse.module_api import ModuleApi

from synapse_pangea_chat.room_code.constants import (
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_INVITE,
)
from synapse_pangea_chat.room_code.get_inviter_user import get_inviter_user

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.room_code.invite_user_to_room"
)


async def invite_user_to_room(api: ModuleApi, user_id: str, room_id: str) -> None:
    # Get a user with permission to invite
    logger.info(f"Getting inviter user for room {room_id}")
    inviter_user = await get_inviter_user(api=api, room_id=room_id)
    if inviter_user is None:
        logger.warning(f"No inviter user found for room {room_id}")
        return
    inviter_user_id = inviter_user.to_string()
    logger.info(f"Inviter user for room {room_id}: {inviter_user_id}")
    content = {MEMBERSHIP_CONTENT_KEY: MEMBERSHIP_INVITE}
    await api.update_room_membership(
        sender=inviter_user_id,
        target=user_id,
        room_id=room_id,
        new_membership=MEMBERSHIP_INVITE,
        content=content,
    )
    logger.info(f"Invited {user_id} to room {room_id}")
