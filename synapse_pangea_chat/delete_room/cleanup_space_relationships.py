import logging
from typing import Optional

from synapse.module_api import ModuleApi

from synapse_pangea_chat.delete_room.constants import (
    EVENT_TYPE_M_ROOM_MEMBER,
    EVENT_TYPE_M_ROOM_POWER_LEVELS,
    EVENT_TYPE_M_SPACE_CHILD,
    EVENT_TYPE_M_SPACE_PARENT,
    MEMBERSHIP_CONTENT_KEY,
    MEMBERSHIP_JOIN,
)

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.delete_room.cleanup_space_relationships"
)


async def _unlink_sender(
    api: ModuleApi, space_id: str, requester_id: str
) -> Optional[str]:
    """
    The user to send a space's unlink as: the requester when the space lets
    them change its children, otherwise its highest-ranked joined local member
    who may (delete-room.instructions.md § What deletion does). None when no
    one may.
    """
    state = await api.get_room_state(
        room_id=space_id,
        event_filter=[
            (EVENT_TYPE_M_ROOM_POWER_LEVELS, ""),
            (EVENT_TYPE_M_ROOM_MEMBER, None),
        ],
    )
    power_levels = None
    members = []
    for state_event in state.values():
        if state_event.type == EVENT_TYPE_M_ROOM_POWER_LEVELS:
            power_levels = state_event.content
        elif state_event.content.get(
            MEMBERSHIP_CONTENT_KEY
        ) == MEMBERSHIP_JOIN and api.is_mine(state_event.state_key):
            members.append(state_event.state_key)

    if power_levels is None:
        # Without a power levels event every member may send state events
        required, users, users_default = 0, {}, 0
    else:
        users = power_levels.get("users", {})
        users_default = power_levels.get("users_default", 0)
        required = power_levels.get("events", {}).get(
            EVENT_TYPE_M_SPACE_CHILD, power_levels.get("state_default", 50)
        )

    def level(user_id: str) -> int:
        return users.get(user_id, users_default)

    if requester_id in members and level(requester_id) >= required:
        return requester_id
    able = [member for member in members if level(member) >= required]
    if not able:
        return None
    # Ties go to the first user id, so the choice is stable
    return max(sorted(able), key=level)


async def cleanup_space_relationships(
    api: ModuleApi, room_id: str, requester_id: str
) -> None:
    """
    Clean up space relationships when a room is deleted.

    Args:
        api: The ModuleApi instance
        room_id: The ID of the room being deleted
        requester_id: The user deleting the room

    Removes m.space.child events from parent spaces that reference the deleted
    room. The room's own m.space.parent events go away with the purge.
    """
    try:
        # Step 1: Get all m.space.parent events from the deleted room
        # These tell us which spaces this room was a child of
        space_parent_events = await api.get_room_state(
            room_id=room_id,
            event_filter=[(EVENT_TYPE_M_SPACE_PARENT, None)],
        )

        parent_space_ids = []
        for state_event in space_parent_events.values():
            if state_event.type == EVENT_TYPE_M_SPACE_PARENT:
                # The state_key is the parent space ID
                parent_space_id = state_event.state_key
                if parent_space_id:
                    parent_space_ids.append(parent_space_id)

        logger.info(
            "Found %d parent spaces for room %s", len(parent_space_ids), room_id
        )

        # Step 2: Remove m.space.child events from parent spaces
        for parent_space_id in parent_space_ids:
            try:
                # Check if the parent space has a m.space.child event for this room
                space_child_events = await api.get_room_state(
                    room_id=parent_space_id,
                    event_filter=[(EVENT_TYPE_M_SPACE_CHILD, room_id)],
                )

                # If there's a m.space.child event for this room, remove it
                if space_child_events:
                    sender = await _unlink_sender(api, parent_space_id, requester_id)
                    if sender is None:
                        logger.warning(
                            "No member of parent space %s may remove its link to "
                            "deleted room %s; leaving it",
                            parent_space_id,
                            room_id,
                        )
                        continue
                    logger.info(
                        "Removing m.space.child event for %s from parent space %s as %s",
                        room_id,
                        parent_space_id,
                        sender,
                    )
                    # Send an empty state event to remove the relationship
                    await api.create_and_send_event_into_room(
                        {
                            "type": EVENT_TYPE_M_SPACE_CHILD,
                            "state_key": room_id,
                            "room_id": parent_space_id,
                            "sender": sender,
                            "content": {},  # Empty content removes the state
                        }
                    )

            except Exception as e:
                logger.error(
                    "Failed to remove m.space.child event from parent space %s: %s",
                    parent_space_id,
                    e,
                )
                # Continue with other parent spaces even if one fails
                continue

    except Exception as e:
        logger.error(
            "Failed to cleanup space relationships for room %s: %s", room_id, e
        )
        # Don't re-raise the exception as this should not block room deletion
